"""Warm orchestrator over `codex app-server` — the second runtime.

Same warm-orch shape as WarmOrchRunner, different vendor: warmth is a durable
property of the CHANNEL, not of process uptime. Claude persists a session id and
passes it as `resume`; codex persists a THREAD id and calls a different method
(`thread/resume` instead of `thread/start`), so these tests assert on which
method went out, not on a config field.

Protocol verified from `codex app-server generate-json-schema` (codex-cli
0.146.0), not from adapter prose — the method names are slash-delimited
(`thread/start`, `turn/start`), which reading the TypeScript adapter's function
names would have got wrong.

The transport is injected, so these tests drive the real JSON-RPC exchange
against a scripted peer — no subprocess, no network. One test per named
behavior, red-first.
"""
from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from arb_warm_orch.codex_approvals import ApproveNonGated, CodexApprovalPolicy
from arb_warm_orch.codex_runner import (
    CodexAppServerRunner,
    CodexGateUnreachable,
    CodexOrchConfig,
    CodexTurnFailed,
)
from arb_warm_orch.gates import EvidenceCheck
from arb_warm_orch.turn_events import ToolCallCompleted, ToolCallStarted


@dataclass
class ScriptedTransport:
    """A codex peer whose replies are computed from the requests it receives.

    Replies are queued per inbound request so the runner's real request ids are
    echoed back — an id-matching bug cannot hide behind a hardcoded id.
    """

    thread_id: str = "th-1"
    turn_id: str = "turn-1"
    deltas: tuple[str, ...] = ("warm ", "codex reply")
    sent: list[dict] = field(default_factory=list)
    _outbox: list[dict] = field(default_factory=list)
    closed: bool = False
    # Extra messages to interleave before turn/completed, e.g. approvals.
    inject_before_completion: tuple[dict, ...] = ()
    # Extra messages BEFORE any delta. Position matters: a stray `turn/completed`
    # injected after the deltas cannot detect a missing turn-id filter, because
    # the reply text is already complete by then and an early return looks
    # identical to a correct one. Injecting first makes the bug observable.
    inject_before_deltas: tuple[dict, ...] = ()
    # Messages queued AHEAD of our own turn/start response. Without this slot the
    # scripted peer always answered first, so `_request` never had to handle an
    # unsolicited message and that guard survived mutation unnoticed.
    inject_before_response: tuple[dict, ...] = ()
    turn_status: str = "completed"

    def send(self, message: dict) -> None:
        self.sent.append(message)
        if "method" not in message:  # a response from the runner; nothing to do
            return
        method = message["method"]
        if method == "initialize":
            self._outbox.append({"id": message["id"], "result": {}})
        elif method in ("thread/start", "thread/resume"):
            self._outbox.append(
                {"id": message["id"], "result": {"thread": {"id": self.thread_id}}}
            )
        elif method == "turn/start":
            self._outbox.extend(self.inject_before_response)
            self._outbox.append(
                {"id": message["id"], "result": {"turn": {"id": self.turn_id}}}
            )
            self._outbox.extend(self.inject_before_deltas)
            for delta in self.deltas:
                self._outbox.append(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "delta": delta,
                            "itemId": "i1",
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                        },
                    }
                )
            self._outbox.extend(self.inject_before_completion)
            self._outbox.append(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.thread_id,
                        "turn": {"id": self.turn_id, "status": self.turn_status, "items": []},
                    },
                }
            )

    def receive(self) -> dict:
        if not self._outbox:
            raise AssertionError("runner read past the end of the script")
        return self._outbox.pop(0)

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------ helpers

    def requests(self, method: str) -> list[dict]:
        return [m for m in self.sent if m.get("method") == method]

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]


@dataclass
class TransportFactory:
    transports: list[ScriptedTransport] = field(default_factory=list)
    template: ScriptedTransport | None = None

    def __call__(self) -> ScriptedTransport:
        transport = ScriptedTransport(
            inject_before_completion=(
                self.template.inject_before_completion if self.template else ()
            ),
            inject_before_deltas=(
                self.template.inject_before_deltas if self.template else ()
            ),
            inject_before_response=(
                self.template.inject_before_response if self.template else ()
            ),
            turn_status=(self.template.turn_status if self.template else "completed"),
        )
        self.transports.append(transport)
        return transport


