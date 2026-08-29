"""Warm-orch runtime 4: pi SDK (warm side).

The COLD counterpart is `engines/pi_sdk.py`, which drives the same
`tools/pi-sdk-host/host.mjs` with an in-memory session so a dispatched worker
accumulates nothing and leaves nothing on disk. This is the opposite polarity:
the channel is durable, its session FILE is persisted, and a later process
reopens it.

Host-side preconditions landed at d6fa0408 (internal orchestration log):
`thread/start` accepts `sessionDir`/`sessionFile` and returns
`thread.sessionFile`, and an opt-in `approvals` param turns on a host->client
`tool/approve` request for bash/write/edit. Both are invisible to the cold
engine, which never sends those params.

Wire shape per host.mjs: `jsonrpc` is deliberately OMITTED to match
engines/codex.py, a turn ENDS at the `turn/completed` notification (the
turn/start response only acknowledges), and notifications carry `turnId`.
"""
from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from arb_warm_orch.gates import EvidenceCheck
from arb_warm_orch.pi_runner import PiOrchConfig, PiSdkRunner
from arb_warm_orch.turn_events import TextDelta, ToolCallCompleted, ToolCallStarted


def _drain(agen):
    """Collect an async generator synchronously.

    Every runtime streams asynchronously since the panel found `--acp` worked
    with only one of the four (a sync generator raises under `async for`).
    """
    async def go():
        return [event async for event in agen]
    return asyncio.run(go())

class ScriptedHost:
    """A scripted pi-sdk-host. Records the wire so tests assert on messages."""

    def __init__(self, responses=None, notifications_on=None, thread_id="th_1",
                 session_file="/sessions/chan/session.jsonl"):
        self.sent: list[dict] = []
        self.pending: list[dict] = []
        self.closed = False
        self.notifications_on = notifications_on or {}
        self.responses = responses or {}
        self.thread_id = thread_id
        self.session_file = session_file

    def send(self, message: dict) -> None:
        self.sent.append(message)
        if "result" in message or "error" in message:
            return  # the client answering a host request (tool/approve)
        if "id" not in message:
            return
        method = message["method"]
        self.pending.extend(self.notifications_on.get(method, []))
        if method in self.responses:
            result = self.responses[method]
        elif method == "thread/start":
            result = {"thread": {"id": self.thread_id, "sessionFile": self.session_file}}
        elif method == "turn/start":
            result = {"turnId": "tn_1"}
        else:
            result = {}
        self.pending.append({"id": message["id"], "result": result})

    def receive(self) -> dict:
        if not self.pending:
            raise AssertionError("scripted host exhausted")
        return self.pending.pop(0)

    def close(self) -> None:
        self.closed = True

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    def params_for(self, method: str) -> dict:
        return next(m["params"] for m in self.sent if m.get("method") == method)


class _Resolvable:
    def __init__(self, resolvable: bool):
        self.resolvable = resolvable

    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=self.resolvable, detail="scripted")


def _runner(tmp_path: Path, host: ScriptedHost, resolvable: bool = True,
            channel: str = "pi-chan") -> PiSdkRunner:
    return PiSdkRunner(
        PiOrchConfig(channel=channel, cwd=str(tmp_path), session_root=tmp_path / "sessions"),
        transport_factory=lambda: host,
        evidence_resolver=_Resolvable(resolvable),
    )


def _completed(turn_id: str = "tn_1", final_text: str = "") -> dict:
    return {"method": "turn/completed",
            "params": {"turnId": turn_id, "ok": True, "finalText": final_text,
                       "stopReason": "end_turn"}}


# ------------------------------------------------------------------ warmth


def test_first_connect_asks_for_a_persisted_session(tmp_path):
    """inMemory is the host default; a warm channel must opt in or it gets a
    session that cannot be reopened."""
    host = ScriptedHost()
    _runner(tmp_path, host).connect()
    params = host.params_for("thread/start")
    assert params["sessionDir"] == str(tmp_path / "sessions" / "pi-chan")
    assert "sessionFile" not in params


