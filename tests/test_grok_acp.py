import json
import os
import queue
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine, normalize_session_update


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeStdout:
    def __init__(self, messages: list[dict]) -> None:
        self.lines = [json.dumps(message) + "\n" for message in messages]

    def __iter__(self):
        return iter(self.lines)


class FakeProcess:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(messages or [])
        self.stderr = FakeStdout([])
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True


class GrokAcpEngineTest(unittest.TestCase):
    def test_default_init_timeout_is_60(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            engine = GrokAcpEngine(cwd="/tmp/project", model=None)

        self.assertEqual(engine._init_timeout, 60)

    def test_initialize_uses_init_timeout(self) -> None:
        fake = FakeProcess()
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "0"}, clear=True):
            engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
            with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
                with self.assertRaisesRegex(EngineError, "initialize timed out after 0s"):
                    engine.start()

    def test_start_handshakes_and_creates_session(self) -> None:
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "grok-session-1"}},
            ]
        )
        engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

        engine.start()

        self.assertEqual(engine.session_id, "grok-session-1")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "initialize")
        self.assertEqual(sent[1]["method"], "session/new")
        self.assertEqual(sent[1]["params"]["cwd"], "/tmp/project")

    def test_prompt_submission_sets_yolo_for_trusted_and_normalizes_text(self) -> None:
        fake = FakeProcess()
        engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "grok-session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "grok-session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello from grok"},
                    },
                },
            }
        )
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        events: list[tuple[str, dict]] = []

        result = engine.run_turn_with_progress(
            "Say hello",
            timeout=1,
            policy="trusted",
            on_event=lambda event, data: events.append((event, data)),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "hello from grok")
        sent = [json.loads(line) for line in fake.stdin.lines]
        # First real action after start is set_mode for trusted policy
        self.assertEqual(sent[0]["method"], "session/set_mode")
        self.assertEqual(sent[0]["params"]["modeId"], "yolo")
        self.assertEqual(sent[1]["method"], "session/prompt")
        model_text = next(data for event, data in events if event == "model_text")
        self.assertEqual(model_text["delta"], "hello from grok")
        self.assertEqual(model_text["turn_id"], "2")
        self.assertEqual(model_text["item_id"], "2:text")
        self.assertEqual(model_text["kind"], "model_text")
        self.assertIsInstance(model_text["seq"], int)
        self.assertTrue(any(event == "turn_completed" for event, _ in events))

    def test_refusal_stop_reason_fails_turn(self) -> None:
        # Mirrors the cursor-acp hardening fix: a refused turn is a failed turn.
        fake = FakeProcess()
        engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "grok-session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")

    def test_thought_chunks_are_prefixed_and_small_fragments_suppressed(self) -> None:
        fake = FakeProcess()
        engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "grok-session-1"
        engine.messages = queue.Queue()
        # set_mode reply (id=1) then prompt completion (id=2). Update is a notification.
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "grok-session-1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking..."},
                    },
                },
            }
        )
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        events: list[tuple[str, dict]] = []

        result = engine.run_turn_with_progress(
            "think step by step",
            timeout=1,
            policy="human",
            on_event=lambda event, data: events.append((event, data)),
        )

        self.assertTrue(result.ok)
        # Only meaningful thoughts (len > 8 or punctuation) are emitted.
        # T-0 intentionally normalizes Grok thought chunks to model_thinking.
        thinking = next(data for event, data in events if event == "model_thinking")
        self.assertEqual(thinking["delta"], "thinking...")
        self.assertEqual(thinking["turn_id"], "2")
        self.assertEqual(thinking["item_id"], "2:thinking")
        self.assertEqual(thinking["kind"], "model_thinking")
        self.assertIsInstance(thinking["seq"], int)

    def test_tool_call_and_tool_call_update_are_normalized(self) -> None:
        fake = FakeProcess()
        engine = GrokAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "grok-session-1"
        engine.messages = queue.Queue()
        # set_mode reply (id=1) then prompt completion (id=2). Updates are notifications.
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "grok-session-1",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "status": "in_progress",
                        "title": "run pytest",
                        "kind": "execute",
                    },
                },
            }
        )
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "grok-session-1",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "status": "completed",
                        "title": "run pytest",
                    },
                },
            }
        )
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        events: list[tuple[str, dict]] = []

        result = engine.run_turn_with_progress(
            "run tests",
            timeout=1,
            policy="trusted",
            on_event=lambda event, data: events.append((event, data)),
        )

        self.assertTrue(result.ok)
        started = [e for e in events if e[0] == "command_started"]
        finished = [e for e in events if e[0] == "command_finished"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][1]["command"], "run pytest")
        self.assertEqual(started[0][1]["status"], "in_progress")
        self.assertEqual(started[0][1]["kind"], "command_started")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][1]["status"], "completed")
        self.assertEqual(finished[0][1]["kind"], "command_finished")

    def test_tool_updates_normalize_without_raw_kind(self) -> None:
        tool_titles: dict[str, str] = {}

        started = normalize_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "status": "in_progress",
                "title": "run pytest",
                "kind": "execute",
            },
            tool_titles,
        )
        finished = normalize_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool-1",
                "status": "completed",
                "kind": "execute",
            },
            tool_titles,
        )

        self.assertEqual(started, ("command_started", {"command": "run pytest", "status": "in_progress", "exit_code": None, "tool_call_id": "tool-1"}))
        self.assertEqual(finished, ("command_finished", {"command": "run pytest", "status": "completed", "exit_code": 0, "tool_call_id": "tool-1"}))

    def test_normalize_session_update_handles_grok_specific_fields(self) -> None:
        # Direct test of the normalizer for grok-specific update shapes
        tool_titles: dict[str, str] = {}
        event = normalize_session_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "direct delta"},
            },
            tool_titles,
        )
        self.assertEqual(event, ("model_text", {"delta": "direct delta"}))


