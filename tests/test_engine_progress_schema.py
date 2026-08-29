from __future__ import annotations

import queue
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from agent_redis_bridge.engines.agy_print import AgyPrintEngine
from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
from agent_redis_bridge.engines.codex import CodexEngine
from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine
from agent_redis_bridge.engines.gemini_acp import GeminiAcpEngine
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine
from test_agy_print import FakeProcess as FakeAgyProcess
from test_pi_sdk import _autoreply as _pi_autoreply
from test_pi_sdk import _make_engine as _make_pi_engine


class FakeCodexEngine(CodexEngine):
    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="never",
            sandbox="workspace-write",
        )
        self.thread_id = "thread-1"
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def _get_message(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None


class FakeAgentClient:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    async def connect(self) -> None:
        return None

    async def query(self, prompt: Any) -> None:
        return None

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _result(session_id: str = "turn-2") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result="done",
        stop_reason="end_turn",
    )


def _assert_progress_fields(data: dict[str, Any], *, turn_id: str, item_id: str, kind: str) -> int:
    assert data["turn_id"] == turn_id
    assert data["item_id"] == item_id
    assert data["kind"] == kind
    assert isinstance(data["seq"], int)
    return data["seq"]


def test_codex_real_run_turn_emits_progress_schema_for_model_and_command() -> None:
    engine = FakeCodexEngine()
    engine.messages.put(
        {
            "method": "item/agentMessage/delta",
            "params": {"turnId": "turn-1", "itemId": "msg-1", "delta": "hello"},
        }
    )
    engine.messages.put(
        {
            "method": "item/started",
            "params": {
                "turnId": "turn-1",
                "item": {"id": "cmd-1", "type": "commandExecution", "command": "pytest"},
            },
        }
    )
    engine.messages.put(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {"turnId": "turn-1", "itemId": "cmd-1", "delta": "ok"},
        }
    )
    engine.messages.put(
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {
                    "id": "cmd-1",
                    "type": "commandExecution",
                    "command": "pytest",
                    "status": "completed",
                    "exitCode": 0,
                    "durationMs": 12,
                },
            },
        }
    )
    engine.messages.put({"method": "turn/completed", "params": {"turnId": "turn-1"}})
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine.run_turn_with_progress(
        "run tests",
        timeout=1,
        policy="trusted",
        on_event=lambda name, data: events.append((name, data)),
    )

    assert result.ok
    model = next(data for name, data in events if name == "model_text")
    started = next(data for name, data in events if name == "command_started")
    output = next(data for name, data in events if name == "command_output")
    finished = next(data for name, data in events if name == "command_finished")
    seqs = [
        _assert_progress_fields(model, turn_id="turn-1", item_id="msg-1", kind="model_text"),
        _assert_progress_fields(started, turn_id="turn-1", item_id="cmd-1", kind="command_started"),
        _assert_progress_fields(output, turn_id="turn-1", item_id="cmd-1", kind="command_output"),
        _assert_progress_fields(finished, turn_id="turn-1", item_id="cmd-1", kind="command_finished"),
    ]
    assert seqs == sorted(seqs)


