"""Characterisation test for the shared ACP engine base.

Written BEFORE the collapse of `cline/devin/cursor/gemini/grok_acp.py` onto a
common base, and red until that base exists. It pins three things:

1. every ACP adapter really sits on the shared base (not a copy of it);
2. the shared machinery is defined ONCE — each per-engine module is checked
   against a table of method names it must no longer re-implement, so a future
   "just paste it back into this one engine" regression fails here;
3. the base's own turn loop works, exercised end-to-end through a fake ACP
   child rather than through any one adapter. This is the arm that can fail on
   its own merits: it drives `AcpEngineBase.run_turn_with_progress` over a
   scripted JSON-RPC conversation and asserts the exact progress-event
   sequence, the aggregated text, the TurnResult fields, and that a mid-turn
   `session/request_permission` is answered on the wire.
"""

from __future__ import annotations

import json
import queue
from typing import Any

import pytest

from agent_redis_bridge.engines._acp_base import AcpEngineBase, HealthReportingAcpEngine


# --------------------------------------------------------------------------
# 1 + 2: structural pins
# --------------------------------------------------------------------------

def _engine_classes():
    from agent_redis_bridge.engines.cline_acp import ClineAcpEngine
    from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine
    from agent_redis_bridge.engines.devin_acp import DevinAcpEngine
    from agent_redis_bridge.engines.gemini_acp import GeminiAcpEngine
    from agent_redis_bridge.engines.generic_acp import GenericAcpEngine
    from agent_redis_bridge.engines.grok_acp import GrokAcpEngine

    return {
        "cline_acp": ClineAcpEngine,
        "cursor_acp": CursorAcpEngine,
        "devin_acp": DevinAcpEngine,
        "gemini_acp": GeminiAcpEngine,
        "generic_acp": GenericAcpEngine,
        "grok_acp": GrokAcpEngine,
    }


@pytest.mark.parametrize("module_name", sorted(_engine_classes()))
def test_every_acp_adapter_subclasses_the_shared_base(module_name: str) -> None:
    cls = _engine_classes()[module_name]
    assert issubclass(cls, AcpEngineBase), (
        f"{cls.__name__} does not sit on AcpEngineBase — it is still carrying its "
        "own copy of the ACP transport."
    )


# Methods the shared base owns. An entry here means: this engine class must not
# define the name in its OWN __dict__; it inherits it. Engines that legitimately
# keep a quirked version are absent from their row, with the reason recorded in
# the engine module.
SHARED_METHODS_BY_ENGINE: dict[str, tuple[str, ...]] = {
    # cursor/devin/cline run the base transport AND the base prompt loop.
    "cursor_acp": (
        "start", "stop", "steer", "interrupt", "request", "send_request_no_wait",
        "notify", "_send", "_next_request_id", "_read_stdout", "_get_message",
        "_process_exited", "_last_chance_message_after_process_exit",
        "_next_progress_seq", "_with_progress_schema", "_handle_client_message",
        "run_turn_with_progress", "is_healthy",
    ),
    "devin_acp": (
        "start", "stop", "steer", "interrupt", "request", "send_request_no_wait",
        "notify", "_send", "_next_request_id", "_read_stdout", "_get_message",
        "_process_exited", "_last_chance_message_after_process_exit",
        "_next_progress_seq", "_with_progress_schema", "_handle_client_message",
        "run_turn_with_progress", "is_healthy", "_respond_to_client_request",
    ),
    "cline_acp": (
        "start", "stop", "steer", "interrupt", "request", "send_request_no_wait",
        "notify", "_send", "_next_request_id", "_read_stdout", "_get_message",
        "_process_exited", "_last_chance_message_after_process_exit",
        "_next_progress_seq", "_with_progress_schema", "_handle_client_message",
        "run_turn_with_progress", "is_healthy", "_respond_to_client_request",
    ),
    # The generic ACP client (base of omp/opencode/kimi-code/mini-agent/dsh)
    # keeps its own prompt loop, request() (the _await_or_detect_death shape)
    # and stdout reader; the transport below is shared.
    "generic_acp": (
        "start", "stop", "steer", "interrupt", "send_request_no_wait", "notify",
        "_send", "_next_request_id", "_get_message", "_next_progress_seq",
        "_with_progress_schema", "_handle_client_message",
    ),
    # gemini_acp is now a deprecated shim over generic_acp and must stay thin —
    # it may not re-implement the turn loop either. tests/test_generic_acp_shim.py
    # pins the rest of that contract.
    "gemini_acp": (
        "start", "stop", "steer", "interrupt", "send_request_no_wait", "notify",
        "_send", "_next_request_id", "_get_message", "_next_progress_seq",
        "_with_progress_schema", "_handle_client_message",
        "run_turn_with_progress", "request", "_read_stdout",
        "_respond_to_client_request", "start_session",
    ),
    # grok keeps its own prompt loop (affirmative health, session rotation,
    # deny-budget grace drain) and interrupt(); the transport below is shared.
    "grok_acp": (
        "start", "stop", "steer", "request", "send_request_no_wait", "notify",
        "_send", "_next_request_id", "_read_stdout", "_get_message",
        "_process_exited", "_last_chance_message_after_process_exit",
        "_next_progress_seq", "_with_progress_schema", "_handle_client_message",
        "is_healthy",
    ),
}


