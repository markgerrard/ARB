"""Warm-orch runtime 3: grok Build over ACP (warm side).

The COLD counterpart is `engines/grok_acp.py`, which rotates a fresh
`session/new` per dispatch so a worker never re-serves accumulated context
(grok_acp.py:255). This is the opposite polarity: the channel is durable and
its session is resumed, so warmth survives process death.

Capabilities probed from the real `grok agent stdio` binary rather than
assumed (internal orchestration log): `loadSession: true`,
`protocolVersion: 1`, and an `x.ai/hooks` pre_tool_use event carrying a `deny`
decision. The bridge's `supports_thread_resume = False` is a worker-appropriate
choice, NOT an adapter limit.

Transport is injected, so these tests drive the real JSON-RPC exchange against
a scripted peer — same seam the codex runner uses.
"""
from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

import json

from arb_warm_orch.gates import EvidenceCheck
from arb_warm_orch.grok_runner import GrokAcpRunner, GrokOrchConfig
from arb_warm_orch.turn_events import (
    ReasoningDelta,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)


def _drain(agen):
    """Collect an async generator synchronously.

    Every runtime streams asynchronously since the panel found `--acp` worked
    with only one of the four (a sync generator raises under `async for`).
    """
    async def go():
        return [event async for event in agen]
    return asyncio.run(go())

class _AlwaysResolvable:
    """Warmth tests are not gate tests; the gate has its own section below."""

    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=True)


class ScriptedTransport:
    """A peer that answers requests from a scripted table.

    Records every message sent so tests assert on the WIRE, not on internal
    state — the same reason the codex runner's tests drive a transport.
    """

    def __init__(
        self,
        responses: dict[str, dict] | None = None,
        notifications_on: dict[str, list[dict]] | None = None,
    ):
        self.sent: list[dict] = []
        self.responses = responses or {}
        # Notifications are keyed by the method that triggers them, so they
        # arrive DURING that exchange. Pre-seeding the queue instead would let
        # an earlier request's read loop consume them first.
        self.notifications_on = notifications_on or {}
        self.pending: list[dict] = []
        self.closed = False

    def send(self, message: dict) -> None:
        self.sent.append(message)
        if "result" in message or "error" in message:
            return  # the client ANSWERING us (e.g. a permission verdict)
        if "id" not in message:
            return  # a notification expects no reply
        method = message["method"]
        self.pending.extend(self.notifications_on.get(method, []))
        result = self.responses.get(method, {})
        self.pending.append({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def receive(self) -> dict:
        if not self.pending:
            raise AssertionError("scripted transport exhausted — nothing left to receive")
        return self.pending.pop(0)

    def close(self) -> None:
        self.closed = True

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]


def _runner(tmp_path: Path, transport: ScriptedTransport, channel: str = "grok-chan"):
    return GrokAcpRunner(
        GrokOrchConfig(
            channel=channel,
            cwd=str(tmp_path),
            session_root=tmp_path / "sessions",
        ),
        transport_factory=lambda: transport,
        evidence_resolver=_AlwaysResolvable(),
    )


def _default_transport() -> ScriptedTransport:
    return ScriptedTransport(
        responses={
            "initialize": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
            "session/new": {"sessionId": "grok-sess-1"},
            "session/load": {},
        }
    )


# ------------------------------------------------------------------ warmth


def test_first_connect_creates_a_session(tmp_path):
    transport = _default_transport()
    runner = _runner(tmp_path, transport)
    runner.connect()
    assert "session/new" in transport.methods()
    assert "session/load" not in transport.methods()


def test_the_channels_session_id_is_persisted(tmp_path):
    transport = _default_transport()
    runner = _runner(tmp_path, transport)
    runner.connect()
    persisted = (tmp_path / "sessions" / "grok-chan" / "last-session-id").read_text()
    assert persisted.strip() == "grok-sess-1"


def test_a_later_process_resumes_the_channel_rather_than_starting_fresh(tmp_path):
    """The property the warm orchestrator exists for.

    grok advertises `loadSession: true` (probed live), so a second process on
    the same channel must LOAD the persisted session, not mint a new one —
    otherwise warmth dies with the process and this is just the cold engine
    with extra steps.
    """
    first = _default_transport()
    _runner(tmp_path, first).connect()

    second = _default_transport()
    _runner(tmp_path, second).connect()

    assert "session/load" in second.methods()
    assert "session/new" not in second.methods()
    load = next(m for m in second.sent if m.get("method") == "session/load")
    assert load["params"]["sessionId"] == "grok-sess-1"


def test_resume_is_refused_when_the_agent_cannot_load_sessions(tmp_path):
    """Fail loudly rather than silently going cold.

    If an adapter does not advertise `loadSession`, resuming is impossible —
    and quietly falling back to `session/new` would present a COLD session as a
    warm one, which is exactly the failure the channel abstraction is meant to
    make impossible.
    """
    first = _default_transport()
    _runner(tmp_path, first).connect()

    no_load = ScriptedTransport(
        responses={
            "initialize": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}},
            "session/new": {"sessionId": "grok-sess-2"},
        }
    )
    with pytest.raises(Exception) as excinfo:
        _runner(tmp_path, no_load).connect()
    assert "grok-resume-unsupported" in str(excinfo.value)


