"""Warm orchestrator over `codex app-server` — the second runtime.

Polarity is the same as WarmOrchRunner and opposite to the worker engines:
AgentSdkEngine retires after every dispatch so a pool never re-serves
accumulated context; the warm orchestrator KEEPS context. Warmth is a durable
property of the CHANNEL, not of process uptime — codex persists a thread id and
resumes it, exactly as the Claude runner persists a session id.

Only this module is vendor-specific. `dispatch.py` (the typed tool) and
`gates.py` (the merge/close decision) are runtime-agnostic and are shared with
the Claude runner unchanged; `codex_approvals.py` translates the same gate
decision into codex's approval wire format.

Protocol facts are verified against `codex app-server generate-json-schema`
(codex-cli 0.146.0), NOT against adapter prose — the methods are slash-delimited
(`thread/start`, `thread/resume`, `turn/start`), which reading the TypeScript
adapter's camelCase function names would have got wrong.

Turn loop shape, and why it is not just "read until completed": codex multiplexes
three kinds of message onto one stream mid-turn — responses to our requests,
notifications, and BLOCKING server->client requests. An unanswered blocking
request stalls the harness forever, so the loop answers every request it sees,
including ones it has no policy for (with a specific-coded error). Notifications
are filtered by turn id: another turn's `turn/completed` must not end ours.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from .codex_approvals import CodexApprovalPolicy
from .turn_events import (
    ReasoningDelta,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEvent,
)

CLIENT_NAME = "arb-warm-orch"
CLIENT_VERSION = "0.1.0"

AGENT_MESSAGE_DELTA = "item/agentMessage/delta"
# Reasoning stream: summaries are what codex emits under normal config
# (`--effort high` produces them); raw textDelta only appears with special
# config, but costs nothing to accept. Both carry `delta` like agentMessage.
REASONING_DELTAS = ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta")
# Phase boundary within a reasoning item. Carries `summaryIndex`; index 0 opens
# the item (no separator wanted), later parts get a blank line so consumers
# that concatenate deltas (the buzz thinking panel does) show distinct phases
# instead of one run-on string of bold headings.
REASONING_PART_ADDED = "item/reasoning/summaryPartAdded"
# Item lifecycle pair. Tool use rides these, not a dedicated tool notification:
# codex wraps each command/patch/MCP call in a typed ThreadItem and announces it
# via `item/started` / `item/completed`. Without translating them the ACP layer
# reports zero tool calls for every codex turn — the buzz thinking panel and the
# durable turn transcript both showed "0 tool calls" while codex was visibly
# running commands — and, worse, a long silent tool phase emits nothing that
# resets buzz's idle deadline (`turn_events.ToolCallStarted` is what resets it).
ITEM_STARTED = "item/started"
ITEM_COMPLETED = "item/completed"
TURN_COMPLETED = "turn/completed"
ERROR_NOTIFICATION = "error"

# ThreadItem variants that represent tool use, mapped to an ACP `kind`. The
# item types and their fields are from `codex app-server generate-json-schema`
# v2/ItemStartedNotification.json (codex-cli 0.146.0); variants not listed
# (reasoning, agentMessage, plan, …) are streams, not tool calls.
_TOOL_ITEM_KINDS = {
    "commandExecution": "execute",
    "fileChange": "edit",
    "mcpToolCall": "fetch",
    "dynamicToolCall": "other",
    "webSearch": "search",
}

# Item statuses are inProgress|completed|failed|declined (McpToolCall and
# DynamicToolCall omit declined). ACP consumers distinguish success from
# failure only, and a declined call did not run — that is a failure to execute,
# not a success.
_FAILED_ITEM_STATUSES = frozenset({"failed", "declined"})


def _tool_item_title(item: dict[str, Any]) -> str:
    """A human-readable one-liner for the panel/transcript row."""
    item_type = item.get("type")
    if item_type == "commandExecution":
        return str(item.get("command") or "command")
    if item_type == "fileChange":
        paths = [
            str(change.get("path"))
            for change in item.get("changes") or []
            if isinstance(change, dict) and change.get("path")
        ]
        return "edit " + ", ".join(paths) if paths else "apply file changes"
    if item_type == "mcpToolCall":
        return f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
    if item_type == "dynamicToolCall":
        namespace = item.get("namespace")
        tool = str(item.get("tool") or "tool")
        return f"{namespace}.{tool}" if namespace else tool
    if item_type == "webSearch":
        return str(item.get("query") or "web search")
    return str(item_type)


class CodexTurnFailed(RuntimeError):
    """The server reported a non-retryable error for the turn under way."""


class CodexGateUnreachable(RuntimeError):
    """The configured approval policy cannot reach the merge/close gate."""


# Approval policies under which codex actually asks the client, so the gate can
# fire. `never` is excluded on purpose: under it codex executes without asking,
# the policy layer is never consulted, and the gate becomes decorative — a
# control that cannot fire is worse than none, because it reads as protection.
GATE_REACHABLE_APPROVAL_POLICIES: frozenset[str] = frozenset({"untrusted", "on-request"})

# Sent when the operator names no policy. The gate is the whole point of this
# runner, so the default has to be one the gate can reach.
DEFAULT_APPROVAL_POLICY = "untrusted"

# TurnStatus values that mean the turn actually finished successfully.
COMPLETED_TURN_STATUSES: frozenset[str] = frozenset({"completed"})

# Reasoning-effort levels the codex app-server accepts (ReasoningEffort in the
# protocol). Mirrors engines/codex.py rather than importing it: the warm
# orchestrator is a separate runtime and must not take a dependency on the
# bridge's engine pool just to name six strings.
CODEX_EFFORT_LEVELS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Validate a pinned reasoning-effort value; None/'' → None (follow config)."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in CODEX_EFFORT_LEVELS:
        raise ValueError(
            f"invalid reasoning effort {value!r}; expected one of {sorted(CODEX_EFFORT_LEVELS)}"
        )
    return normalized


class Transport(Protocol):
    """One framed JSON-RPC connection to a codex app-server."""

    def send(self, message: dict[str, Any]) -> None: ...
    def receive(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class CodexOrchConfig:
    channel: str
    cwd: str
    session_root: Path
    model: str | None = None
    developer_instructions: str | None = None
    # Load-bearing for the gate, not cosmetic. Under codex's default policy an
    # exec may be auto-approved, in which case no approval request ever reaches
    # the policy layer and the merge/close gate is never consulted — a green
    # that proves nothing. "untrusted" is what makes the refusal path reachable.
    approval_policy_mode: str | None = None
    sandbox_mode: str | None = None
    # Pinned reasoning effort for this seat. None = follow ~/.codex/config.toml
    # (and, failing that, the model's own default — which for gpt-5.6-sol is
    # "low"). Pinning here is what makes a seat's depth independent of a
    # host-wide config edit. Verified 2026-08-03 on codex-cli 0.146.0: the
    # per-turn `effort` param OVERRIDES a config.toml model_reasoning_effort,
    # so a seat pin wins over the file.
    effort: str | None = None


class CodexAppServerRunner:
    def __init__(
        self,
        config: CodexOrchConfig,
        *,
        approval_policy: CodexApprovalPolicy,
        transport_factory: Callable[[], Transport],
    ) -> None:
        mode = config.approval_policy_mode or DEFAULT_APPROVAL_POLICY
        if mode not in GATE_REACHABLE_APPROVAL_POLICIES:
            raise CodexGateUnreachable(
                f"codex-approval-policy-cannot-reach-gate: {mode!r} does not make "
                "codex ask the client, so the merge/close gate would never be "
                f"consulted; use one of {sorted(GATE_REACHABLE_APPROVAL_POLICIES)}"
            )
        self.config = config
        self.approval_policy_mode = mode
        # Fail at construction, not mid-turn: a typo'd level should stop the
        # seat coming up rather than surface as a rejected turn hours later.
        self.effort = normalize_reasoning_effort(config.effort)
        self.approval_policy = approval_policy
        self.transport_factory = transport_factory
        self._transport: Transport | None = None
        self._thread_id: str | None = None
        # The peer identifies a turn by id; `turn/interrupt` REQUIRES it. Not
        # retaining it is why the first interrupt implementation could not work.
        self._active_turn_id: str | None = None
        self._next_id = 0

    # -------------------------------------------------------- thread id

    def _thread_id_path(self) -> Path:
        return Path(self.config.session_root) / self.config.channel / "last-thread-id"

    def _load_thread_id(self) -> str | None:
        try:
            value = self._thread_id_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _persist_thread_id(self, thread_id: str) -> None:
        path = self._thread_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{thread_id}\n", encoding="utf-8")

    # ------------------------------------------------------------- rpc

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and pump the stream until ITS response arrives.

        Anything else arriving first is handled in place — notifications and
        blocking server requests do not wait politely for our reply.
        """
        assert self._transport is not None
        request_id = self._allocate_id()
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._transport.send(message)
        while True:
            incoming = self._transport.receive()
            if incoming.get("id") == request_id and "method" not in incoming:
                if "error" in incoming:
                    raise CodexTurnFailed(f"{method} failed: {incoming['error']}")
                return incoming.get("result", {})
            self._handle_unsolicited(incoming)

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self._transport is not None
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._transport.send(message)

    def _handle_unsolicited(self, message: dict[str, Any]) -> None:
        """Answer server requests; ignore notifications we are not tracking."""
        if "method" in message and "id" in message:
            self._answer_server_request(message)

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        assert self._transport is not None
        answer = self.approval_policy.answer(
            message["method"], message.get("params") or {}
        )
        response: dict[str, Any] = {"id": message["id"]}
        if answer.error is not None:
            response["error"] = answer.error
        else:
            response["result"] = answer.result
        self._transport.send(response)

    # --------------------------------------------------------- connect

    def connect(self) -> None:
        if self._transport is not None:
            return
        self._transport = self.transport_factory()
        self._request(
            "initialize",
            {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}},
        )
        self._notify("initialized")

        prior = self._load_thread_id()
        if prior:
            result = self._request("thread/resume", self._thread_params(threadId=prior))
        else:
            result = self._request("thread/start", self._thread_params())
        thread_id = result.get("thread", {}).get("id")
        if not thread_id:
            raise CodexTurnFailed("codex-thread-id-missing: server returned no thread id")
        self._thread_id = thread_id
        self._persist_thread_id(thread_id)

    def _thread_params(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": self.config.cwd, **extra}
        if self.config.model:
            params["model"] = self.config.model
        if self.config.developer_instructions:
            params["developerInstructions"] = self.config.developer_instructions
        params["approvalPolicy"] = self.approval_policy_mode
        if self.config.sandbox_mode:
            params["sandbox"] = self.config.sandbox_mode
        return params

    # -------------------------------------------------- client system prompt

    def apply_system_prompt(self, system_prompt: str) -> bool:
        """Adopt a client-supplied system prompt. Returns whether it took.

        Mirrors `WarmOrchRunner.apply_system_prompt`. Without this method the
        ACP server's duck-typed `getattr(runner, "apply_system_prompt")` finds
        nothing and drops the prompt silently -- so a codex seat runs with none
        of what buzz composed for it, including the line telling the seat to
        post its reply with `buzz messages send`.

        codex's seam is `developerInstructions`, which `_thread_params` already
        sends on both thread/start and thread/resume. Naming is why this was
        missed: codex has the capability, under a word that does not contain
        "system prompt".

        Return value, and why it is not simply True:

        - Already connected -> False. The thread exists; changing it now would
          mean dropping warmth to adopt a refreshed prompt, which costs more
          than the staleness. Same call the Claude runner makes.
        - A persisted thread for this channel -> False, because we will RESUME
          it, and codex ignores developerInstructions on resume. Measured
          2026-08-03 on codex-cli 0.146.0 AND 0.147.0-alpha.1.2: start with
          instructions A, resume with B, and turns 1, 2 and 3 all still follow
          A. Upstream https://github.com/openai/codex/issues/19045 claims only
          the first resumed turn is affected; that is understated, and its
          proposed patch is stale against a field deleted in f2368b7de.

        We still SET it in the resume case even though we return False -- the
        value rides thread/resume, is ignored today, and starts working the day
        the fork fix lands with no change here. When it does, this should
        collapse to returning True whenever we are not already connected.

        Deliberately NOT forcing a fresh thread when the prompt changes: that
        trades channel continuity for prompt correctness and is a decision for
        the operator, not a side effect of a setter.
        """
        if self._transport is not None:
            return False
        self.config = replace(self.config, developer_instructions=system_prompt)
        return self._load_thread_id() is None

    def _turn_params(self, text: str) -> dict[str, Any]:
        """Params for `turn/start`.

        `effort` rides every turn, not just the first: codex STICKS the last
        effort it was given on a warm thread, and this runner resumes a
        persisted thread across restarts — so a pin that was only sent once
        would be silently inherited from whatever ran last.

        When nothing is pinned we send no `effort` at all rather than inventing
        a default, so an unpinned seat keeps following ~/.codex/config.toml.
        The cost of that choice: an unpinned seat resuming a thread that once
        ran at another level inherits it. Pin the seat if that matters.
        """
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if self.effort:
            params["effort"] = self.effort
        return params

    def disconnect(self) -> None:
        if self._transport is None:
            return
        self._transport.close()
        self._transport = None

    # ------------------------------------------------------------ turns

    async def turn(self, text: str) -> str:
        """Drain a whole turn and return its text — a consumer of stream_turn.

        Async like every other runtime: `acp_server` and the CLI drive all four
        through one interface, and a sync outlier is how `--acp` silently
        supported only one of them (panel P1).
        """
        return "".join(
            [
                event.text
                async for event in self.stream_turn(text)
                if isinstance(event, TextDelta)
            ]
        )

    async def stream_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Stream a turn as runtime-neutral events.

        The transport blocks, so reads go through `asyncio.to_thread`; pinning
        the loop here would stop the serve loop reading the cancel meant to
        interrupt this turn.
        """
        await asyncio.to_thread(self.connect)
        assert self._transport is not None
        result = await asyncio.to_thread(
            self._request,
            "turn/start",
            self._turn_params(text),
        )
        turn_id = result.get("turn", {}).get("id")
        if not turn_id:
            raise CodexTurnFailed("codex-turn-id-missing: server returned no turn id")
        self._active_turn_id = turn_id
        try:
            async for event in self._stream_until_complete(turn_id):
                yield event
        finally:
            self._active_turn_id = None

    async def interrupt(self) -> None:
        """Interrupt the in-flight turn via codex's `turn/interrupt`.

        A REQUEST carrying BOTH `threadId` and `turnId`, mirroring the
        correct implementation this repo already had at
        `engines/codex.py:594`. The first attempt sent a NOTIFICATION with
        `threadId` alone: the vendor schema defines `turn/interrupt` in
        `ClientRequest` (0 occurrences in `ClientNotification`) with
        `TurnInterruptParams = {threadId, turnId}`, so that message was
        silently ignored — cancel reported success while the turn ran on.

        No active turn means nothing to interrupt; that is a no-op, not an
        error, because `session/cancel` may arrive after a turn has ended.
        """
        if self._transport is None or self._thread_id is None:
            return
        if self._active_turn_id is None:
            return
        await asyncio.to_thread(
            self._request_no_wait,
            "turn/interrupt",
            {"threadId": self._thread_id, "turnId": self._active_turn_id},
        )

    def _request_no_wait(self, method: str, params: dict[str, Any]) -> None:
        """Send a REQUEST without blocking for its reply.

        Interrupt is issued from the cancel path while the turn loop owns the
        read side; waiting here would deadlock against it. The peer's reply is
        consumed by that loop as an unsolicited message.
        """
        assert self._transport is not None
        self._transport.send(
            {"id": self._allocate_id(), "method": method, "params": params}
        )

    async def _stream_until_complete(self, turn_id: str) -> AsyncIterator[TurnEvent]:
        assert self._transport is not None
        while True:
            message = await asyncio.to_thread(self._transport.receive)
            if "method" in message and "id" in message:
                self._answer_server_request(message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == AGENT_MESSAGE_DELTA and params.get("turnId") == turn_id:
                delta = params.get("delta", "")
                if delta:
                    yield TextDelta(text=delta)
            elif method in REASONING_DELTAS and params.get("turnId") == turn_id:
                delta = params.get("delta", "")
                if delta:
                    yield ReasoningDelta(text=delta)
            elif method == REASONING_PART_ADDED and params.get("turnId") == turn_id:
                if params.get("summaryIndex"):
                    yield ReasoningDelta(text="\n\n")
            elif method == ITEM_STARTED and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                kind = _TOOL_ITEM_KINDS.get(item.get("type"))
                if kind and item.get("id"):
                    yield ToolCallStarted(
                        tool_call_id=str(item["id"]),
                        title=_tool_item_title(item),
                        kind=kind,
                    )
            elif method == ITEM_COMPLETED and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                if _TOOL_ITEM_KINDS.get(item.get("type")) and item.get("id"):
                    yield ToolCallCompleted(
                        tool_call_id=str(item["id"]),
                        status=(
                            "failed"
                            if item.get("status") in _FAILED_ITEM_STATUSES
                            else "completed"
                        ),
                    )
            elif method == ERROR_NOTIFICATION and params.get("turnId") == turn_id:
                if not params.get("willRetry"):
                    detail = params.get("error", {}).get("message", "")
                    raise CodexTurnFailed(f"codex-turn-error: {detail}")
            elif method == TURN_COMPLETED and params.get("turn", {}).get("id") == turn_id:
                status = params.get("turn", {}).get("status")
                if status not in COMPLETED_TURN_STATUSES:
                    # TurnStatus includes interrupted/failed; returning the
                    # partial text would present an unfinished turn as an answer.
                    raise CodexTurnFailed(
                        f"codex-turn-not-completed: turn {turn_id} ended with "
                        f"status {status!r}"
                    )
                return
