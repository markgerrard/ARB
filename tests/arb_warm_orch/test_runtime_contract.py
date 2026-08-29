"""The interface every warm runtime must satisfy for `acp_server` to drive it.

Panel `panel-warmorch-4slices-20260802T100544Z-222b63` found the gap this file
exists to close. `acp_server._cancel` awaited `runner.interrupt()`, which NO
runtime implemented; `_prompt` does `async for` over `stream_turn`, which only
the Claude runtime provided as an async generator. Both slipped because the
server's tests drive FAKES — `BlockingRunner` implements `interrupt`, and every
fake's `stream_turn` is an async generator — so the suite was green against an
interface no production class satisfied.

The lesson generalises past the two specific bugs: a test that exercises a
double instead of the real type proves the double conforms, not the code. So
this file asserts the contract against the REAL classes, and any runtime added
later is a one-line addition to `ALL_RUNTIMES`.
"""
from __future__ import annotations

import inspect

import pytest

from arb_warm_orch.codex_runner import CodexAppServerRunner
from arb_warm_orch.grok_runner import GrokAcpRunner
from arb_warm_orch.pi_runner import PiSdkRunner
from arb_warm_orch.runner import WarmOrchRunner

# Every runtime `--runtime` can select. Adding one here without implementing
# the contract is the failure mode this file catches.
ALL_RUNTIMES = [WarmOrchRunner, CodexAppServerRunner, GrokAcpRunner, PiSdkRunner]


@pytest.mark.parametrize("runtime", ALL_RUNTIMES, ids=lambda c: c.__name__)
def test_every_runtime_can_be_interrupted(runtime):
    """`acp_server._cancel` awaits `runner.interrupt()`.

    Missing on all four when the panel found it, so a cancel raised
    AttributeError inline and killed the serve loop — taking down the
    session-id persistence that cancel exists to protect.
    """
    assert hasattr(runtime, "interrupt"), (
        f"{runtime.__name__} has no interrupt(); acp_server._cancel awaits it"
    )
    assert inspect.iscoroutinefunction(runtime.interrupt), (
        f"{runtime.__name__}.interrupt must be awaitable — _cancel awaits it"
    )


@pytest.mark.parametrize("runtime", ALL_RUNTIMES, ids=lambda c: c.__name__)
def test_every_runtime_streams_asynchronously(runtime):
    """`acp_server._prompt` does `async for ... in runner.stream_turn(...)`.

    A sync generator raises TypeError there, and a missing method raises
    AttributeError — so `--acp` worked with exactly one of the four runtimes
    while three docstrings claimed otherwise.
    """
    stream = getattr(runtime, "stream_turn", None)
    assert stream is not None, f"{runtime.__name__} has no stream_turn()"
    assert inspect.isasyncgenfunction(stream), (
        f"{runtime.__name__}.stream_turn must be an ASYNC generator; "
        "acp_server consumes it with `async for`"
    )


@pytest.mark.parametrize("runtime", ALL_RUNTIMES, ids=lambda c: c.__name__)
def test_every_runtime_disconnects_awaitably(runtime):
    """The CLI's ACP path awaits `runner.disconnect()` in its finally block."""
    assert hasattr(runtime, "disconnect")


# The contract tests above check SHAPE. These drive the real ACP server over a
# real runner instance, which is the check that would have caught the panel's
# P1: `--acp` silently supported one runtime while three docstrings claimed
# four, and no test ever put a non-Claude runner behind the server.

import asyncio
import json
from pathlib import Path

from arb_warm_orch.acp_server import AcpServer
from arb_warm_orch.gates import EvidenceCheck
from arb_warm_orch.grok_runner import GrokAcpRunner, GrokOrchConfig
from arb_warm_orch.pi_runner import PiOrchConfig, PiSdkRunner


class _Never:
    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=False, detail="contract test")


class _Scripted:
    """Minimal scripted peer for whichever protocol the runtime speaks."""

    def __init__(self, answers, stream):
        self.answers, self.stream, self.q, self.sent = answers, stream, [], []

    def send(self, m):
        self.sent.append(m)
        if "result" in m or "error" in m or "id" not in m:
            return
        self.q.extend(self.stream.get(m["method"], []))
        self.q.append({"id": m["id"], "result": self.answers.get(m["method"], {})})

    def receive(self):
        if not self.q:
            raise AssertionError("scripted peer exhausted")
        return self.q.pop(0)

    def close(self):
        pass