# ------------------------------------------------------------------- turns


def test_turn_streams_agent_message_chunks_as_text_deltas(tmp_path):
    """Same runtime-neutral vocabulary as the other runtimes (entry 37), so
    the ACP server can drive this one without knowing which it is."""
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "grok-sess-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "grok reply"},
                },
            },
        },
    ]
    runner = _runner(tmp_path, transport)
    events = _drain(runner.stream_turn("hello"))
    assert TextDelta(text="grok reply") in events


def _session_update(update: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "grok-sess-1", "update": update},
    }


def test_turn_streams_thought_chunks_as_reasoning_deltas(tmp_path):
    """Without this the buzz thinking panel is structurally empty for grok
    seats — same gap ReasoningDelta closed for codex."""
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        _session_update(
            {"sessionUpdate": "agent_thought_chunk",
             "content": {"type": "text", "text": "pondering"}}
        ),
    ]
    runner = _runner(tmp_path, transport)
    events = _drain(runner.stream_turn("hello"))
    assert ReasoningDelta(text="pondering") in events


def test_turn_streams_tool_calls_with_terminal_status_only(tmp_path):
    """ACP tool_call/tool_call_update pass through; intermediate updates are
    not part of the runtime-neutral vocabulary and must not surface."""
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        _session_update(
            {"sessionUpdate": "tool_call", "toolCallId": "tc-1",
             "title": "ls -la", "kind": "execute"}
        ),
        _session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1",
             "status": "in_progress"}
        ),
        _session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1",
             "status": "completed"}
        ),
    ]
    runner = _runner(tmp_path, transport)
    events = _drain(runner.stream_turn("hello"))
    assert ToolCallStarted(tool_call_id="tc-1", title="ls -la", kind="execute") in events
    assert ToolCallCompleted(tool_call_id="tc-1", status="completed") in events
    # Exactly one terminal event: the in_progress update must not leak.
    assert sum(isinstance(e, ToolCallCompleted) for e in events) == 1


def test_a_failed_tool_call_reports_failed_not_completed(tmp_path):
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        _session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-2",
             "status": "failed"}
        ),
    ]
    runner = _runner(tmp_path, transport)
    events = _drain(runner.stream_turn("hello"))
    assert ToolCallCompleted(tool_call_id="tc-2", status="failed") in events


# -------------------------------------------------------------- the gate
#
# Direction matters and INVERTS against acp_server.py (entry 36). There we
# could not gate on `session/request_permission` because the consumer
# auto-approves it; here we are the CLIENT, so answering it IS the gate.


