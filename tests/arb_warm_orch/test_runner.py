"""Warm-orch test-drive slice: warm session runner.

The runner inverts the worker engine's polarity: AgentSdkEngine retires after
every dispatch so a pool never re-serves accumulated context; the warm orch
KEEPS context — the channel's session id is persisted and auto-resumed at
connect, so warmth is a durable property of the channel, not of process
uptime. The client factory is injected (same seam AgentSdkEngine uses), so
these tests drive a fake client through the real query/receive_response
protocol and real SDK message types. One test per named behavior, red-first.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_redis_bridge.engines.agent_sdk_session import FileSessionStore
from arb_warm_orch.dispatch import StubSeatDispatcher
from arb_warm_orch.gates import EvidenceCheck
from arb_warm_orch.runner import WarmOrchConfig, WarmOrchRunner
from arb_warm_orch.turn_events import TextDelta, ToolCallCompleted, ToolCallStarted


def _result_message(session_id: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
    )


@dataclass
class FakeClient:
    options: object
    queries: list = field(default_factory=list)
    connected: bool = False
    session_id: str = "sess-fake-1"
    # Scripted transport: None means the default one-text-block turn. Tests
    # that need tool-call traffic supply their own message sequence.
    scripted: list | None = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        if self.scripted is not None:
            for message in self.scripted:
                yield message
            return
        yield AssistantMessage(content=[TextBlock(text="warm reply")], model="m")
        yield _result_message(self.session_id)


class FakeClientFactory:
    def __init__(self, scripted: list | None = None):
        self.clients: list[FakeClient] = []
        self.scripted = scripted

    def __call__(self, options):
        client = FakeClient(options=options, scripted=self.scripted)
        self.clients.append(client)
        return client


@dataclass
class AlwaysResolvable:
    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=True)


def _runner(tmp_path: Path, channel: str = "test-channel") -> tuple[WarmOrchRunner, FakeClientFactory]:
    factory = FakeClientFactory()
    runner = WarmOrchRunner(
        WarmOrchConfig(
            channel=channel,
            cwd=str(tmp_path),
            session_root=tmp_path / "sessions",
            model="claude-fable-5",
            system_prompt="orch profile",
        ),
        dispatcher=StubSeatDispatcher(),
        evidence_resolver=AlwaysResolvable(),
        client_factory=factory,
    )
    return runner, factory


# ---------------------------------------------------------------- options


def test_first_connect_has_no_resume(tmp_path):
    runner, _ = _runner(tmp_path)
    assert runner.build_options().resume is None


def test_persisted_channel_session_id_is_resumed(tmp_path):
    runner, _ = _runner(tmp_path)
    id_path = tmp_path / "sessions" / "test-channel" / "last-session-id"
    id_path.parent.mkdir(parents=True)
    id_path.write_text("sess-prior\n")
    assert runner.build_options().resume == "sess-prior"


def test_options_carry_cwd_model_and_system_prompt(tmp_path):
    runner, _ = _runner(tmp_path)
    options = runner.build_options()
    assert options.cwd == str(tmp_path)
    assert options.model == "claude-fable-5"
    assert options.system_prompt == "orch profile"


def test_options_wire_merge_close_gate_into_pre_tool_use_hooks(tmp_path):
    runner, _ = _runner(tmp_path)
    options = runner.build_options()
    matchers = options.hooks["PreToolUse"]
    assert any(m.hooks for m in matchers)


def test_wired_gate_actually_denies_unresolved_merge(tmp_path):
    class NeverResolvable:
        def resolve(self, tool_name, tool_input):
            return EvidenceCheck(resolvable=False, detail="nothing on record")

    factory = FakeClientFactory()
    runner = WarmOrchRunner(
        WarmOrchConfig(
            channel="c",
            cwd=str(tmp_path),
            session_root=tmp_path / "s",
        ),
        dispatcher=StubSeatDispatcher(),
        evidence_resolver=NeverResolvable(),
        client_factory=factory,
    )
    options = runner.build_options()
    gate = options.hooks["PreToolUse"][0].hooks[0]
    out = asyncio.run(
        gate(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git merge topic"},
                "tool_use_id": "t1",
            },
            "t1",
            None,
        )
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "merge-close-evidence-unresolved" in (
        out["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_options_expose_dispatch_seat_via_mcp_server(tmp_path):
    runner, _ = _runner(tmp_path)
    options = runner.build_options()
    assert "arb_orch" in options.mcp_servers
    assert "mcp__arb_orch__dispatch_seat" in options.allowed_tools


def test_options_use_file_session_store_under_channel_root(tmp_path):
    runner, _ = _runner(tmp_path)
    options = runner.build_options()
    assert isinstance(options.session_store, FileSessionStore)


# ------------------------------------------------------------------ turns


def test_turn_returns_assistant_text(tmp_path):
    runner, _ = _runner(tmp_path)
    reply = asyncio.run(runner.turn("hello"))
    assert reply == "warm reply"


def test_turn_persists_result_session_id_for_the_channel(tmp_path):
    runner, factory = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    persisted = (tmp_path / "sessions" / "test-channel" / "last-session-id").read_text()
    assert persisted.strip() == "sess-fake-1"


def test_next_process_resumes_the_persisted_session(tmp_path):
    runner, _ = _runner(tmp_path)
    asyncio.run(runner.turn("hello"))
    fresh_runner, fresh_factory = _runner(tmp_path)
    asyncio.run(fresh_runner.turn("again"))
    assert fresh_factory.clients[0].options.resume == "sess-fake-1"


def test_one_client_serves_consecutive_turns(tmp_path):
    runner, factory = _runner(tmp_path)
    asyncio.run(runner.turn("one"))
    asyncio.run(runner.turn("two"))
    assert len(factory.clients) == 1
    assert len(factory.clients[0].queries) == 2


# --------------------------------------------------------- streaming seam
#
# `turn()` is a CONSUMER of this stream, not its sibling (warm-orch-sdk-log
# entry 28): two code paths over one protocol drift, and the session-id
# persistence on ResultMessage is exactly the kind of step that gets forgotten
# in one of them. The existing turn tests above are the regression net for
# that refactor — they must keep passing untouched.


async def _collect(stream) -> list:
    return [event async for event in stream]


def test_stream_turn_yields_a_text_delta_for_each_assistant_text_block(tmp_path):
    runner, _ = _runner(tmp_path)
    events = asyncio.run(_collect(runner.stream_turn("hello")))
    assert TextDelta(text="warm reply") in events


def test_turn_returns_text_when_the_turn_also_used_tools(tmp_path):
    """`turn()` consumes a stream that is no longer text-only.

    Regression guard for the stream_turn refactor: every existing turn test
    scripts text blocks alone, so none of them can see a non-text event reach
    the text join.
    """
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Bash", input={"command": "ls"})],
                model="m",
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="tu-1", content="ok")]),
            AssistantMessage(content=[TextBlock(text="done")], model="m"),
            _result_message("sess-fake-1"),
        ],
    )
    assert asyncio.run(runner.turn("go")) == "done"


def _scripted_runner(tmp_path: Path, messages: list) -> WarmOrchRunner:
    return WarmOrchRunner(
        WarmOrchConfig(channel="c", cwd=str(tmp_path), session_root=tmp_path / "s"),
        dispatcher=StubSeatDispatcher(),
        evidence_resolver=AlwaysResolvable(),
        client_factory=FakeClientFactory(scripted=messages),
    )


def test_stream_turn_reports_a_tool_call_starting(tmp_path):
    """Load-bearing, not cosmetic (internal orchestration log).

    buzz's idle deadline is 900s and resets on `tool_call` updates; a seat
    dispatch is allowed 1800s. Without a start event the client sees silence
    and kills any dispatch past fifteen minutes.
    """
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Bash", input={"command": "ls"})],
                model="m",
            ),
            _result_message("sess-fake-1"),
        ],
    )
    events = asyncio.run(_collect(runner.stream_turn("go")))
    assert (
        ToolCallStarted(
            tool_call_id="tu-1", title="Bash", kind="execute", command="ls"
        )
        in events
    )


def test_stream_turn_reports_a_tool_call_finishing(tmp_path):
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Bash", input={"command": "ls"})],
                model="m",
            ),
            UserMessage(content=[ToolResultBlock(tool_use_id="tu-1", content="ok")]),
            _result_message("sess-fake-1"),
        ],
    )
    events = asyncio.run(_collect(runner.stream_turn("go")))
    assert (
        ToolCallCompleted(tool_call_id="tu-1", status="completed", output="ok")
        in events
    )


def test_a_failed_tool_call_is_not_reported_as_completed(tmp_path):
    """`is_error` must reach the client. Reporting every result as
    `completed` would make a failing tool indistinguishable from a working
    one in the cockpit."""
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Bash", input={"command": "x"})],
                model="m",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="tu-1", content="boom", is_error=True)
                ]
            ),
            _result_message("sess-fake-1"),
        ],
    )
    events = asyncio.run(_collect(runner.stream_turn("go")))
    assert (
        ToolCallCompleted(tool_call_id="tu-1", status="failed", output="boom") in events
    )


def test_tool_preview_carries_command_and_output(tmp_path):
    """The activity view renders a tool call from these two fields; before
    2026-08-07 both were dropped at this seam and the panel could only ever
    show a bare tool name."""
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="tu-1", name="Bash", input={"command": "sqlcmd -Q 'x'"}
                    )
                ],
                model="m",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="tu-1", content="row1\nrow2")]
            ),
            _result_message("sess-fake-1"),
        ],
    )
    events = asyncio.run(_collect(runner.stream_turn("go")))
    started = next(e for e in events if isinstance(e, ToolCallStarted))
    finished = next(e for e in events if isinstance(e, ToolCallCompleted))
    assert started.command == "sqlcmd -Q 'x'"
    assert finished.output == "row1\nrow2"
    assert finished.output_dropped_lines == 0


def test_oversized_tool_output_is_clipped_with_a_visible_dropped_count(tmp_path):
    """A sqlcmd dump must not evict the rest of the transcript (18 KB artifact
    ceiling downstream), and what is cut must be countable rather than silent."""
    runner = _scripted_runner(
        tmp_path,
        [
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Bash", input={"command": "d"})],
                model="m",
            ),
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tu-1",
                        content="\n".join(f"row {i}" for i in range(500)),
                    )
                ]
            ),
            _result_message("sess-fake-1"),
        ],
    )
    events = asyncio.run(_collect(runner.stream_turn("go")))
    finished = next(e for e in events if isinstance(e, ToolCallCompleted))
    assert len(finished.output.splitlines()) == 40
    assert finished.output_dropped_lines == 460


def test_options_grant_the_orchestrator_working_toolset(tmp_path):
    runner, _ = _runner(tmp_path)
    allowed = runner.build_options().allowed_tools
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep"):
        assert tool in allowed
    assert "mcp__arb_orch__dispatch_seat" in allowed