@dataclass
class AlwaysResolvable:
    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=True)


@dataclass
class NeverResolvable:
    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=False, detail="no close record")


def _runner(
    tmp_path: Path,
    channel: str = "codex-channel",
    evidence=None,
    inject: tuple[dict, ...] = (),
    inject_first: tuple[dict, ...] = (),
) -> tuple[CodexAppServerRunner, TransportFactory, CodexApprovalPolicy]:
    factory = TransportFactory()
    factory.template = ScriptedTransport(
        inject_before_completion=inject, inject_before_deltas=inject_first
    )
    policy = CodexApprovalPolicy(
        evidence=evidence or AlwaysResolvable(), base_policy=ApproveNonGated()
    )
    runner = CodexAppServerRunner(
        CodexOrchConfig(
            channel=channel,
            cwd=str(tmp_path),
            session_root=tmp_path / "sessions",
            model="gpt-5-codex",
            approval_policy_mode="untrusted",
        ),
        approval_policy=policy,
        transport_factory=factory,
    )
    return runner, factory, policy


def _pinned_runner(
    tmp_path: Path, effort: str | None
) -> tuple[CodexAppServerRunner, TransportFactory, CodexApprovalPolicy]:
    """Same wiring as `_runner`, with a seat-level reasoning-effort pin."""
    factory = TransportFactory()
    factory.template = ScriptedTransport()
    policy = CodexApprovalPolicy(
        evidence=AlwaysResolvable(), base_policy=ApproveNonGated()
    )
    runner = CodexAppServerRunner(
        CodexOrchConfig(
            channel="codex-channel",
            cwd=str(tmp_path),
            session_root=tmp_path / "sessions",
            model="gpt-5-codex",
            approval_policy_mode="untrusted",
            effort=effort,
        ),
        approval_policy=policy,
        transport_factory=factory,
    )
    return runner, factory, policy


# ------------------------------------------------------------- handshake