@pytest.mark.parametrize("module_name", sorted(SHARED_METHODS_BY_ENGINE))
def test_shared_machinery_is_not_reimplemented_per_engine(module_name: str) -> None:
    cls = _engine_classes()[module_name]
    reimplemented = sorted(
        name for name in SHARED_METHODS_BY_ENGINE[module_name] if name in vars(cls)
    )
    assert not reimplemented, (
        f"{cls.__name__} re-defines {reimplemented}, which AcpEngineBase already "
        "owns. Either delete the copy or, if the engine genuinely needs a quirked "
        "version, drop the name from this table and say why in the module."
    )
    # Guard the guard: a table that names nothing the class could ever have had
    # would pass vacuously.
    inherited = [
        name for name in SHARED_METHODS_BY_ENGINE[module_name] if hasattr(cls, name)
    ]
    assert inherited == list(SHARED_METHODS_BY_ENGINE[module_name]), (
        f"{cls.__name__} is missing shared names entirely: "
        f"{sorted(set(SHARED_METHODS_BY_ENGINE[module_name]) - set(inherited))}"
    )


# --------------------------------------------------------------------------
# 3: the base's own turn loop, driven through a fake ACP child
# --------------------------------------------------------------------------

class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = iter([])
        self.stderr = iter([])
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True


def _normalize(update: dict[str, Any], tool_titles: dict[str, str] | None = None):
    """The smallest ACP normaliser that exercises the base's event plumbing."""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        return "model_text", {"delta": update["content"]["text"]}
    if kind in {"tool_call", "tool_call_update"}:
        status = update.get("status")
        data = {
            "command": update.get("title"),
            "status": status,
            "exit_code": 0 if status == "completed" else None,
            "tool_call_id": update.get("toolCallId"),
        }
        if status in {"pending", "in_progress"}:
            return "command_started", data
        if status == "completed":
            return "command_finished", data
    return None


class FakeAcpEngine(AcpEngineBase):
    """A minimal concrete engine: only the per-adapter hooks, no transport."""

    engine_label = "fake"
    display_name = "Fake"
    default_command = "fake-acp"

    def command_args(self) -> list[str]:
        return [self.command, "acp"]

    def _start_handshake(self) -> None:  # pragma: no cover - start() unused here
        self._initialize()

    def set_session_mode_for_policy(self, policy: str) -> None:
        self.request(
            "session/set_mode",
            {"sessionId": self.session_id, "modeId": policy},
            timeout=15,
            allow_empty_result=True,
        )

    def _normalize_session_update(self, update, tool_titles):
        return _normalize(update, tool_titles)

    def _respond_to_client_request(self, message: dict[str, Any]) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"outcome": {"outcome": "selected", "optionId": "allow"}},
            }
        )


