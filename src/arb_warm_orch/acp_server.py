"""Thin ACP server over the warm-orch runner (ARB-B25).

The graduation seam: the cockpit chain becomes direct-CLI -> ACP client -> buzz
without the runner changing underneath. Design pass and the citations behind
every wire-level choice: an internal orchestration log.

Two properties are load-bearing and easy to lose in a refactor:

* **The merge/close gate is NOT on this wire.** ACP offers
  `session/request_permission` as the natural home for an approval, and the
  shipped consumer auto-approves it — buzz selects the `allow_once` option by
  kind without asking anyone (see buzz's ACP permission-request handling). A gate whose
  verdict is supplied by the party it constrains is not a gate, so the
  merge/close decision stays in the runner's PreToolUse hook and this module
  never asks the client for permission.
* **The channel is the durable identity, not the ACP session.** `session/new`
  re-attaches to the launch-configured channel rather than minting a fresh
  one, because buzz never calls `session/load` (zero occurrences crate-wide)
  and its `_meta.sessionTitle` is a truncatable display string that degrades
  to the bare agent name (`src/config.rs:610-626`). Warmth survives client
  reconnects only if the server holds the channel.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .turn_events import ReasoningDelta, TextDelta, ToolCallCompleted, ToolCallStarted

PROTOCOL_VERSION = 2

# JSON-RPC 2.0 reserved codes.
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603

# Methods that are NOTIFICATIONS: no id, and answering one would put an
# unmatched response on the client's wire.
_NOTIFICATIONS = frozenset({"session/cancel"})


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


# buzz parses `stopReason` against a CLOSED set and raises a protocol error on
# anything outside it (see buzz's ACP stop-reason validation), so these are
# not free-form strings.
STOP_END_TURN = "end_turn"
STOP_CANCELLED = "cancelled"

Send = Callable[[dict[str, Any]], Awaitable[None]]


class BlockingWriter:
    """Minimal writer over any binary stream — file, pipe, socket alike.

    asyncio's `connect_write_pipe` REFUSES a regular file
    ("Pipe transport is only for pipes, sockets and character devices"), so an
    ACP server built on it works when its stdout is piped to another process
    and crashes when it is redirected to a file. Found live, after a smoke
    that piped through `head` passed. Writes here are small, newline-framed and
    already serialised by the send lock, so a blocking write with an explicit
    flush is both correct and portable.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, data: bytes) -> None:
        self._stream.write(data)

    async def drain(self) -> None:
        self._stream.flush()


async def serve(reader: Any, writer: Any, *, runner: Any, channel: str) -> list[str]:
    """Run the NDJSON read loop to EOF; returns the non-JSON lines skipped.

    Framing is newline-delimited JSON, matching `codex_stdio.py` on the client
    side. Non-JSON lines are skipped rather than fatal — a peer may print a
    banner — but they are RETURNED rather than silently dropped, because a
    stream quietly discarding everything looks identical to a quiet one.

    **`session/prompt` is dispatched as a task, not awaited inline.** A turn is
    long-running and `session/cancel` arrives DURING it; a loop that awaited
    each handler before reading the next line could not deliver the cancel
    until the turn it cancels had already ended.
    """
    lock = asyncio.Lock()

    async def send(message: dict[str, Any]) -> None:
        # Serialised: notifications are emitted from the prompt task while the
        # read loop may be writing a response, and interleaved partial writes
        # would corrupt the framing.
        async with lock:
            writer.write((json.dumps(message) + "\n").encode())
            await writer.drain()

    server = AcpServer(runner, channel=channel, send=send)
    skipped: list[str] = []
    in_flight: set[asyncio.Task] = set()

    async def dispatch(message: dict[str, Any]) -> None:
        """Answer every request, including the ones that fail.

        Without this the module emitted NO JSON-RPC error responses at all: a
        raising turn left the `session/prompt` request unanswered, and since
        prompts run under `create_task` the exception was captured in the task
        and never surfaced. The client cannot tell that from a slow turn — it
        waits forever. Making grok/pi RAISE on a failed turn (correct in
        itself) turned silent-empty-success into silent-hang.
        """
        request_id = message.get("id")
        try:
            response = await server.handle(message)
        except Exception as exc:  # noqa: BLE001 — every failure must be answered
            if request_id is not None:
                await send(_error(request_id, ERR_INTERNAL, str(exc)))
            return
        if response is not None:
            await send(response)
        elif request_id is not None and message.get("method") not in _NOTIFICATIONS:
            # A request we do not implement. Returning None meant "nothing to
            # send", so the caller hung on an unimplemented method too.
            await send(_error(request_id, ERR_METHOD_NOT_FOUND,
                              f"unknown method: {message.get('method')}"))

    while True:
        raw = await reader.readline()
        if not raw:
            break
        line = raw.decode().strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            skipped.append(line)
            continue
        if message.get("method") == "session/prompt":
            task = asyncio.create_task(dispatch(message))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        else:
            await dispatch(message)

    if in_flight:
        await asyncio.gather(*in_flight)
    return skipped