def test_initialize_is_the_first_request_sent(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    assert factory.transports[0].methods()[0] == "initialize"


def test_initialize_declares_client_info(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("initialize")[0]["params"]
    assert params["clientInfo"]["name"]
    assert params["clientInfo"]["version"]


def test_initialized_notification_follows_the_initialize_response(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    methods = factory.transports[0].methods()
    assert methods[1] == "initialized"
    assert "id" not in factory.transports[0].requests("initialized")[0]


# ------------------------------------------------------------ thread warmth


def test_fresh_channel_starts_a_thread(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    assert factory.transports[0].requests("thread/start")
    assert not factory.transports[0].requests("thread/resume")


def test_thread_start_carries_cwd_and_model(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("thread/start")[0]["params"]
    assert params["cwd"] == str(tmp_path)
    assert params["model"] == "gpt-5-codex"


def test_thread_start_carries_approval_policy_and_sandbox_when_configured(tmp_path):
    # Load-bearing for the gate, not cosmetic: with codex's default policy an
    # exec may be auto-approved, so no approval request reaches us and the gate
    # would "pass" having never been consulted. Asking for `untrusted` is what
    # makes the refusal path reachable.
    factory = TransportFactory()
    factory.template = ScriptedTransport()
    runner = CodexAppServerRunner(
        CodexOrchConfig(
            channel="c",
            cwd=str(tmp_path),
            session_root=tmp_path / "sessions",
            approval_policy_mode="untrusted",
            sandbox_mode="read-only",
        ),
        approval_policy=CodexApprovalPolicy(
            evidence=AlwaysResolvable(), base_policy=ApproveNonGated()
        ),
        transport_factory=factory,
    )
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("thread/start")[0]["params"]
    assert params["approvalPolicy"] == "untrusted"
    assert params["sandbox"] == "read-only"


def test_thread_start_omits_sandbox_when_not_configured(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("thread/start")[0]["params"]
    assert "sandbox" not in params


def test_thread_start_always_carries_a_gate_reachable_approval_policy(tmp_path):
    # Panel finding (codex F1 / grok F2 / cold-Opus P1-1): the previous version
    # of this test pinned the UNSAFE default as correct — with no approvalPolicy
    # sent, codex may auto-approve an exec, no approval request reaches the
    # policy layer, and the merge/close gate is never consulted. The danger was
    # documented in three places and mitigated in none.
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("thread/start")[0]["params"]
    assert params["approvalPolicy"] == "untrusted"


def test_resume_carries_approval_policy_too(tmp_path):
    # thread/resume accepts the same fields; a policy that applied only on
    # first contact would silently lapse on every warm resume — i.e. exactly
    # the long-lived channels the orchestrator is built around.
    def build():
        factory = TransportFactory()
        factory.template = ScriptedTransport()
        return (
            CodexAppServerRunner(
                CodexOrchConfig(
                    channel="c",
                    cwd=str(tmp_path),
                    session_root=tmp_path / "sessions",
                    approval_policy_mode="untrusted",
                ),
                approval_policy=CodexApprovalPolicy(
                    evidence=AlwaysResolvable(), base_policy=ApproveNonGated()
                ),
                transport_factory=factory,
            ),
            factory,
        )

    first, _ = build()
    asyncio.run(first.turn("hello"))
    second, second_factory = build()
    asyncio.run(second.turn("again"))
    params = second_factory.transports[0].requests("thread/resume")[0]["params"]
    assert params["approvalPolicy"] == "untrusted"


def test_thread_id_is_persisted_for_the_channel(tmp_path):
    runner, _, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    persisted = (tmp_path / "sessions" / "codex-channel" / "last-thread-id").read_text()
    assert persisted.strip() == "th-1"


def test_next_process_resumes_the_persisted_thread(tmp_path):
    runner, _, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    fresh_runner, fresh_factory, _ = _runner(tmp_path)
    asyncio.run(fresh_runner.turn("again"))
    resumes = fresh_factory.transports[0].requests("thread/resume")
    assert resumes[0]["params"]["threadId"] == "th-1"
    assert not fresh_factory.transports[0].requests("thread/start")


def test_one_transport_serves_consecutive_turns(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("one"))
    asyncio.run(runner.turn("two"))
    assert len(factory.transports) == 1
    assert len(factory.transports[0].requests("turn/start")) == 2
    assert len(factory.transports[0].requests("initialize")) == 1


# ------------------------------------------------------------------ turns


def test_turn_start_carries_thread_id_and_text_input(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello codex"))
    params = factory.transports[0].requests("turn/start")[0]["params"]
    assert params["threadId"] == "th-1"
    assert params["input"] == [{"type": "text", "text": "hello codex"}]


def test_client_system_prompt_reaches_thread_start_as_developer_instructions(tmp_path):
    """The whole point: acp_server's duck-typed lookup must now find something,
    and what it finds must actually reach codex."""
    runner, factory, _ = _runner(tmp_path)
    assert runner.apply_system_prompt("seat rules") is True
    asyncio.run(runner.turn("hello"))
    params = factory.transports[0].requests("thread/start")[0]["params"]
    assert params["developerInstructions"] == "seat rules"


def test_apply_system_prompt_is_refused_once_connected(tmp_path):
    """Adopting a refreshed prompt mid-session would mean dropping the thread.
    Same call WarmOrchRunner makes: report False, keep the warmth."""
    runner, _, _ = _runner(tmp_path)
    asyncio.run(runner.turn("first"))
    assert runner.apply_system_prompt("too late") is False
    assert runner.config.developer_instructions is None


def test_apply_system_prompt_reports_false_on_a_warm_channel(tmp_path):
    """A persisted thread means we resume, and codex ignores
    developerInstructions on resume (measured on 0.146.0 and 0.147.0-alpha.1.2).
    Claiming True here would be a lie about effect."""
    runner, _, _ = _runner(tmp_path)
    asyncio.run(runner.turn("first"))  # persists the thread id for the channel
    fresh, _, _ = _runner(tmp_path)
    assert fresh.apply_system_prompt("seat rules") is False


def test_warm_channel_still_sends_the_prompt_despite_reporting_false(tmp_path):
    """Forward compatibility: the value must ride thread/resume anyway, so the
    day the fork fix lands it starts working with no change here."""
    runner, _, _ = _runner(tmp_path)
    asyncio.run(runner.turn("first"))
    fresh, factory, _ = _runner(tmp_path)
    fresh.apply_system_prompt("seat rules")
    asyncio.run(fresh.turn("second"))
    params = factory.transports[0].requests("thread/resume")[0]["params"]
    assert params["developerInstructions"] == "seat rules"


def test_turn_start_omits_effort_when_the_seat_pins_none(tmp_path):
    """No pin must mean no param — sending an invented default would silently
    override whatever ~/.codex/config.toml says for every unpinned seat."""
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    assert "effort" not in factory.transports[0].requests("turn/start")[0]["params"]


def test_turn_start_carries_the_pinned_effort(tmp_path):
    runner, factory, _ = _pinned_runner(tmp_path, effort="xhigh")
    asyncio.run(runner.turn("hello"))
    assert factory.transports[0].requests("turn/start")[0]["params"]["effort"] == "xhigh"


def test_every_turn_repeats_the_pin_not_just_the_first(tmp_path):
    """codex sticks the last effort on a warm thread, and this runner resumes a
    persisted thread — so a pin sent once would decay into whatever ran last."""
    runner, factory, _ = _pinned_runner(tmp_path, effort="high")
    asyncio.run(runner.turn("one"))
    asyncio.run(runner.turn("two"))
    sent = factory.transports[0].requests("turn/start")
    assert [request["params"].get("effort") for request in sent] == ["high", "high"]


def test_pinned_effort_is_normalized(tmp_path):
    runner, factory, _ = _pinned_runner(tmp_path, effort="  HIGH  ")
    asyncio.run(runner.turn("hello"))
    assert factory.transports[0].requests("turn/start")[0]["params"]["effort"] == "high"


def test_invalid_effort_is_refused_at_construction_not_mid_turn(tmp_path):
    with pytest.raises(ValueError, match="invalid reasoning effort"):
        _pinned_runner(tmp_path, effort="highest")


def _drain(agen):
    """Collect an async generator synchronously — same helper as the grok
    tests; `turn()` only assembles TextDelta, so tool events need the stream."""
    async def go():
        return [event async for event in agen]
    return asyncio.run(go())


def _item_started(item: dict, turn_id: str = "turn-1") -> dict:
    return {
        "method": "item/started",
        "params": {"threadId": "th-1", "turnId": turn_id,
                   "startedAtMs": 0, "item": item},
    }


def _item_completed(item: dict, turn_id: str = "turn-1") -> dict:
    return {
        "method": "item/completed",
        "params": {"threadId": "th-1", "turnId": turn_id,
                   "completedAtMs": 0, "item": item},
    }


def test_turn_returns_assembled_agent_message_deltas(tmp_path):
    runner, _, _ = _runner(tmp_path)
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


def test_command_execution_items_stream_as_tool_call_events(tmp_path):
    """The gap that made every codex turn report "0 tool calls" to the buzz
    panel and durable transcript: tool use rides item/started+item/completed,
    which the loop previously dropped on the floor."""
    command = {"type": "commandExecution", "id": "item-7",
               "command": "systemctl is-active arb-codex-seat",
               "status": "inProgress"}
    done = dict(command, status="completed")
    runner, _, _ = _runner(
        tmp_path, inject_first=(_item_started(command), _item_completed(done))
    )
    events = _drain(runner.stream_turn("hello"))
    assert ToolCallStarted(
        tool_call_id="item-7",
        title="systemctl is-active arb-codex-seat",
        kind="execute",
    ) in events
    assert ToolCallCompleted(tool_call_id="item-7", status="completed") in events


def test_declined_and_failed_items_both_report_failed(tmp_path):
    """`declined` means the call never ran — presenting it as success would
    make a gate refusal invisible in the transcript."""
    declined = {"type": "commandExecution", "id": "item-8",
                "command": "git merge", "status": "declined"}
    failed = {"type": "mcpToolCall", "id": "item-9", "server": "s",
              "tool": "t", "status": "failed"}
    runner, _, _ = _runner(
        tmp_path, inject_first=(_item_completed(declined), _item_completed(failed))
    )
    events = _drain(runner.stream_turn("hello"))
    assert ToolCallCompleted(tool_call_id="item-8", status="failed") in events
    assert ToolCallCompleted(tool_call_id="item-9", status="failed") in events


def test_tool_items_for_another_turn_are_ignored(tmp_path):
    stray = {"type": "commandExecution", "id": "item-X",
             "command": "echo stray", "status": "inProgress"}
    runner, _, _ = _runner(
        tmp_path, inject_first=(_item_started(stray, turn_id="turn-OTHER"),)
    )
    events = _drain(runner.stream_turn("hello"))
    assert not any(isinstance(e, ToolCallStarted) for e in events)


def test_stream_items_do_not_emit_tool_calls(tmp_path):
    """reasoning/agentMessage items are streams, not tool calls — mapping them
    would double-count every turn's prose as a tool."""
    reasoning = {"type": "reasoning", "id": "item-R", "summary": [], "content": []}
    runner, _, _ = _runner(
        tmp_path,
        inject_first=(_item_started(reasoning), _item_completed(reasoning)),
    )
    events = _drain(runner.stream_turn("hello"))
    assert not any(
        isinstance(e, (ToolCallStarted, ToolCallCompleted)) for e in events
    )


def test_deltas_for_another_turn_are_ignored(tmp_path):
    stray = {
        "method": "item/agentMessage/delta",
        "params": {
            "delta": "STRAY",
            "itemId": "i9",
            "threadId": "th-1",
            "turnId": "turn-OTHER",
        },
    }
    runner, _, _ = _runner(tmp_path, inject=(stray,))
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


def test_completion_of_another_turn_does_not_end_this_one(tmp_path):
    other = {
        "method": "turn/completed",
        "params": {
            "threadId": "th-1",
            "turn": {"id": "turn-OTHER", "status": "completed", "items": []},
        },
    }
    # Injected FIRST on purpose — see ScriptedTransport.inject_before_deltas.
    runner, _, _ = _runner(tmp_path, inject_first=(other,))
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


def test_error_notification_for_this_turn_raises_with_the_server_message(tmp_path):
    err = {
        "method": "error",
        "params": {
            "error": {"message": "model exploded"},
            "threadId": "th-1",
            "turnId": "turn-1",
            "willRetry": False,
        },
    }
    runner, _, _ = _runner(tmp_path, inject=(err,))
    with pytest.raises(CodexTurnFailed) as excinfo:
        asyncio.run(runner.turn("hello"))
    assert "model exploded" in str(excinfo.value)


def test_retryable_error_notification_does_not_abort_the_turn(tmp_path):
    err = {
        "method": "error",
        "params": {
            "error": {"message": "transient"},
            "threadId": "th-1",
            "turnId": "turn-1",
            "willRetry": True,
        },
    }
    runner, _, _ = _runner(tmp_path, inject=(err,))
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


# ------------------------------------------------------- approvals inline


def _approval_request(command: str, request_id: int = 900) -> dict:
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "itemId": "i2",
            "threadId": "th-1",
            "turnId": "turn-1",
            "startedAtMs": 0,
            "command": command,
        },
    }


def _responses(transport: ScriptedTransport) -> list[dict]:
    return [m for m in transport.sent if "method" not in m]


def test_blocking_approval_is_answered_during_the_turn(tmp_path):
    runner, factory, _ = _runner(tmp_path, inject=(_approval_request("ls -la"),))
    asyncio.run(runner.turn("hello"))
    answers = _responses(factory.transports[0])
    assert answers == [{"id": 900, "result": {"decision": "accept"}}]


def test_gated_merge_approval_is_declined_mid_turn(tmp_path):
    runner, factory, policy = _runner(
        tmp_path,
        evidence=NeverResolvable(),
        inject=(_approval_request("git merge dev"),),
    )
    asyncio.run(runner.turn("hello"))
    assert _responses(factory.transports[0])[0]["result"] == {"decision": "decline"}
    assert policy.records[-1].code == "merge-close-evidence-unresolved"


def test_unhandled_server_request_is_answered_with_an_error_not_ignored(tmp_path):
    unhandled = {"id": 901, "method": "item/permissions/requestApproval", "params": {}}
    runner, factory, _ = _runner(tmp_path, inject=(unhandled,))
    asyncio.run(runner.turn("hello"))
    answer = _responses(factory.transports[0])[0]
    assert answer["id"] == 901
    assert "codex-approval-unhandled-method" in answer["error"]["message"]


def test_turn_still_completes_after_answering_an_approval(tmp_path):
    runner, _, _ = _runner(tmp_path, inject=(_approval_request("ls"),))
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


# ---------------------------------------------------------------- teardown


def test_disconnect_closes_the_transport(tmp_path):
    runner, factory, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    runner.disconnect()
    assert factory.transports[0].closed is True


# ------------------- panel remediation: gate reachability + untested guards


def test_approval_policy_that_cannot_reach_the_gate_is_refused(tmp_path):
    # `never` means codex never asks, so the gate is decorative. Refuse it
    # rather than run a cockpit whose control cannot fire.
    factory = TransportFactory()
    factory.template = ScriptedTransport()
    with pytest.raises(CodexGateUnreachable) as excinfo:
        CodexAppServerRunner(
            CodexOrchConfig(
                channel="c",
                cwd=str(tmp_path),
                session_root=tmp_path / "s",
                approval_policy_mode="never",
            ),
            approval_policy=CodexApprovalPolicy(
                evidence=AlwaysResolvable(), base_policy=ApproveNonGated()
            ),
            transport_factory=factory,
        )
    assert "codex-approval-policy-cannot-reach-gate" in str(excinfo.value)


def test_error_for_another_turn_does_not_abort_this_one(tmp_path):
    # Panel finding (cold-Opus P2-3a), CONFIRMED by mutation before remediation:
    # deleting the turn-id check on the error branch left all 115 tests green,
    # because every scripted error carried this runner's own turn id. The delta
    # and completion branches had negative tests; this one did not.
    stray_error = {
        "method": "error",
        "params": {
            "error": {"message": "someone else's turn exploded"},
            "threadId": "th-1",
            "turnId": "turn-OTHER",
            "willRetry": False,
        },
    }
    runner, _, _ = _runner(tmp_path, inject_first=(stray_error,))
    assert asyncio.run(runner.turn("hello")) == "warm codex reply"


def test_blocking_request_arriving_before_our_response_is_answered(tmp_path):
    # Panel finding (cold-Opus P2-3b), CONFIRMED by mutation: gutting
    # _handle_unsolicited left all tests green, because the scripted peer always
    # queued our response FIRST, so _request never saw anything ahead of it.
    # Queue the approval BEFORE the turn/start response and the guard becomes
    # observable — a stalled harness is the real-world cost of missing it.
    factory = TransportFactory()
    factory.template = ScriptedTransport(
        inject_before_response=(_approval_request("ls -la", request_id=910),)
    )
    policy = CodexApprovalPolicy(
        evidence=AlwaysResolvable(), base_policy=ApproveNonGated()
    )
    runner = CodexAppServerRunner(
        CodexOrchConfig(
            channel="c", cwd=str(tmp_path), session_root=tmp_path / "s",
            approval_policy_mode="untrusted",
        ),
        approval_policy=policy,
        transport_factory=factory,
    )
    asyncio.run(runner.turn("hello"))
    assert {"id": 910, "result": {"decision": "accept"}} in _responses(factory.transports[0])


def test_failed_turn_status_raises_rather_than_returning_a_partial_reply(tmp_path):
    # Panel finding (grok F4): TurnStatus includes `failed`; treating any
    # turn/completed as success returns a partial string as if it were a
    # finished answer.
    runner, factory, _ = _runner(tmp_path)
    factory.template.turn_status = "failed"
    with pytest.raises(CodexTurnFailed) as excinfo:
        asyncio.run(runner.turn("hello"))
    assert "codex-turn-not-completed" in str(excinfo.value)
