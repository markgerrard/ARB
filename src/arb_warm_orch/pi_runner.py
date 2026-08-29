"""Warm-orch runtime 4 — pi SDK (warm side).

Polarity: the COLD seat engine (`engines/pi_sdk.py`) drives the same
`tools/pi-sdk-host/host.mjs` with an in-memory session, so a dispatched worker
accumulates nothing and leaves nothing on disk. This module is the opposite —
the channel is durable, its session FILE is persisted, and a later process
reopens it. Two modules per runtime, warm and cold, as elsewhere.

The host-side preconditions landed separately (warm-orch-sdk-log entries
38-42), both OPT-IN so the cold seat path is untouched:

* `thread/start` accepts `sessionDir`/`sessionFile` and returns
  `thread.sessionFile`. Without them the host builds
  `SessionManager.inMemory(cwd)` — correct for a worker, useless for a channel.
* an `approvals` param turns on a host->client `tool/approve` request for
  bash/write/edit. pi has no other client-consulted veto: `tool-guard.mjs`
  checks workspace containment, which is a different job.

Wire shape per host.mjs: `jsonrpc` is deliberately OMITTED to match
engines/codex.py, and a turn ENDS at the `turn/completed` notification — the
`turn/start` response only acknowledges and carries the new turnId.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from .gates import EvidenceResolver, evaluate_merge_close
from .turn_events import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEvent,
    tool_kind,
)

# pi names its shell tool `bash`; gates.py matches `tool_name != "Bash"` and
# returns NO OPINION otherwise. Without this mapping the gate would be present,
# reachable, and silently never fire on a single pi command.
_GATE_TOOL_NAMES = {"bash": "Bash"}


class PiSessionNotPersisted(RuntimeError):
    """The host gave us a session that cannot be reopened."""


class PiTurnFailed(RuntimeError):
    """The turn ended with ok=false. Never silently returns empty text."""


class Transport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...
    def receive(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class PiOrchConfig:
    channel: str
    cwd: str
    session_root: Path
    model: str | None = None
    thinking_level: str | None = None
    tools: tuple[str, ...] | None = None
    append_system_prompt: str | None = None


class PiSdkRunner:
    def __init__(
        self,
        config: PiOrchConfig,
        *,
        transport_factory: Callable[[], Transport],
        evidence_resolver: EvidenceResolver,
    ) -> None:
        # Required, not defaulted: a runtime constructible without a gate ships
        # with the gate unreachable — the defect the 2026-08-01 panel found in
        # the codex approval policy.
        self.config = config
        self.transport_factory = transport_factory
        self.evidence_resolver = evidence_resolver
        self._transport: Transport | None = None
        self._thread_id: str | None = None
        self._next_id = 0

    # ------------------------------------------------------- session file

    def _session_dir(self) -> Path:
        return Path(self.config.session_root) / self.config.channel

    def _session_file_pointer(self) -> Path:
        return self._session_dir() / "last-session-file"

    def _load_session_file(self) -> str | None:
        try:
            value = self._session_file_pointer().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _persist_session_file(self, session_file: str) -> None:
        path = self._session_file_pointer()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{session_file}\n", encoding="utf-8")

    # ---------------------------------------------------------- plumbing

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, message: dict[str, Any]) -> None:
        assert self._transport is not None
        self._transport.send(message)

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._allocate_id()
        self._send({"id": request_id, "method": method, "params": params or {}})
        while True:
            message = self._transport.receive()  # type: ignore[union-attr]
            if message.get("method") == "tool/approve":
                self._answer_approval(message)
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"pi-request-failed: {method}: {message['error']}")
                return message.get("result") or {}

    # ----------------------------------------------------------- connect

    def connect(self) -> None:
        if self._transport is not None:
            return
        self._transport = self.transport_factory()
        self._request(
            "initialize",
            {"clientInfo": {"name": "arb-warm-orch", "version": "0.1.0"}, "capabilities": {}},
        )

        params: dict[str, Any] = {
            "cwd": self.config.cwd,
            # Ask for a PERSISTED session; the host's default is in-memory.
            "sessionDir": str(self._session_dir()),
            # Turn on the host->client veto. Absent this the host wraps nothing
            # and never asks, leaving the merge/close gate unreachable.
            "approvals": {},
        }
        prior = self._load_session_file()
        if prior is not None:
            params["sessionFile"] = prior
        if self.config.model is not None:
            params["model"] = self.config.model
        if self.config.thinking_level is not None:
            params["thinkingLevel"] = self.config.thinking_level
        if self.config.tools is not None:
            params["tools"] = list(self.config.tools)
        if self.config.append_system_prompt is not None:
            params["appendSystemPrompt"] = self.config.append_system_prompt

        thread = self._request("thread/start", params).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError("pi-thread-start-no-id: thread/start returned no thread id")
        session_file = thread.get("sessionFile")
        if not session_file:
            # We asked to persist. A host that answers without a sessionFile
            # has handed us an in-memory session, and continuing would present
            # a cold session as a warm one — invisible from outside, because
            # the turn would simply have no memory.
            raise PiSessionNotPersisted(
                "pi-session-not-persisted: thread/start returned no sessionFile "
                f"for channel {self.config.channel!r}; refusing to present a cold "
                "session as warm"
            )
        self._thread_id = thread_id
        self._persist_session_file(session_file)

    def disconnect(self) -> None:
        if self._transport is None:
            return
        self._transport.close()
        self._transport = None

    # ------------------------------------------------------------- turns

    async def stream_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Stream a turn. ASYNC because `acp_server._prompt` does `async for`.

        A sync generator raised TypeError there, so `--acp --runtime pi` was
        dead on arrival while the docstrings claimed portability (panel P1).
        The host transport blocks, so reads go through `asyncio.to_thread` —
        otherwise the event loop would be pinned and the serve loop could not
        read the cancel meant to interrupt this turn.
        """
        await asyncio.to_thread(self.connect)
        request_id = self._allocate_id()
        await asyncio.to_thread(
            self._send,
            {"id": request_id, "method": "turn/start",
             "params": {"threadId": self._thread_id, "message": text}})
        while True:
            message = await asyncio.to_thread(self._transport.receive)  # type: ignore[union-attr]
            if message.get("id") == request_id and "error" in message:
                # The turn/start REQUEST itself was rejected. Ignoring it left
                # the loop waiting forever for a turn/completed the rejecting
                # host will never send (panel P1).
                raise PiTurnFailed(
                    f"pi-turn-failed: turn/start rejected: {message['error']}")
            method = message.get("method")
            if method == "tool/approve":
                self._answer_approval(message)
                continue
            if method == "turn/completed":
                # The turn ends HERE, not at the turn/start response — that
                # response only acknowledges. Stopping there truncates the turn.
                #
                # A failed turn RAISES. Found live: a 404 from the model
                # provider arrived as {ok: false, stopReason: "error", error:
                # "404 ..."} and simply returning left the caller with "" —
                # a failed turn indistinguishable from a successful empty one,
                # which the orchestrator would have read as "the seat answered".
                params = message.get("params") or {}
                if params.get("ok") is False:
                    raise PiTurnFailed(
                        f"pi-turn-failed: stopReason="
                        f"{params.get('stopReason')!r} error={params.get('error')!r}"
                    )
                return
            if method is not None:
                event = _as_turn_event(method, message.get("params") or {})
                if event is not None:
                    yield event

    async def turn(self, text: str) -> str:
        return "".join(
            [
                event.text
                async for event in self.stream_turn(text)
                if isinstance(event, TextDelta)
            ]
        )

    async def interrupt(self) -> None:
        """Abort the in-flight turn via the host's `turn/abort`.

        Required by `acp_server._cancel`. Per host.mjs's protocol contract the
        client must then wait for `turn/completed` rather than treating the
        abort response as the end — so the stream still terminates normally.
        """
        if self._transport is None or self._thread_id is None:
            return
        await asyncio.to_thread(
            self._send,
            {"id": self._allocate_id(), "method": "turn/abort",
             "params": {"threadId": self._thread_id}})

    # -------------------------------------------------------------- gate

    def _answer_approval(self, request: dict[str, Any]) -> None:
        """Answer a host `tool/approve` — this is where the gate lives.

        Same direction as the grok runtime: we are the client, so the verdict
        is ours to give. Anything the gate has no opinion on proceeds, or the
        orchestrator could not work at all.
        """
        params = request.get("params") or {}
        raw_name = str(params.get("toolName", ""))
        gate_name = _GATE_TOOL_NAMES.get(raw_name, raw_name)
        decision = evaluate_merge_close(
            self.evidence_resolver, gate_name, params.get("args") or {}
        )
        self._send(
            {
                "id": request.get("id"),
                "result": {
                    "allow": not decision.denied,
                    "code": decision.code,
                    "detail": decision.detail,
                },
            }
        )


def _as_turn_event(method: str, params: dict[str, Any]) -> TurnEvent | None:
    """Translate a host notification into the runtime-neutral vocabulary.

    Same events the other three runtimes emit, so `acp_server.py` can drive
    any of them without knowing which it is talking to.
    """
    if method == "turn/textDelta":
        delta = params.get("delta")
        if delta:
            return TextDelta(text=delta)
    elif method == "turn/toolStarted":
        name = str(params.get("toolName", ""))
        return ToolCallStarted(
            # `or ""`, not a dict default: the host sends the key PRESENT with
            # value null, so a default never fires and str(None) yielded the
            # literal "None" — collapsing concurrent calls to one id for any
            # client keying rows by it (buzz does).
            tool_call_id=str(params.get("toolCallId") or ""),
            title=name,
            kind=tool_kind(_GATE_TOOL_NAMES.get(name, name.capitalize())),
        )
    elif method == "turn/toolFinished":
        return ToolCallCompleted(
            tool_call_id=str(params.get("toolCallId") or ""),
            status="failed" if params.get("isError") else "completed",
        )
    return None