def test_agent_sdk_emits_synthesized_text_ids_and_tool_ids_with_monotonic_seq() -> None:
    messages = [
        AssistantMessage(
            content=[
                TextBlock(text="hello"),
                ThinkingBlock(thinking="plan", signature="sig"),
                ToolUseBlock(id="tool-1", name="Read", input={}),
                ToolResultBlock(tool_use_id="tool-1", content="done", is_error=False),
            ],
            model="m",
        ),
        _result(),
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        client = FakeAgentClient(messages)
        engine = AgentSdkEngine(
            cwd=".",
            model="minimax-m3",
            tool_ceiling="Read,Write,Bash",
            key="K",
            session_root=Path(temp_dir),
            startup_probe=False,
            client_factory=lambda **_: client,
        )
        engine.start()
        engine._last_session_id = "turn-1"
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            result = engine.run_turn_with_progress(
                "do work",
                timeout=30,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )
        finally:
            engine.stop()

    assert result.ok
    by_name = {name: data for name, data in events if name in {"model_text", "model_thinking", "command_started", "command_output"}}
    seqs = [
        _assert_progress_fields(by_name["model_text"], turn_id="turn-1", item_id="turn-1:text", kind="model_text"),
        _assert_progress_fields(by_name["model_thinking"], turn_id="turn-1", item_id="turn-1:thinking", kind="model_thinking"),
        _assert_progress_fields(by_name["command_started"], turn_id="turn-1", item_id="tool-1", kind="command_started"),
        # ToolResultBlock now emits command_output (the ⎿ body) under a distinct :output item_id.
        _assert_progress_fields(by_name["command_output"], turn_id="turn-1", item_id="tool-1:output", kind="command_output"),
    ]
    assert seqs == sorted(seqs)


def test_acp_call_sites_inject_turn_item_kind_and_seq() -> None:
    cases = [
        (GeminiAcpEngine(cwd="/tmp", model=None), "gemini-session"),
        (CursorAcpEngine(cwd="/tmp", model=None), "cursor-session"),
        (GrokAcpEngine(cwd="/tmp", model=None), "grok-session"),
    ]

    for engine, session_id in cases:
        engine.session_id = session_id
        engine.active_prompt_id = 42
        events: list[tuple[str, dict[str, Any]]] = []
        chunks: list[str] = []
        tool_titles: dict[str, str] = {}
        message = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hello"},
                },
            },
        }
        kwargs: dict[str, Any] = {"on_event": lambda name, data: events.append((name, data)), "chunks": chunks, "tool_titles": tool_titles}
        if isinstance(engine, CursorAcpEngine):
            kwargs["started_tool_calls"] = set()
        engine._handle_client_message(message, **kwargs)

        tool_message = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-1",
                    "title": "ls",
                    "kind": "execute",
                    "status": "pending" if isinstance(engine, CursorAcpEngine) else "in_progress",
                },
            },
        }
        engine._handle_client_message(tool_message, **kwargs)

        model = next(data for name, data in events if name == "model_text")
        command = next(data for name, data in events if name == "command_started")
        seqs = [
            _assert_progress_fields(model, turn_id="42", item_id="42:text", kind="model_text"),
            _assert_progress_fields(command, turn_id="42", item_id="tool-1", kind="command_started"),
        ]
        assert seqs == sorted(seqs)


def test_grok_thought_chunks_emit_model_thinking_without_prefix() -> None:
    engine = GrokAcpEngine(cwd="/tmp", model=None)
    engine.session_id = "grok-session"
    engine.active_prompt_id = 42
    events: list[tuple[str, dict[str, Any]]] = []
    message = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "grok-session",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "thinking..."},
            },
        },
    }

    engine._handle_client_message(
        message,
        on_event=lambda name, data: events.append((name, data)),
        chunks=[],
        tool_titles={},
    )

    assert events
    name, data = events[0]
    assert name == "model_thinking"
    assert data["delta"] == "thinking..."
    _assert_progress_fields(data, turn_id="42", item_id="42:thinking", kind="model_thinking")


