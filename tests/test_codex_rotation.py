"""ENG-1 rotation unit tests (design v1.2, G1)."""
import logging
import os
import queue
from typing import Any

import pytest

from agent_redis_bridge.engines.codex import AppServerError, CodexEngine


def _with_env(env, factory):
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return factory()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_CODEX_RETIRE_AFTER_TURN": retire, "BRIDGE_CODEX_MAX_PROCESS_TURNS": cap},
        lambda: CodexEngine(cwd="/tmp", model=None, approval_policy="never", sandbox="read-only"),
    )


# --- Task 1: property / counters / declarations -------------------------------
# NOTE: the first two tests and test_cap_zero_means_unlimited PASS before the
# Task-1 edit (regression pins on existing env-flag semantics); the other three
# FAIL before it. Report the observed split.


def test_construction_retire_default_on():
    eng = make_engine(retire=None)
    assert eng.retire_after_turn is True


def test_construction_retire_opt_out():
    eng = make_engine(retire="0")
    assert eng.retire_after_turn is False


def test_cap_flips_retire_property():
    eng = make_engine(retire="0", cap="3")
    assert eng.retire_after_turn is False
    eng._process_turns = 3
    assert eng.retire_after_turn is True   # D9: capped engine retires itself at release


def test_cap_zero_means_unlimited():
    eng = make_engine(retire="0", cap="0")
    eng._process_turns = 10_000
    assert eng.retire_after_turn is False


def test_supports_continuation_tripwire():
    # D10: enabling continuation without resetting _thread_turns per
    # drive_to_completion attempt would rotate mid-dispatch and destroy the
    # dispatch's own context. Do NOT flip this without the D10 enable-path work.
    assert CodexEngine.supports_continuation is False


def test_counters_initialized():
    eng = make_engine()
    assert eng._thread_turns == 0 and eng._process_turns == 0 and eng._interrupted is False


# --- Task 2: D7 affirmative health --------------------------------------------


class RotationEngine(CodexEngine):
    """Self-contained fake: scripted request() + message queue, no subprocess.
    Mirrors tests/test_codex_io.py's FakeCodexEngine, plus: turn/start enqueues
    its own terminal (configurable status), send_request_no_wait is stubbed so
    interrupt() works without a process, and thread/turn failures are scriptable."""

    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp", model="gpt-5.5", approval_policy="never", sandbox="workspace-write"
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.thread_counter = 0
        self.turn_counter = 0
        self.thread_start_calls = 0
        self.sent_turn_threads: list[str] = []
        self.sent_no_wait: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_thread_start = False
        self.fail_next_turn_start = False
        self.terminal_status: str | None = "completed"

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "thread/start":
            if self.fail_next_thread_start:
                self.fail_next_thread_start = False
                raise AppServerError("scripted thread/start failure")
            self.thread_start_calls += 1
            self.thread_counter += 1
            return {"thread": {"id": f"thread-{self.thread_counter}"}}
        if method == "thread/resume":
            return {}
        if method == "thread/fork":
            return {"thread": {"id": "thread-child"}}
        if method == "turn/start":
            if self.fail_next_turn_start:
                self.fail_next_turn_start = False
                raise AppServerError("turn/start timed out after 30s")
            self.sent_turn_threads.append(params["threadId"])
            self.turn_counter += 1
            turn_id = f"turn-{self.turn_counter}"
            turn: dict[str, Any] = {"id": turn_id}
            if self.terminal_status is not None:
                turn["status"] = self.terminal_status
            self.messages.put(
                {"method": "turn/completed", "params": {"turnId": turn_id, "turn": turn}}
            )
            return {"turn": {"id": turn_id}}
        return {}

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        self.sent_no_wait.append((method, params))
        return 999

    def _get_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


def make_rotation_engine(retire=None, cap=None):
    return _with_env(
        {"BRIDGE_CODEX_RETIRE_AFTER_TURN": retire, "BRIDGE_CODEX_MAX_PROCESS_TURNS": cap},
        RotationEngine,
    )