def test_the_returned_session_file_is_persisted_for_the_channel(tmp_path):
    host = ScriptedHost(session_file="/s/chan/abc.jsonl")
    _runner(tmp_path, host).connect()
    stored = (tmp_path / "sessions" / "pi-chan" / "last-session-file").read_text()
    assert stored.strip() == "/s/chan/abc.jsonl"


def test_a_later_process_reopens_the_channels_session(tmp_path):
    """The property the warm orchestrator exists for."""
    first = ScriptedHost(session_file="/s/chan/abc.jsonl")
    _runner(tmp_path, first).connect()

    second = ScriptedHost(session_file="/s/chan/abc.jsonl")
    _runner(tmp_path, second).connect()

    assert second.params_for("thread/start")["sessionFile"] == "/s/chan/abc.jsonl"


def test_a_host_that_returns_no_session_file_is_refused(tmp_path):
    """Fail loudly rather than silently going cold.

    Persistence was requested; a host that answers without a sessionFile has
    given us an in-memory session, and continuing would present a cold session
    as a warm one — invisible from outside, because the turn would simply have
    no memory.
    """
    host = ScriptedHost(responses={"thread/start": {"thread": {"id": "th_1"}}})
    with pytest.raises(Exception) as excinfo:
        _runner(tmp_path, host).connect()
    assert "pi-session-not-persisted" in str(excinfo.value)


# ------------------------------------------------------------------- turns


def test_text_deltas_stream_as_the_shared_event_type(tmp_path):
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/textDelta", "params": {"turnId": "tn_1", "delta": "pi says"}},
        _completed(),
    ]})
    events = _drain(_runner(tmp_path, host).stream_turn("hello"))
    assert TextDelta(text="pi says") in events


def test_tool_calls_stream_as_the_shared_event_types(tmp_path):
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/toolStarted",
         "params": {"turnId": "tn_1", "toolCallId": "tc1", "toolName": "bash",
                    "args": {"command": "ls"}}},
        {"method": "turn/toolFinished",
         "params": {"turnId": "tn_1", "toolCallId": "tc1", "toolName": "bash",
                    "isError": False}},
        _completed(),
    ]})
    events = _drain(_runner(tmp_path, host).stream_turn("go"))
    assert ToolCallStarted(tool_call_id="tc1", title="bash", kind="execute") in events
    assert ToolCallCompleted(tool_call_id="tc1", status="completed") in events


def test_a_failed_tool_is_not_reported_as_completed(tmp_path):
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/toolFinished",
         "params": {"turnId": "tn_1", "toolCallId": "tc1", "toolName": "bash",
                    "isError": True}},
        _completed(),
    ]})
    events = _drain(_runner(tmp_path, host).stream_turn("go"))
    assert ToolCallCompleted(tool_call_id="tc1", status="failed") in events


def test_the_turn_ends_at_turn_completed_not_at_the_response(tmp_path):
    """host.mjs's contract: the turn/start response only ACKNOWLEDGES; the turn
    is over at the turn/completed notification. Stopping at the response would
    truncate every turn."""
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/textDelta", "params": {"turnId": "tn_1", "delta": "a"}},
        _completed(),
        {"method": "turn/textDelta", "params": {"turnId": "tn_1", "delta": "AFTER"}},
    ]})
    events = _drain(_runner(tmp_path, host).stream_turn("go"))
    assert TextDelta(text="a") in events
    assert TextDelta(text="AFTER") not in events


# -------------------------------------------------------------- the gate


def test_approvals_are_requested_so_the_gate_is_reachable(tmp_path):
    """Without this param the host wraps nothing and never asks — the gate
    would be unreachable, which is the defect the panel found in the codex
    approval policy."""
    host = ScriptedHost()
    _runner(tmp_path, host).connect()
    assert "approvals" in host.params_for("thread/start")


def _approve_request(command: str, request_id: int = 77) -> dict:
    return {"id": request_id, "method": "tool/approve",
            "params": {"toolName": "bash", "args": {"command": command}}}


def test_an_unresolved_merge_is_denied_with_its_specific_code(tmp_path):
    host = ScriptedHost(notifications_on={"turn/start": [
        _approve_request("git -C /repo merge topic"), _completed(),
    ]})
    _drain(_runner(tmp_path, host, resolvable=False).stream_turn("merge it"))
    answer = next(m for m in host.sent if m.get("id") == 77 and "result" in m)
    assert answer["result"]["allow"] is False
    assert answer["result"]["code"] == "merge-close-evidence-unresolved"