def _update(session_id: str, update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def _engine() -> tuple[FakeAcpEngine, FakeProcess]:
    fake = FakeProcess()
    engine = FakeAcpEngine(cwd="/tmp/fake", model=None, popen_factory=lambda *a, **k: fake)
    engine.process = fake
    engine.messages = queue.Queue()
    engine.session_id = "sess-fake"
    return engine, fake


def test_base_turn_loop_produces_the_expected_event_sequence() -> None:
    engine, fake = _engine()
    # id 1 = session/set_mode ack, id 2 = the prompt.
    engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
    engine.messages.put(
        _update("sess-fake", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}})
    )
    engine.messages.put(
        _update("sess-fake", {"sessionUpdate": "tool_call", "toolCallId": "tc-1", "title": "ls", "status": "pending"})
    )
    # A second start for the same tool call must NOT surface twice.
    engine.messages.put(
        _update("sess-fake", {"sessionUpdate": "tool_call", "toolCallId": "tc-1", "title": "ls", "status": "in_progress"})
    )
    engine.messages.put(
        _update("sess-fake", {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "completed"})
    )
    engine.messages.put(
        _update("sess-fake", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world"}})
    )
    engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn", "usage": {"totalTokens": 7}}})

    events: list[tuple[str, dict[str, Any]]] = []
    result = engine.run_turn_with_progress(
        "say hi", timeout=10, policy="trusted", on_event=lambda n, d: events.append((n, d))
    )

    assert result.ok
    assert result.result == "Hello world"
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == 1
    assert [name for name, _ in events] == [
        "turn_started",
        "model_text",
        "command_started",
        "command_finished",
        "model_text",
        "turn_completed",
    ]
    # The progress schema is injected by the base for every mid-turn event.
    text = events[1][1]
    assert text["turn_id"] == "2"
    assert text["item_id"] == "2:text"
    assert text["kind"] == "model_text"
    assert isinstance(text["seq"], int)
    assert events[3][1]["item_id"] == "tc-1"
    seqs = [d["seq"] for name, d in events if name not in {"turn_started", "turn_completed"}]
    assert seqs == sorted(seqs)
    assert events[-1][1] == {
        "turn_id": "2",
        "ok": True,
        "stop_reason": "end_turn",
        "usage": {"totalTokens": 7},
    }
    # set_mode then prompt, in that order, on the wire.
    sent = [json.loads(line) for line in fake.stdin.lines]
    assert [m["method"] for m in sent] == ["session/set_mode", "session/prompt"]
    assert sent[1]["params"]["prompt"] == [{"type": "text", "text": "say hi"}]


def test_base_turn_loop_answers_a_mid_turn_client_request() -> None:
    engine, fake = _engine()
    engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
    # Id collision on purpose: the agent's own request carries the prompt id.
    engine.messages.put(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/request_permission",
            "params": {"sessionId": "sess-fake", "options": [{"optionId": "allow", "kind": "allow_once"}]},
        }
    )
    engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

    result = engine.run_turn_with_progress("do work", timeout=10, policy="trusted", on_event=None)

    assert result.ok, result.error
    answered = [
        json.loads(line)
        for line in fake.stdin.lines
        if json.loads(line).get("id") == 2 and "result" in json.loads(line)
    ]
    assert answered and answered[0]["result"]["outcome"]["optionId"] == "allow"


def test_base_turn_loop_reports_a_dead_child_instead_of_burning_the_timeout() -> None:
    engine, fake = _engine()
    engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
    fake.returncode = 1

    result = engine.run_turn_with_progress("do work", timeout=30, policy="trusted", on_event=None)

    assert not result.ok
    assert "fake process exited unexpectedly" == result.error


def test_base_turn_loop_timeout_cancels_the_prompt_and_marks_unhealthy() -> None:
    engine, fake = _engine()
    engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})

    result = engine.run_turn_with_progress("do work", timeout=0, policy="trusted", on_event=None)

    assert not result.ok
    assert "timed out after 0s" in (result.error or "")
    assert engine.healthy is False
    sent = [json.loads(line) for line in fake.stdin.lines]
    assert {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "sess-fake"}} in sent


# --------------------------------------------------------------------------
# is_healthy membership — EXACT, and proven at the surface that consumes it
#
# The shared-method table above only checks that listed names are absent from a
# class's own __dict__ and present somewhere on its MRO. That cannot see a name
# being ADDED where it does not belong: hoisting `is_healthy` onto
# AcpEngineBase leaves every structural case green while silently opting the
# generic-acp family into pool-side health quarantine (engine_pool.py:164-166),
# which is precisely the change _acp_base's layering exists to prevent.
# Codex found this false-green on 2026-08-29 by running that mutation.
# --------------------------------------------------------------------------

def _all_acp_engine_classes() -> dict[str, type]:
    """Every ACP engine class in the tree, keyed by class name."""
    from agent_redis_bridge.engines._acp_base import (
        DenyBudgetAcpEngine,
        HealthReportingAcpEngine,
    )
    from agent_redis_bridge.engines.dsh_acp import DshAcpEngine
    from agent_redis_bridge.engines.generic_acp import GenericAcpEngine
    from agent_redis_bridge.engines.kimi_code_acp import KimiCodeAcpEngine
    from agent_redis_bridge.engines.mini_agent_acp import MiniAgentAcpEngine
    from agent_redis_bridge.engines.omp_acp import OmpAcpEngine
    from agent_redis_bridge.engines.opencode_acp import OpencodeAcpEngine

    classes = {cls.__name__: cls for cls in _engine_classes().values()}
    classes.update(
        {
            cls.__name__: cls
            for cls in (
                AcpEngineBase,
                HealthReportingAcpEngine,
                DenyBudgetAcpEngine,
                GenericAcpEngine,
                OmpAcpEngine,
                OpencodeAcpEngine,
                KimiCodeAcpEngine,
                MiniAgentAcpEngine,
                DshAcpEngine,
            )
        }
    )
    return classes


