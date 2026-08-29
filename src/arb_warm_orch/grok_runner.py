"""Warm-orch runtime 3 — grok Build over ACP (warm side).

Polarity: the COLD seat engine (`engines/grok_acp.py`) rotates a fresh
`session/new` per dispatch so a worker never re-serves accumulated context
(grok_acp.py:255). This module is the opposite — the channel is durable, its
session id is persisted, and a later process RESUMES it. Two modules per
runtime, warm and cold, as with `runner.py`/`agent_sdk.py` and
`codex_runner.py`/`codex.py`.

Capabilities were probed from the real `grok agent stdio` binary rather than
inferred (internal orchestration log):

* `loadSession: true` — so warmth is genuinely available. The bridge's
  `supports_thread_resume = False` on the cold engine is a worker-appropriate
  CHOICE, not an adapter limit; reading it as a limit would have designed this
  module around a constraint that does not exist.
* `protocolVersion: 1` — grok does NOT answer the 2 that buzz pins, so the
  version this client prefers is a request, never an assumption.
* `_meta."x.ai/hooks"` with `blockingEvents: ["pre_tool_use", ...]` and
  `decisions: ["deny","block"]` — a native pre-tool-use gate seam.

Gate direction matters here and inverts against the ACP SERVER in
`acp_server.py`. There we could not gate on `session/request_permission`
because the consumer auto-approves it. Here we are the CLIENT, so that method
is ours to answer and is a legitimate gate. Gate where you decide, never where
you ask.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from .gates import EvidenceResolver, evaluate_merge_close
from .turn_events import (
    ReasoningDelta,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEvent,
)

# What this client asks for. grok answers 1; the negotiated value is whatever
# the agent returns, never what we sent.
PREFERRED_PROTOCOL_VERSION = 1


class GrokResumeUnsupported(RuntimeError):
    """The agent cannot load a session, so a warm channel cannot be honoured."""


class GrokTurnFailed(RuntimeError):
    """The turn came back a JSON-RPC error. Never a silent empty reply."""


class Transport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...
    def receive(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class GrokOrchConfig:
    channel: str
    cwd: str
    session_root: Path
    model: str | None = None


class GrokAcpRunner:
    def __init__(
        self,
        config: GrokOrchConfig,
        *,
        transport_factory: Callable[[], Transport],
        evidence_resolver: EvidenceResolver,
    ) -> None:
        # `evidence_resolver` is REQUIRED, not defaulted. A runtime that can be
        # constructed without a gate ships with the gate unreachable, which is
        # exactly the defect the 2026-08-01 panel found in the codex approval
        # policy: the danger was documented in three places and mitigated in
        # none. Make the omission impossible rather than discouraged.
        self.config = config
        self.transport_factory = transport_factory
        self.evidence_resolver = evidence_resolver
        self._transport: Transport | None = None
        self._session_id: str | None = None
        self._next_id = 0

    # ------------------------------------------------------- session id

    def _session_id_path(self) -> Path:
        return Path(self.config.session_root) / self.config.channel / "last-session-id"

    def _load_session_id(self) -> str | None:
        try:
            value = self._session_id_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _persist_session_id(self, session_id: str) -> None:
        path = self._session_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{session_id}\n", encoding="utf-8")

    # ---------------------------------------------------------- plumbing

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._transport is not None
        request_id = self._allocate_id()
        self._transport.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            message = self._transport.receive()
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"grok-request-failed: {method}: {message['error']}")
                return message.get("result") or {}

    # ----------------------------------------------------------- connect

    def connect(self) -> None:
        if self._transport is not None:
            return
        self._transport = self.transport_factory()
        capabilities = self._request(
            "initialize",
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "clientCapabilities": {},
            },
        ).get("agentCapabilities") or {}

        prior = self._load_session_id()
        if prior is None:
            result = self._request(
                "session/new", {"cwd": self.config.cwd, "mcpServers": []}
            )
            session_id = result.get("sessionId")
            if not session_id:
                raise RuntimeError("grok-session-new-no-id: session/new returned no sessionId")
            self._session_id = session_id
            self._persist_session_id(session_id)
            return

        # Resuming. Refuse rather than silently minting a fresh session: a cold
        # session presented as a warm one is the failure the channel
        # abstraction exists to make impossible, and it would be invisible.
        if not capabilities.get("loadSession"):
            raise GrokResumeUnsupported(
                "grok-resume-unsupported: channel "
                f"{self.config.channel!r} has session {prior!r} to resume but the "
                "agent does not advertise loadSession; refusing to present a cold "
                "session as warm"
            )
        self._request(
            "session/load",
            {"sessionId": prior, "cwd": self.config.cwd, "mcpServers": []},
        )
        self._session_id = prior

    def disconnect(self) -> None:
        if self._transport is None:
            return
        self._transport.close()
        self._transport = None

    # ------------------------------------------------------------- turns

    async def stream_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Stream a turn. ASYNC because `acp_server._prompt` does `async for`.

        A sync generator raised TypeError there, so `--acp --runtime grok` was
        dead on arrival while the docstrings claimed runtime portability
        (panel P1). The transport is blocking, so every read goes through
        `asyncio.to_thread` — otherwise this would pin the event loop and the
        serve loop could not read the `session/cancel` that is supposed to
        interrupt this very turn.
        """
        await asyncio.to_thread(self.connect)
        assert self._transport is not None
        request_id = self._allocate_id()
        await asyncio.to_thread(
            self._transport.send,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            },
        )
        while True:
            message = await asyncio.to_thread(self._transport.receive)
            if message.get("id") == request_id:
                # A JSON-RPC error here is a FAILED turn. Returning silently
                # made it indistinguishable from a successful empty one — the
                # same defect the pi 404 exposed, fixed there and not carried
                # across until the panel found it here (P1).
                if "error" in message:
                    raise GrokTurnFailed(
                        f"grok-turn-failed: session/prompt returned {message['error']}"
                    )
                return
            method = message.get("method")
            if method == "session/update":
                event = _as_turn_event(message.get("params") or {})
                if event is not None:
                    yield event
            elif method == "session/request_permission":
                self._answer_permission(message)

    async def interrupt(self) -> None:
        """Cancel the in-flight turn via ACP's `session/cancel` NOTIFICATION.

        Required by `acp_server._cancel`. Notification, not request: the agent
        answers the in-flight `session/prompt` with a cancelled stop reason
        rather than replying to this (see buzz's ACP cancel-notification
        handling), so the stream still ends on its own terms.
        """
        if self._transport is None or self._session_id is None:
            return
        await asyncio.to_thread(
            self._transport.send,
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": self._session_id},
            },
        )

    # -------------------------------------------------------------- gate

    def _answer_permission(self, request: dict[str, Any]) -> None:
        """Answer `session/request_permission` — this is where the gate lives.

        Legitimate here and NOT on the server side of `acp_server.py`: there
        the consumer answers and auto-approves, here WE answer. Gate where you
        decide, never where you ask.

        Options are matched by `kind`, never by a hardcoded `optionId` — the
        ids are agent-generated and differ per request.
        """
        assert self._transport is not None
        params = request.get("params") or {}
        tool_call = params.get("toolCall") or {}
        tool_input = tool_call.get("rawInput") or {}
        decision = evaluate_merge_close(self.evidence_resolver, "Bash", tool_input)

        wanted = "reject_once" if decision.denied else "allow_once"
        option_id = _option_id_by_kind(params.get("options") or [], wanted)
        if option_id is None:
            # No option of the kind we need. Fail CLOSED: pick any rejecting
            # option, and if there is none, say so rather than allowing.
            option_id = _option_id_by_kind(params.get("options") or [], "reject_always")
        if option_id is None:
            raise RuntimeError(
                "grok-permission-no-usable-option: cannot express "
                f"{'refusal' if decision.denied else 'approval'} for this request"
            )

        self._transport.send(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "outcome": {"outcome": "selected", "optionId": option_id},
                    # The decision's CODE travels with the answer so a refusal
                    # is attributable rather than an anonymous rejection.
                    "_meta": {
                        "arb": {
                            "code": decision.code,
                            "detail": decision.detail,
                            "denied": decision.denied,
                        }
                    },
                },
            }
        )

    async def turn(self, text: str) -> str:
        return "".join(
            [
                event.text
                async for event in self.stream_turn(text)
                if isinstance(event, TextDelta)
            ]
        )