def test_a_merge_with_evidence_is_allowed(tmp_path):
    """The other direction — proves the refusal above is the gate deciding,
    not the gate refusing everything."""
    host = ScriptedHost(notifications_on={"turn/start": [
        _approve_request("git -C /repo merge topic"), _completed(),
    ]})
    _drain(_runner(tmp_path, host, resolvable=True).stream_turn("merge it"))
    answer = next(m for m in host.sent if m.get("id") == 77 and "result" in m)
    assert answer["result"]["allow"] is True


def test_pis_lowercase_bash_is_mapped_before_the_gate_sees_it(tmp_path):
    """gates.py matches `tool_name != "Bash"` and returns NO OPINION otherwise.

    pi names the tool `bash`. Passing it through unmapped would make the gate a
    silent no-op for EVERY pi command — present, reachable, and never firing.
    """
    host = ScriptedHost(notifications_on={"turn/start": [
        _approve_request("git merge topic"), _completed(),
    ]})
    _drain(_runner(tmp_path, host, resolvable=False).stream_turn("merge"))
    answer = next(m for m in host.sent if m.get("id") == 77 and "result" in m)
    assert answer["result"]["allow"] is False, "unmapped tool name would allow the merge"


def test_a_non_gated_tool_is_allowed_without_evidence(tmp_path):
    """The gate has an opinion only on merge/close-shaped actions; everything
    else proceeds, or the orchestrator could not work at all."""
    host = ScriptedHost(notifications_on={"turn/start": [
        {"id": 78, "method": "tool/approve",
         "params": {"toolName": "bash", "args": {"command": "ls -la"}}},
        _completed(),
    ]})
    _drain(_runner(tmp_path, host, resolvable=False).stream_turn("look"))
    answer = next(m for m in host.sent if m.get("id") == 78 and "result" in m)
    assert answer["result"]["allow"] is True


def test_a_failed_turn_raises_instead_of_returning_empty_text(tmp_path):
    """Found live: a 404 from the model provider produced
    `turn/completed {ok: false, stopReason: "error", error: "404 ..."}` and the
    runner returned "" — a failed turn indistinguishable from a successful
    empty one. Silent failure is worse than a crash: the orchestrator would
    have carried on believing the seat had answered.
    """
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/completed",
         "params": {"turnId": "tn_1", "ok": False, "finalText": "",
                    "stopReason": "error", "error": "404 model not found"}},
    ]})
    with pytest.raises(Exception) as excinfo:
        _drain(_runner(tmp_path, host).stream_turn("go"))
    assert "pi-turn-failed" in str(excinfo.value)
    assert "404" in str(excinfo.value)


def test_a_successful_empty_turn_is_not_an_error(tmp_path):
    """The other direction: ok=true with no text is legitimate, so the guard
    must key on `ok`, not on emptiness."""
    host = ScriptedHost(notifications_on={"turn/start": [_completed()]})
    assert asyncio.run(_runner(tmp_path, host).turn("go")) == ""


def test_a_null_tool_call_id_does_not_become_the_string_None(tmp_path):
    """Panel finding (cold-Opus P2-1).

    The host sends `toolCallId` PRESENT with value null, so a dict default
    never fires and `str(None)` produced the literal "None". Two concurrent
    tool calls both became "None", and a client keying rows by id — buzz does,
    acp.rs:1721-1741 — collapses them into one.
    """
    host = ScriptedHost(notifications_on={"turn/start": [
        {"method": "turn/toolStarted",
         "params": {"turnId": "tn_1", "toolCallId": None, "toolName": "bash", "args": {}}},
        {"method": "turn/toolFinished",
         "params": {"turnId": "tn_1", "toolCallId": None, "toolName": "bash"}},
        _completed(),
    ]})
    events = _drain(_runner(tmp_path, host).stream_turn("go"))
    ids = [getattr(e, "tool_call_id", None) for e in events]
    assert "None" not in ids, f"literal 'None' reached the wire: {ids}"