def _as_session_update(event: Any) -> dict[str, Any] | None:
    """Translate one runtime-neutral turn event into an ACP `session/update`.

    Returns None for events with no wire representation, so an unknown event
    is dropped rather than sent as a malformed update.
    """
    if isinstance(event, TextDelta):
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": event.text},
        }
    if isinstance(event, ReasoningDelta):
        return {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": event.text},
        }
    if isinstance(event, ToolCallStarted):
        update = {
            "sessionUpdate": "tool_call",
            "toolCallId": event.tool_call_id,
            "title": event.title,
            "kind": event.kind,
            "status": "in_progress",
        }
        # `rawInput` is ACP's slot for what the tool was actually asked to do.
        # Sent as a BARE STRING, not {"command": ...}: the host's extractor
        # passes strings through and JSON-stringifies anything else, so an
        # object arrives rendered as `{"command":"echo hi"}` in the panel.
        # Verified live 2026-08-07 — the object form shipped and looked fine
        # until the first real transcript showed the braces.
        # Omitted entirely when absent rather than sent as null, so a host that
        # predates the field sees exactly the update shape it saw before.
        if event.command:
            update["rawInput"] = event.command
        return update
    if isinstance(event, ToolCallCompleted):
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": event.tool_call_id,
            "status": event.status,
        }
        if event.output:
            # Text at the PART level, not nested under a "content" key: the
            # host collects `part.text` (or a bare string) from the array and
            # ignores parts shaped any other way. The nested form produced a
            # tool row with an empty output preview — present, plausible, and
            # silently carrying nothing (verified live 2026-08-07).
            update["content"] = [{"type": "text", "text": event.output}]
            if event.output_dropped_lines:
                # Key name is host-b's ACP contract (2026-08-07), not ours: the
                # host reads `outputTruncatedLines` on the update, with
                # `_meta.outputTruncatedLines` as its documented alternative.
                # An invented key here would have silently dropped the count.
                update["outputTruncatedLines"] = event.output_dropped_lines
        return update
    return None


def _prompt_text(params: dict[str, Any]) -> str:
    """Join every text block, not just the first.

    buzz sends `["/cmd args", "<buzz context>"]` as SEPARATE blocks so that
    slash-command detection sees the leading `/` (see buzz's ACP prompt-block
    construction). Reading only `prompt[0]` would silently drop the context.
    """
    blocks = params.get("prompt") or []
    return "\n".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