# The ONLY engines that may answer engine_pool's health probe.
HEALTH_REPORTING = {
    "HealthReportingAcpEngine",
    "DenyBudgetAcpEngine",
    "CursorAcpEngine",
    "DevinAcpEngine",
    "ClineAcpEngine",
    "GrokAcpEngine",
}

# The generic-acp family, which must NOT expose it — including the deprecated
# gemini shim, which is one of them now.
GENERIC_FAMILY = {
    "AcpEngineBase",
    "GenericAcpEngine",
    "GeminiAcpEngine",
    "OmpAcpEngine",
    "OpencodeAcpEngine",
    "KimiCodeAcpEngine",
    "MiniAgentAcpEngine",
    "DshAcpEngine",
}


def test_is_healthy_membership_is_exact() -> None:
    classes = _all_acp_engine_classes()
    # Guard the guard: both sets must actually name classes that exist, or the
    # membership assertion below is comparing against nothing.
    missing = sorted((HEALTH_REPORTING | GENERIC_FAMILY) - set(classes))
    assert not missing, f"membership sets name classes that do not exist: {missing}"
    assert set(classes) == HEALTH_REPORTING | GENERIC_FAMILY, (
        "an ACP engine class is in neither membership set: "
        f"{sorted(set(classes) ^ (HEALTH_REPORTING | GENERIC_FAMILY))}"
    )

    exposing = {name for name, cls in classes.items() if hasattr(cls, "is_healthy")}
    assert exposing == HEALTH_REPORTING, (
        "is_healthy membership changed. engine_pool.release consults the "
        "predicate ONLY when the engine defines it (engine_pool.py:164-166), so "
        "adding it to a generic-acp engine silently changes how that seat is "
        "recycled, and removing it from a policy-driving one silently stops "
        "quarantining a deaf engine.\n"
        f"  unexpectedly exposing: {sorted(exposing - HEALTH_REPORTING)}\n"
        f"  unexpectedly missing:  {sorted(HEALTH_REPORTING - exposing)}"
    )


class _HealthReportingFake(HealthReportingAcpEngine):
    engine_label = "fake-health"
    display_name = "fake-health"
    default_command = "fake"

    def command_args(self) -> list[str]:
        return [self.command]

    def _start_handshake(self) -> None:
        pass

    def set_session_mode_for_policy(self, policy: str) -> None:
        pass

    def _normalize_session_update(self, update, tool_titles):
        return None


def _generic_fake_cls():
    from agent_redis_bridge.engines.generic_acp import GenericAcpEngine

    class _GenericFake(GenericAcpEngine):
        engine_label = "fake-generic"
        display_name = "fake-generic"
        default_command = "fake"

        def command_args(self) -> list[str]:
            return [self.command]

        def _start_handshake(self) -> None:
            pass

        def set_session_mode_for_policy(self, policy: str) -> None:
            pass

    return _GenericFake


def _pooled(engine):
    """Put a pre-wired engine through EnginePool.acquire, as the daemon does."""
    from agent_redis_bridge.engine_pool import EnginePool

    engine.start = lambda: None  # acquire() start()s factory-fresh engines
    engine.reader_thread = None  # no real reader in fixtures
    pool = EnginePool(lambda: engine, max_size=1)
    assert pool.acquire("task-1") is engine
    return pool


def _dead_engine(cls):
    fake = FakeProcess()
    fake.returncode = 1  # child gone: is_healthy() must be False for anyone who has it
    engine = cls(cwd="/tmp/fake", model=None, popen_factory=lambda *a, **k: fake)
    engine.process = fake
    engine.healthy = False
    return engine, fake


def test_pool_quarantines_a_health_reporting_engine() -> None:
    engine, fake = _dead_engine(_HealthReportingFake)
    assert engine.is_healthy() is False
    pool = _pooled(engine)

    pool.release("task-1")

    assert engine not in pool._idle, "a dead health-reporting engine was re-idled"
    assert fake.terminated, "stop() was not called on the quarantined engine"


def test_pool_cannot_quarantine_a_generic_engine_because_it_reports_no_health() -> None:
    """The other half of the membership claim, at the surface that consumes it.

    This is characterisation, not endorsement: the generic-acp family has never
    declared is_healthy, so engine_pool's probe does not fire and an equally
    dead engine is re-idled. Hoisting is_healthy onto the shared base would
    change exactly this, which is why it is pinned here rather than left to the
    structural table.
    """
    cls = _generic_fake_cls()
    engine, fake = _dead_engine(cls)
    assert not hasattr(engine, "is_healthy")
    pool = _pooled(engine)

    pool.release("task-1")

    assert engine in pool._idle, "the generic engine's recycling changed"
    assert not fake.terminated, "stop() was called on an engine the pool cannot judge"