if __name__ == "__main__":
    unittest.main()


class IdCollisionTests(unittest.TestCase):
    """Same per-side id-namespace bug as cursor-acp eee0b15 / audit LT-1. Pre-fix, a
    colliding agent request hit the non-dict-result branch and raised
    'session/prompt returned non-object result', discarding all streamed chunks and
    leaving the agent's request unanswered."""

    def _engine(self) -> tuple[GrokAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = GrokAcpEngine(cwd="/tmp/gr", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.session_id = "sess-gr"
        return eng, fake

    def _stdin_replies(self, fake: FakeProcess) -> list[dict]:
        return [json.loads(line) for line in fake.stdin.lines if line.strip()]

    def test_agent_request_with_prompt_id_is_answered_not_treated_as_response(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})  # set_mode ack
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/request_permission",
            "params": {"sessionId": "sess-gr", "toolCall": {"title": "git diff"}},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do work", timeout=10, policy="trusted", on_event=None)

        self.assertTrue(result.ok, f"turn failed: {result.error}")
        answered = [r for r in self._stdin_replies(fake) if r.get("id") == 2 and "result" in r]
        self.assertTrue(answered, f"permission request never answered; stdin: {self._stdin_replies(fake)}")

    def test_agent_request_colliding_with_rpc_request_id_is_answered_not_swallowed(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {"sessionId": "sess-gr", "toolCall": {"title": "read file"}},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"modeId": "yolo"}})

        result = eng.request("session/set_mode", {"sessionId": "sess-gr", "modeId": "yolo"}, timeout=5)

        self.assertEqual(result, {"modeId": "yolo"})
        answered = [r for r in self._stdin_replies(fake) if r.get("id") == 1 and "result" in r]
        self.assertTrue(answered, "colliding client request was swallowed instead of answered")


class GrokRequestLivenessTest(unittest.TestCase):
    """`request()` must notice a dead child instead of burning the full timeout.

    The ACP liveness backlog item was closed on the claim that grok "already
    checks poll()". It did — in its TURN loop only. `request()` serves
    `initialize`, `session/new` and `session/set_mode`, and had no liveness path
    at all, so a grok CLI that died at spawn still waited the whole
    BRIDGE_ENGINE_INIT_TIMEOUT_S and reported a timeout. Panel finding P1-2,
    run panel-omp-opencode-arc-20260803T125825Z-570c21.
    """

    class DeadProcess(FakeProcess):
        def __init__(self, exit_code: int) -> None:
            super().__init__()
            self.exit_code = exit_code

        def poll(self) -> int | None:
            return self.exit_code

    def _engine(self, fake) -> GrokAcpEngine:
        engine = GrokAcpEngine(
            cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake
        )
        engine.process = fake  # type: ignore[assignment]
        engine.messages = queue.Queue()
        return engine

    def test_dead_child_surfaces_exit_code_instead_of_timing_out(self) -> None:
        fake = self.DeadProcess(exit_code=2)
        engine = self._engine(fake)

        # timeout=3600 so a pass CANNOT come from the deadline being hit: the
        # only way out in reasonable time is the liveness check.
        with self.assertRaises(EngineError) as ctx:
            engine.request("initialize", {}, timeout=3600)

        message = str(ctx.exception)
        self.assertIn("exited with code 2", message)
        self.assertNotIn("timed out", message)

    def test_reply_already_queued_is_consumed_even_though_child_exited(self) -> None:
        # Answered-then-exited stays healthy: the queued reply wins over the
        # exit code, exactly as in the turn loop.
        fake = self.DeadProcess(exit_code=0)
        engine = self._engine(fake)
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})

        result = engine.request("initialize", {}, timeout=30)

        self.assertEqual(result, {"protocolVersion": 1})

    def test_live_child_is_not_declared_dead(self) -> None:
        # poll() -> None means alive; a quiet live child must still time out
        # rather than be mistaken for a corpse.
        fake = FakeProcess()  # poll() returns None until terminated
        engine = self._engine(fake)

        with self.assertRaises(EngineError) as ctx:
            engine.request("initialize", {}, timeout=1)

        self.assertIn("timed out", str(ctx.exception))