def _permission_request(command: str, request_id: int = 99) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "grok-sess-1",
            "toolCall": {"title": command, "kind": "execute",
                         "rawInput": {"command": command}},
            "options": [
                {"optionId": "allow-1", "kind": "allow_once", "name": "Allow"},
                {"optionId": "reject-1", "kind": "reject_once", "name": "Reject"},
            ],
        },
    }


def _gated_runner(tmp_path: Path, transport: ScriptedTransport, resolvable: bool):
    class Resolver:
        def resolve(self, tool_name, tool_input):
            return EvidenceCheck(resolvable=resolvable, detail="scripted")

    return GrokAcpRunner(
        GrokOrchConfig(channel="grok-chan", cwd=str(tmp_path),
                       session_root=tmp_path / "sessions"),
        transport_factory=lambda: transport,
        evidence_resolver=Resolver(),
    )


def test_an_unresolved_merge_is_refused_with_its_specific_code(tmp_path):
    """Assert the CODE, never a bare refusal.

    On a default-deny path a bare rejection proves nothing — deleting the gate
    would let some other layer refuse and the test would stay green.
    """
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        _permission_request("git -C /repo merge topic")
    ]
    runner = _gated_runner(tmp_path, transport, resolvable=False)
    _drain(runner.stream_turn("merge it"))

    answers = [m for m in transport.sent if m.get("id") == 99 and "result" in m]
    assert answers, "the gate must answer the permission request"
    outcome = answers[0]["result"]["outcome"]
    assert outcome["outcome"] == "selected"
    assert outcome["optionId"] == "reject-1"
    assert "merge-close-evidence-unresolved" in json.dumps(answers[0])


def test_a_merge_with_resolvable_evidence_is_allowed(tmp_path):
    """The other direction — proving the refusal above is the gate deciding,
    not the gate refusing everything."""
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    transport.notifications_on["session/prompt"] = [
        _permission_request("git -C /repo merge topic")
    ]
    runner = _gated_runner(tmp_path, transport, resolvable=True)
    _drain(runner.stream_turn("merge it"))

    answers = [m for m in transport.sent if m.get("id") == 99 and "result" in m]
    assert answers[0]["result"]["outcome"]["optionId"] == "allow-1"


def test_the_gate_recognises_the_git_dash_c_spelling(tmp_path):
    """The panel's finding, carried into this runtime: `git -C <path> merge`
    is the ordinary orchestrator idiom and slipped a naive regex. gates.py
    parses the git SUBCOMMAND, and this runtime must route through it rather
    than re-deriving its own matcher."""
    from arb_warm_orch.gates import evaluate_merge_close

    class Never:
        def resolve(self, tool_name, tool_input):
            return EvidenceCheck(resolvable=False, detail="none")

    decision = evaluate_merge_close(
        Never(), "Bash", {"command": "git -C /repo merge topic"}
    )
    assert decision.denied
    assert decision.code == "merge-close-evidence-unresolved"


def test_a_json_rpc_error_on_the_prompt_raises_rather_than_returning_empty(tmp_path):
    """Panel finding (codex-sol P1).

    The loop returned on any matching request id without checking `error`, so a
    rejected turn came back as a successful empty reply — the orchestrator
    would read that as "the seat answered". Identical to the defect the pi 404
    exposed; it was fixed in pi and not carried across until the panel found it.
    """
    transport = _default_transport()

    original_send = transport.send

    def send(message):
        original_send(message)
        if message.get("method") == "session/prompt":
            transport.pending[-1] = {
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32000, "message": "model unavailable"},
            }

    transport.send = send
    runner = _runner(tmp_path, transport)
    with pytest.raises(Exception) as excinfo:
        _drain(runner.stream_turn("go"))
    assert "grok-turn-failed" in str(excinfo.value)


def test_a_successful_empty_turn_is_still_fine(tmp_path):
    """Key on the error field, not on emptiness — an empty successful turn is
    legitimate and must not be turned into a failure."""
    transport = _default_transport()
    transport.responses["session/prompt"] = {"stopReason": "end_turn"}
    assert asyncio.run(_runner(tmp_path, transport).turn("go")) == ""