class AcpServer:
    def __init__(self, runner: Any, *, channel: str, send: Send) -> None:
        self.runner = runner
        self.channel = channel
        self.send = send
        self._cancelled = False

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        if method == "initialize":
            return self._ok(message, {"protocolVersion": PROTOCOL_VERSION})
        if method == "session/new":
            # Re-attach, never mint: the channel IS the session id, so a
            # reconnecting client lands back on the warm channel.
            self._adopt_system_prompt(message.get("params") or {})
            return self._ok(message, {"sessionId": self.channel})
        if method == "session/prompt":
            return await self._prompt(message)
        if method == "session/cancel":
            await self._cancel()
            return None  # a notification carries no id and gets no response
        return None

    # ------------------------------------------------------ system prompt

    def _adopt_system_prompt(self, params: dict[str, Any]) -> None:
        """Take the client's composed system prompt, if this runtime can hold one.

        We answer `protocolVersion: 2`, which makes buzz choose the modern
        delivery path and put the whole composed prompt in `session/new` params
        (see buzz's session-pool-to-ACP-session wiring) instead of re-injecting it as
        user-message sections every turn (the legacy path, gated on
        `has_system_prompt_support`). Reading no params therefore threw
        it away with no error on either side — the failure mode is a seat that
        works perfectly and says nothing in the channel.

        Duck-typed on purpose: a runtime that cannot hold a system prompt should
        no-op rather than raise. `grok_runner` genuinely has none —
        `GrokOrchConfig` carries no prompt field and its `session/prompt` sends
        bare text. `pi_runner` carries `append_system_prompt`, a different seam,
        left alone here.

        `codex_runner` is the trap: it DOES have a system-prompt seam, under a
        different name. `developer_instructions` (`codex_runner.py:80`) becomes
        `params["developerInstructions"]` on both `thread/start` and
        `thread/resume` (`codex_runner.py:209-210`), and `cli.py:133` wires
        `--system-prompt` straight into it. The getattr below misses it only
        because it looks for `apply_system_prompt`, which `WarmOrchRunner`
        alone defines. So a codex seat silently drops the client's composed
        prompt while HAVING somewhere to put it — the contract is lost at this
        line, not for want of a runtime capability. An earlier revision of this
        docstring asserted codex had "no system-prompt concept at all"; that was
        wrong, and it cost a live-test round in 2026-08 by making the fix look
        far more expensive than it is.

        Not yet established: whether codex honours `developerInstructions` on
        `thread/resume` for an already-started thread, or only at creation. Our
        normal path is warm re-attach, so wiring this seam without answering
        that would work cold and no-op on every warm channel.
        """
        text = params.get("systemPrompt")
        if not isinstance(text, str) or not text:
            return
        adopt = getattr(self.runner, "apply_system_prompt", None)
        if adopt is None:
            return
        adopt(text)

    # ------------------------------------------------------------- prompt

    async def _prompt(self, request: dict[str, Any]) -> dict[str, Any]:
        params = request.get("params") or {}
        self._cancelled = False  # per-turn; a leaked flag would mark every
        # later turn on the channel cancelled while it ran perfectly well
        try:
            async for event in self.runner.stream_turn(_prompt_text(params)):
                update = _as_session_update(event)
                if update is not None:
                    await self._notify_update(update)
        except Exception:
            # A cancel we ASKED for is an expected outcome, not an error, and
            # not every runtime ends a cancelled stream quietly. The codex
            # runner raises on any TurnStatus outside its completed set, and
            # `interrupted` — exactly what a cancel produces — is outside it
            # (`codex_runner.py:319-327`). On that path the stream unwinds
            # instead of ending, so the stop reason below was unreachable and
            # the caller got a JSON-RPC -32603. buzz parses `stopReason`
            # against a CLOSED set (cited above), so that is
            # not a cosmetic mislabel: the client sees an internal error where
            # the cancel it just requested belongs.
            #
            # Scoped to `self._cancelled` DELIBERATELY. Absorbing every stream
            # failure here would report a turn nobody cancelled as cleanly
            # cancelled, which is a worse lie than the error it replaced — so
            # an unrequested failure still propagates untouched.
            if not self._cancelled:
                raise
        stop_reason = STOP_CANCELLED if self._cancelled else STOP_END_TURN
        return self._ok(request, {"stopReason": stop_reason})

    async def _cancel(self) -> None:
        """Interrupt the turn — deliberately NOT `break` out of the stream.

        Abandoning the generator would skip the end-of-stream step where the
        channel's session id is persisted, so a cancelled turn would silently
        cost the channel its continuity and the next process would resume from
        a stale id or none at all. Interrupting instead lets the stream end on
        its own terms with that step intact.
        """
        self._cancelled = True
        await self.runner.interrupt()

    # ---------------------------------------------------------- responses

    async def _notify_update(self, update: dict[str, Any]) -> None:
        """Emit a `session/update` NOTIFICATION — deliberately no `id`.

        JSON-RPC 2.0 distinguishes a notification from a request by the
        absence of `id` alone; adding one would leave the client awaiting a
        reply that never arrives.
        """
        await self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": self.channel, "update": update},
            }
        )

    def _ok(self, request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