def _option_id_by_kind(options: list[dict[str, Any]], kind: str) -> str | None:
    """Match by `kind`; ids are agent-generated and must never be hardcoded."""
    for option in options:
        if isinstance(option, dict) and option.get("kind") == kind:
            option_id = option.get("optionId")
            if isinstance(option_id, str):
                return option_id
    return None


def _as_turn_event(params: dict[str, Any]) -> TurnEvent | None:
    """Translate a `session/update` into the runtime-neutral vocabulary.

    Same events the Claude and codex runners emit, so `acp_server.py` can
    drive any runtime without knowing which one it is talking to.
    """
    update = params.get("update") or {}
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = (update.get("content") or {}).get("text")
        if text:
            return TextDelta(text=text)
    # grok speaks ACP natively, so the remaining vocabulary is a passthrough,
    # not a translation. Dropping these (the original behaviour) made every
    # grok turn report zero tool calls and an empty thought stream to the buzz
    # panel and the durable transcript, and left nothing to reset buzz's idle
    # deadline during a long tool phase.
    if kind == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text")
        if text:
            return ReasoningDelta(text=text)
    if kind == "tool_call":
        return ToolCallStarted(
            # `or ""` mirrors pi_runner: the key can be PRESENT with value
            # null, and str(None) would collapse concurrent calls to one id.
            tool_call_id=str(update.get("toolCallId") or ""),
            title=str(update.get("title") or ""),
            kind=str(update.get("kind") or "other"),
        )
    if kind == "tool_call_update":
        status = update.get("status")
        # ACP sends intermediate updates too; only terminal states are part of
        # the runtime-neutral vocabulary.
        if status in ("completed", "failed"):
            return ToolCallCompleted(
                tool_call_id=str(update.get("toolCallId") or ""),
                status=str(status),
            )
    return None
