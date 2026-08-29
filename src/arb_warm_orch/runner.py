"""Warm session runner — channel-keyed, auto-resuming, gated.

Polarity note: AgentSdkEngine (the worker engine) retires after every dispatch
so a pool never re-serves accumulated context. The warm orchestrator is the
opposite: the channel's session id is persisted and auto-resumed at connect,
so warmth survives process death. The channel is the durable identity;
sessions rotate beneath it.

The client factory is injected (the same seam AgentSdkEngine exposes), so
tests drive the full query/receive_response protocol against a fake client.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from agent_redis_bridge.engines.agent_sdk_session import FileSessionStore

from .dispatch import SeatDispatcher, build_dispatch_seat_tool
from .gates import EvidenceResolver, build_merge_close_gate
from .turn_events import (
    MAX_COMMAND_BYTES,
    ReasoningDelta,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEvent,
    clip_output,
    tool_kind,
)


def _command_preview(tool_name: str, tool_input: Any) -> str | None:
    """Render a tool's arguments as one readable line for the activity view.

    Shell-shaped tools get their bare command (what a reader actually wants to
    see); everything else gets compact JSON. Falls back to str() rather than
    raising, because a preview is never worth failing a turn over.
    """
    if not isinstance(tool_input, dict):
        return None if tool_input is None else str(tool_input)[:MAX_COMMAND_BYTES]
    for key in ("command", "file_path", "path", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:MAX_COMMAND_BYTES]
    try:
        rendered = json.dumps(tool_input, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        rendered = str(tool_input)
    return rendered[:MAX_COMMAND_BYTES] or None


def _result_text(content: Any) -> str:
    """Flatten an SDK tool result into plain text.

    Results arrive either as a bare string or as a list of content blocks; a
    block may be a dict (`{"type": "text", "text": ...}`) or an SDK object.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)

MCP_SERVER_NAME = "arb_orch"
DISPATCH_SEAT_TOOL_NAME = f"mcp__{MCP_SERVER_NAME}__dispatch_seat"

# The orchestrator's working toolset (buzz brief: dispatch · wait · steer ·
# cancel · memory · audit-close · git). Capability is broad ON PURPOSE — the
# control is the PreToolUse gates at the choke points, not capability
# starvation, and hooks fire BEFORE the permission layer (proven live:
# gate-tempting drive 3, the internal orchestration log). Narrowing this list is not a
# security mechanism; the gates are — and a gate is only as good as the command
# shapes it recognises. A 2026-08-01 panel found `git -C <path> merge` slipping
# past the original regex; gates.py now parses the git SUBCOMMAND instead. What
# is enforced is listed EXHAUSTIVELY in gates.py's module docstring, measured
# after three review panels — read that list, not this comment, before relying
# on the gate. In short: git merge/pull in any flag arrangement the tokeniser
# can read, compound commands, `gh pr merge/close`, bare `sh -c`, and
# merge-defining aliases are CAUGHT; backslash line continuations, an unquoted
# `#`, combined shell flags (`-lc`), gh boolean flags, `eval` and command
# substitution are NOT. An earlier version of this comment said shell
# indirection was uniformly uncovered and not claimed; it is now PARTLY
# covered, which is worse to describe loosely than either extreme.
ORCHESTRATOR_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    DISPATCH_SEAT_TOOL_NAME,
)


@dataclass(frozen=True)
class WarmOrchConfig:
    channel: str
    cwd: str
    session_root: Path
    model: str | None = None
    system_prompt: str | None = None