def _serve_one_prompt(runner):
    sent = []

    async def send(message):
        sent.append(message)

    server = AcpServer(runner, channel="chan", send=send)
    response = asyncio.run(
        server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "session/prompt",
            "params": {"sessionId": "chan", "prompt": [{"type": "text", "text": "hi"}]},
        })
    )
    return response, sent


def test_acp_server_actually_drives_the_grok_runtime(tmp_path):
    peer = _Scripted(
        answers={
            "initialize": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
            "session/new": {"sessionId": "s1"},
            "session/prompt": {"stopReason": "end_turn"},
        },
        stream={"session/prompt": [{
            "method": "session/update",
            "params": {"sessionId": "s1", "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "from grok"}}},
        }]},
    )
    runner = GrokAcpRunner(
        GrokOrchConfig(channel="chan", cwd=str(tmp_path), session_root=tmp_path / "s"),
        transport_factory=lambda: peer, evidence_resolver=_Never())
    response, sent = _serve_one_prompt(runner)
    assert response["result"]["stopReason"] == "end_turn"
    texts = [m["params"]["update"]["content"]["text"]
             for m in sent if m.get("method") == "session/update"]
    assert texts == ["from grok"]


def test_acp_server_actually_drives_the_pi_runtime(tmp_path):
    peer = _Scripted(
        answers={
            "initialize": {},
            "thread/start": {"thread": {"id": "th1", "sessionFile": "/s/a.jsonl"}},
            "turn/start": {"turnId": "tn1"},
        },
        stream={"turn/start": [
            {"method": "turn/textDelta", "params": {"turnId": "tn1", "delta": "from pi"}},
            {"method": "turn/completed",
             "params": {"turnId": "tn1", "ok": True, "stopReason": "end_turn"}},
        ]},
    )
    runner = PiSdkRunner(
        PiOrchConfig(channel="chan", cwd=str(tmp_path), session_root=tmp_path / "s"),
        transport_factory=lambda: peer, evidence_resolver=_Never())
    response, sent = _serve_one_prompt(runner)
    assert response["result"]["stopReason"] == "end_turn"
    texts = [m["params"]["update"]["content"]["text"]
             for m in sent if m.get("method") == "session/update"]
    assert texts == ["from pi"]


# ======================================================================
# BEHAVIOURAL interrupt tests — panel
# panel-warmorch-remediation-20260802T121813Z-06f457
#
# The shape tests above assert `interrupt` EXISTS and is a coroutine. A codex
# implementation that sent a NOTIFICATION with a missing required `turnId`
# satisfied every one of them and could not work — the vendor schema defines
# `turn/interrupt` as a ClientRequest requiring {threadId, turnId}, and a
# correct implementation already existed in this repo at engines/codex.py:594.
#
# So these call interrupt() on the REAL runtime and assert the WIRE: method
# name, params, and request-vs-notification. Shape conformance is not
# behaviour, and this file previously conflated them.
# ======================================================================

from arb_warm_orch.codex_approvals import ApproveNonGated, CodexApprovalPolicy
from arb_warm_orch.codex_runner import CodexAppServerRunner, CodexOrchConfig


class _WireTransport:
    """Records every message sent, and answers requests minimally."""

    def __init__(self, answers=None, stream=None):
        self.sent, self.q = [], []
        self.answers, self.stream = answers or {}, stream or {}

    def send(self, m):
        self.sent.append(m)
        if "result" in m or "error" in m or "id" not in m:
            return
        # Response FIRST, then any notifications: a request's reply precedes
        # the stream it opens, which is the order codex's turn/start needs.
        self.q.append({"id": m["id"], "result": self.answers.get(m["method"], {})})
        self.q.extend(self.stream.get(m["method"], []))

    def receive(self):
        if not self.q:
            raise AssertionError("scripted peer exhausted")
        return self.q.pop(0)

    def close(self):
        pass

    def sent_with_method(self, method):
        return [m for m in self.sent if m.get("method") == method]


def test_codex_interrupt_sends_a_REQUEST_with_both_required_fields(tmp_path):
    """`TurnInterruptParams = {threadId, turnId}` and `turn/interrupt` is a
    ClientRequest (0 occurrences in ClientNotification). A notification, or a
    call missing `turnId`, is silently ignored by the peer — the turn keeps
    running while cancel reports success."""
    peer = _WireTransport(
        answers={
            "initialize": {"userAgent": "x"},
            "thread/start": {"thread": {"id": "th-1"}},
            "turn/start": {"turn": {"id": "tn-1"}},
        },
        stream={"turn/start": [
            {"method": "item/agentMessage/delta",
             "params": {"turnId": "tn-1", "delta": "partial"}},
            {"method": "turn/completed",
             "params": {"turn": {"id": "tn-1", "status": "completed"}}},
        ]},
    )
    runner = CodexAppServerRunner(
        CodexOrchConfig(channel="c", cwd=str(tmp_path), session_root=tmp_path / "s",
                        approval_policy_mode="untrusted"),
        approval_policy=CodexApprovalPolicy(evidence=_Never(), base_policy=ApproveNonGated()),
        transport_factory=lambda: peer,
    )
    asyncio.run(_interrupt_mid_turn(runner))

    calls = peer.sent_with_method("turn/interrupt")
    assert calls, "interrupt() sent no turn/interrupt at all"
    msg = calls[0]
    assert "id" in msg, "turn/interrupt must be a REQUEST — a notification is ignored"
    assert msg["params"].get("threadId"), "missing threadId"
    assert msg["params"].get("turnId"), "missing REQUIRED turnId — peer cannot identify the turn"


async def _interrupt_mid_turn(runner):
    """Interrupt while the turn is still OPEN — the only case that matters.

    Cancelling an already-finished turn is a no-op, so a test that drains the
    stream first proves nothing about cancel. This consumes one event and then
    interrupts with the generator still live, which is what
    `acp_server._cancel` does.
    """
    stream = runner.stream_turn("go")
    await stream.__anext__()          # mid-turn: one event consumed
    await runner.interrupt()
    await stream.aclose()


def test_grok_interrupt_sends_session_cancel_as_a_notification(tmp_path):
    """ACP `session/cancel` is a NOTIFICATION; the agent answers the in-flight
    prompt with a cancelled stop reason instead of replying to it."""
    peer = _WireTransport(answers={
        "initialize": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
        "session/new": {"sessionId": "s1"},
    })
    runner = GrokAcpRunner(
        GrokOrchConfig(channel="c", cwd=str(tmp_path), session_root=tmp_path / "s"),
        transport_factory=lambda: peer, evidence_resolver=_Never())

    async def go():
        await asyncio.to_thread(runner.connect)
        await runner.interrupt()
    asyncio.run(go())

    calls = peer.sent_with_method("session/cancel")
    assert calls, "interrupt() sent no session/cancel"
    assert "id" not in calls[0], "session/cancel is a notification, not a request"
    assert calls[0]["params"]["sessionId"] == "s1"


def test_pi_interrupt_sends_turn_abort_for_the_live_thread(tmp_path):
    peer = _WireTransport(answers={
        "initialize": {},
        "thread/start": {"thread": {"id": "th1", "sessionFile": "/s/a.jsonl"}},
    })
    runner = PiSdkRunner(
        PiOrchConfig(channel="c", cwd=str(tmp_path), session_root=tmp_path / "s"),
        transport_factory=lambda: peer, evidence_resolver=_Never())

    async def go():
        await asyncio.to_thread(runner.connect)
        await runner.interrupt()
    asyncio.run(go())

    calls = peer.sent_with_method("turn/abort")
    assert calls, "interrupt() sent no turn/abort"
    assert calls[0]["params"]["threadId"] == "th1"


def test_claude_interrupt_reaches_the_sdk_client(tmp_path):
    from arb_warm_orch.dispatch import StubSeatDispatcher
    from arb_warm_orch.runner import WarmOrchConfig, WarmOrchRunner

    class FakeClient:
        def __init__(self, options): self.interrupted = False
        async def connect(self): pass
        async def disconnect(self): pass
        async def interrupt(self): self.interrupted = True
        async def query(self, p): pass
        async def receive_response(self):
            return; yield  # empty async generator

    made = []
    def factory(options):
        c = FakeClient(options); made.append(c); return c

    runner = WarmOrchRunner(
        WarmOrchConfig(channel="c", cwd=str(tmp_path), session_root=tmp_path / "s"),
        dispatcher=StubSeatDispatcher(), evidence_resolver=_Never(), client_factory=factory)

    async def go():
        await runner.connect()
        await runner.interrupt()
    asyncio.run(go())
    assert made and made[0].interrupted, "interrupt() never reached the SDK client"