def _run(eng, on_event=None):
    return eng.run_turn_with_progress("hi", timeout=5, policy="trusted", on_event=on_event)


def test_clean_completed_status_reaffirms_healthy():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    result = _run(eng)
    assert result.ok is True
    assert eng.healthy is True   # D7 flag; is_healthy() also ANDs process liveness (CDX-3), which these process-less fakes cannot satisfy


def test_missing_status_quarantines(caplog):
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = None
    with caplog.at_level(logging.WARNING, logger="agent_redis_bridge.engines.codex"):
        result = _run(eng)
    assert result.ok is True            # ok semantics UNCHANGED (blocklist)
    assert eng.healthy is False    # reuse semantics: allowlist
    assert "non-clean terminal status" in caplog.text


def test_unknown_status_quarantines():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = "cancelled"
    result = _run(eng)
    assert eng.healthy is False


def test_interrupted_status_quarantines_but_ok_unchanged():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.terminal_status = "interrupted"
    result = _run(eng)
    assert result.ok is True            # today's ok computation, pinned
    assert eng.healthy is False    # D7: the process is never reused


def test_interrupt_sets_latch():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"
    eng.active_turn_id = "turn-x"
    eng.interrupt()
    assert eng._interrupted is True
    assert eng.sent_no_wait and eng.sent_no_wait[-1][0] == "turn/interrupt"


def test_interrupt_latch_quarantines_even_on_clean_status():
    eng = make_rotation_engine()
    eng.thread_id = "thread-0"

    def on_event(name, data):
        if name == "turn_started":
            eng.interrupt()

    result = _run(eng, on_event=on_event)
    assert eng.healthy is False    # latch wins over the clean status


def test_start_thread_rejects_empty_id():
    eng = make_rotation_engine()
    eng.thread_id = "thread-keep"

    def bad_request(method, params, *, timeout):
        return {"thread": {"id": ""}}

    eng.request = bad_request  # type: ignore[method-assign]
    with pytest.raises(AppServerError):
        eng.start_thread()
    assert eng.thread_id == "thread-keep"


# --- Task 3: rotation ----------------------------------------------------------


def test_rotation_fires_on_second_turn_when_not_retiring():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    assert eng.thread_id == "thread-1"
    _run(eng)
    assert eng.thread_start_calls == 1     # no rotation on the fresh thread
    _run(eng)
    assert eng.thread_start_calls == 2     # rotated
    assert eng.thread_id == "thread-2"
    assert eng.sent_turn_threads == ["thread-1", "thread-2"]


def test_no_rotation_when_retiring():
    eng = make_rotation_engine()
    eng.start_thread()
    _run(eng)
    _run(eng)
    assert eng.thread_start_calls == 1


def test_no_rotation_after_resume():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.resume_thread("t-explicit")
    _run(eng)
    assert eng.thread_start_calls == 1               # rotation must NOT fire
    assert eng.sent_turn_threads[-1] == "t-explicit"  # the resumed thread served


def test_no_rotation_after_fork():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.fork_thread("t-base")
    _run(eng)
    assert eng.thread_start_calls == 1
    assert eng.sent_turn_threads[-1] == "thread-child"


def test_no_rotation_after_reset_context():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.reset_context()                    # start_thread -> thread-2, counter reset
    assert eng.thread_start_calls == 2
    _run(eng)
    assert eng.thread_start_calls == 2     # no ADDITIONAL rotation
    assert eng.sent_turn_threads[-1] == "thread-2"


def test_rotation_failure_quarantines():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    _run(eng)
    eng.fail_next_thread_start = True
    with pytest.raises(AppServerError, match="thread rotation failed"):
        _run(eng)
    assert eng.healthy is False
    assert eng.thread_id == "thread-1"     # no fallback, no partial flip


def test_counter_dirty_before_send():
    eng = make_rotation_engine(retire="0")
    eng.start_thread()
    eng.fail_next_turn_start = True
    with pytest.raises(AppServerError):
        _run(eng)
    assert eng._thread_turns == 1          # R1: attempted == dirty