class WarmOrchRunner:
    def __init__(
        self,
        config: WarmOrchConfig,
        *,
        dispatcher: SeatDispatcher,
        evidence_resolver: EvidenceResolver,
        client_factory: Any = ClaudeSDKClient,
    ) -> None:
        self.config = config
        self.dispatcher = dispatcher
        self.evidence_resolver = evidence_resolver
        self.client_factory = client_factory
        self._client: Any | None = None

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

    # -------------------------------------------------- client system prompt

    def apply_system_prompt(self, system_prompt: str) -> bool:
        """Adopt a client-supplied system prompt. Returns whether it took.

        buzz composes the seat's prompt — `[Workspace]`, `[Base]`, `[System]`,
        `[Team Instructions]`, agent memory core, channel canvas — and writes it
        into `session/new` params whenever the agent answers protocolVersion 2
        (see buzz's session-pool-to-ACP-session wiring). Dropping it is silent
        and expensive: among the sections lost is the line instructing the seat
        to post its reply with `buzz messages send`, without which nothing ever
        reaches the channel (buzz itself only ever posts kind:9 for dead-letter
        notices).

        Cold runtimes only, by design. `system_prompt` is fixed at Agent SDK
        session creation and we resume a persisted session, so a warm channel
        keeps the prompt it was created with. Returning False (rather than
        reconnecting) is deliberate: silently dropping warmth to adopt a
        refreshed prompt would cost more than the staleness it fixes. The
        caller decides whether to care.
        """
        if self._client is not None:
            return False
        self.config = replace(self.config, system_prompt=system_prompt)
        return True

    # ---------------------------------------------------------- options

    def build_options(self) -> ClaudeAgentOptions:
        store = FileSessionStore(Path(self.config.session_root), self.config.channel)
        return ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model,
            # Adaptive thinking with summarized display: the model thinks when
            # a turn warrants it and the SDK surfaces streamable summaries as
            # ThinkingBlocks — which stream_turn forwards as ReasoningDelta so
            # buzz's observer panel shows thought frames for claude seats.
            thinking={"type": "adaptive", "display": "summarized"},
            # Load CLAUDE.md from the seat's cwd (SDK default is None = load
            # nothing). This is each seat's per-seat project memory — the
            # place to index host runbooks and standing recipes ("healthcheck
            # = follow <path>") so channel asks resolve to real documents.
            setting_sources=["project"],
            system_prompt=self.config.system_prompt,
            resume=self._load_session_id(),
            session_store=store,
            session_store_flush="batched",
            hooks={
                "PreToolUse": [
                    HookMatcher(hooks=[build_merge_close_gate(self.evidence_resolver)])
                ]
            },
            mcp_servers={
                MCP_SERVER_NAME: create_sdk_mcp_server(
                    MCP_SERVER_NAME,
                    tools=[build_dispatch_seat_tool(self.dispatcher)],
                )
            },
            allowed_tools=list(ORCHESTRATOR_TOOLS),
        )

    # ------------------------------------------------------------ turns

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = self.client_factory(options=self.build_options())
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        await self._client.disconnect()
        self._client = None

    async def interrupt(self) -> None:
        """End the in-flight turn without abandoning the stream.

        Required by `acp_server._cancel`. Its absence here was the panel's P0
        (`panel-warmorch-4slices-20260802T100544Z-222b63`): cancel awaited a
        method no runtime implemented, raised AttributeError inline, and killed
        the serve loop — destroying the session-id persistence cancel exists to
        protect. The cancel tests passed because the test double had it.

        Interrupting rather than abandoning is the point: the SDK ends the turn
        and the stream still terminates with its ResultMessage, so
        `_persist_session_id` runs and the channel advances.
        """
        if self._client is None:
            return
        await self._client.interrupt()

    async def stream_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        await self.connect()
        assert self._client is not None
        await self._client.query(text)
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield TextDelta(text=block.text)
                    elif isinstance(block, ThinkingBlock):
                        # Without this branch thinking is silently dropped and
                        # the buzz observer's thought view stays empty for
                        # claude seats while bash/tool rows stream fine —
                        # exactly the asymmetry Mark saw 2026-08-06. The ACP
                        # layer maps ReasoningDelta -> agent_thought_chunk.
                        yield ReasoningDelta(text=block.thinking)
                    elif isinstance(block, ToolUseBlock):
                        yield ToolCallStarted(
                            tool_call_id=block.id,
                            title=block.name,
                            kind=tool_kind(block.name),
                            command=_command_preview(block.name, block.input),
                        )
            elif isinstance(message, UserMessage):
                # The SDK returns tool RESULTS on the user turn, not the
                # assistant one, so a call's start and finish arrive on
                # different message types.
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output, dropped = clip_output(_result_text(block.content))
                        yield ToolCallCompleted(
                            tool_call_id=block.tool_use_id,
                            status="failed" if block.is_error else "completed",
                            output=output or None,
                            output_dropped_lines=dropped,
                        )
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    self._persist_session_id(message.session_id)

    async def turn(self, text: str) -> str:
        """Drain a whole turn and return its text — a consumer of stream_turn.

        Note the coupling this creates deliberately: session-id persistence
        happens inside `stream_turn`, so a caller that ABANDONS the generator
        early never reaches it and the channel does not advance. `turn` drains
        to exhaustion and is therefore safe; the ACP server, which streams to a
        client that may cancel mid-turn, is not automatically so.
        """
        reply_parts = [
            event.text
            async for event in self.stream_turn(text)
            if isinstance(event, TextDelta)
        ]
        return "".join(reply_parts)