def test_pi_sdk_emits_progress_schema_for_model_thinking_and_tool_events() -> None:
    engine, _fake, live = _make_pi_engine()
    _pi_autoreply(_fake, live)
    engine.thread_id = "th_test"
    engine.pi_tools = "read"
    events: list[tuple[str, dict[str, Any]]] = []

    def drive_turn() -> None:
        import time

        time.sleep(0.05)
        live.push({"method": "turn/textDelta", "params": {"turnId": "tn_test", "delta": "Hello"}})
        live.push({"method": "turn/thinkingDelta", "params": {"turnId": "tn_test", "delta": "Plan"}})
        live.push(
            {
                "method": "turn/toolStarted",
                "params": {
                    "turnId": "tn_test",
                    "toolCallId": "tool-1",
                    "toolName": "read",
                    "args": {"path": "src/arb_memory"},
                },
            }
        )
        live.push(
            {
                "method": "turn/toolFinished",
                "params": {
                    "turnId": "tn_test",
                    "toolCallId": "tool-1",
                    "toolName": "read",
                    "result": {"content": "audit.py\nvisibility.py"},
                    "isError": False,
                },
            }
        )
        live.push({"method": "turn/completed", "params": {"turnId": "tn_test", "ok": True, "finalText": "Hello"}})

    import threading

    threading.Thread(target=drive_turn, daemon=True).start()
    try:
        result = engine.run_turn_with_progress(
            "say hi",
            timeout=5,
            policy="trusted",
            on_event=lambda name, data: events.append((name, data)),
        )
    finally:
        live.close()

    assert result.ok
    by_name = {
        name: data
        for name, data in events
        if name in {"model_text", "model_thinking", "command_started", "command_output", "command_finished"}
    }
    seqs = [
        _assert_progress_fields(by_name["model_text"], turn_id="tn_test", item_id="tn_test:text", kind="model_text"),
        _assert_progress_fields(by_name["model_thinking"], turn_id="tn_test", item_id="tn_test:thinking", kind="model_thinking"),
        _assert_progress_fields(by_name["command_started"], turn_id="tn_test", item_id="tool-1", kind="command_started"),
        _assert_progress_fields(by_name["command_output"], turn_id="tn_test", item_id="tool-1:output", kind="command_output"),
        _assert_progress_fields(by_name["command_finished"], turn_id="tn_test", item_id="tool-1", kind="command_finished"),
    ]
    assert seqs == sorted(seqs)
    assert by_name["command_started"]["tool_name"] == "read"
    assert by_name["command_started"]["command"] == 'read {"path":"src/arb_memory"}'
    assert by_name["command_output"]["tool_name"] == "read"
    assert by_name["command_output"]["delta"] == "audit.py\nvisibility.py"
    assert by_name["command_finished"]["tool_name"] == "read"


def test_agy_print_emits_single_model_text_item_for_successful_output(tmp_path) -> None:
    process = FakeAgyProcess(stdout="report text\n", pid=4242)
    # Explicit existing conversations root: the exact event sequence below assumes a
    # proven-light transcript channel; the host-dependent default (~/.gemini/...) makes
    # the engine announce a dark channel on hosts without it (progress_channel event).
    engine = AgyPrintEngine(
        cwd="/tmp/project",
        popen_factory=lambda *args, **kwargs: process,
        conversations_root=tmp_path,
    )
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine.run_turn_with_progress(
        "review",
        timeout=60,
        policy="trusted",
        on_event=lambda name, data: events.append((name, data)),
    )

    assert result.ok
    model = next(data for name, data in events if name == "model_text")
    _assert_progress_fields(model, turn_id="4242", item_id="4242:text", kind="model_text")
    assert model["delta"] == "report text"
    assert [name for name, _ in events] == ["turn_started", "model_text", "turn_completed"]


def test_codex_aggregated_output_becomes_command_output_event() -> None:
    """Codex delivers command output in the completed item's `aggregatedOutput`
    (no outputDelta) — it must surface as a command_output event under a `:output` id."""
    engine = FakeCodexEngine()
    engine.messages.put(
        {
            "method": "item/started",
            "params": {"turnId": "turn-1", "item": {"id": "cmd-9", "type": "commandExecution", "command": "ls"}},
        }
    )
    engine.messages.put(
        {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {
                    "id": "cmd-9",
                    "type": "commandExecution",
                    "command": "ls",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "HELLO\nREADME.md\n",
                },
            },
        }
    )
    engine.messages.put({"method": "turn/completed", "params": {"turnId": "turn-1"}})
    events: list[tuple[str, dict[str, Any]]] = []
    engine.run_turn_with_progress(
        "list files", timeout=1, policy="trusted",
        on_event=lambda name, data: events.append((name, data)),
    )

    outputs = [data for name, data in events if name == "command_output"]
    assert len(outputs) == 1, "aggregatedOutput should yield exactly one command_output event"
    out = outputs[0]
    assert out["delta"] == "HELLO\nREADME.md\n"
    assert out["item_id"] == "cmd-9:output"   # distinct from the command_started id -> own ⎿ row
    assert out["kind"] == "command_output"
    # no aggregatedOutput -> no spurious command_output (covered by the existing test staying green)
