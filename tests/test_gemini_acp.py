import json
import os
import queue
import threading
import time
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.gemini_acp import GeminiAcpEngine, normalize_session_update


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
    def __init__(self, messages: list[dict] | None = None, exit_code: int | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(messages or [])
        self.stderr = FakeStdout([])
        self.terminated = False
        # None => still running. Real Popen always exposes poll(); the ACP base
        # uses it to tell "child died" from "child is quiet", so the fake has to
        # model it or the distinction is untestable.
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True


class GeminiAcpEngineTest(unittest.TestCase):
    def test_default_init_timeout_is_60(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            engine = GeminiAcpEngine(cwd="/tmp/project", model=None)

        self.assertEqual(engine._init_timeout, 60)

    def test_initialize_uses_init_timeout(self) -> None:
        fake = FakeProcess()
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "0"}, clear=True):
            engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
            with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
                with self.assertRaisesRegex(EngineError, "initialize timed out after 0s"):
                    engine.start()

    def test_start_handshakes_and_creates_session(self) -> None:
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-1"}},
            ]
        )
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

        engine.start()

        self.assertEqual(engine.session_id, "session-1")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "initialize")
        self.assertEqual(sent[1]["method"], "session/new")
        self.assertEqual(sent[1]["params"]["cwd"], "/tmp/project")

    def test_reset_context_creates_new_session(self) -> None:
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "session-2"}})

        self.assertEqual(engine.reset_context(), "session-2")

        self.assertEqual(engine.session_id, "session-2")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/new")
        self.assertEqual(sent[0]["params"]["cwd"], "/tmp/project")

    def test_reset_context_reapplies_model(self) -> None:
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model="gemini-pro", popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "session-2"}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {}})

        self.assertEqual(engine.reset_context(), "session-2")

        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/new")
        self.assertEqual(sent[1]["method"], "session/set_model")
        self.assertEqual(sent[1]["params"], {"sessionId": "session-2", "modelId": "gemini-pro"})

    def test_prompt_submission_normalizes_text_and_completion(self) -> None:
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
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
        self.assertEqual(result.result, "hello")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/set_mode")
        self.assertEqual(sent[0]["params"]["modeId"], "yolo")
        self.assertEqual(sent[1]["method"], "session/prompt")
        self.assertEqual(sent[1]["params"]["prompt"][0]["text"], "Say hello")
        model_text = next(data for event, data in events if event == "model_text")
        self.assertEqual(model_text["delta"], "hello")
        self.assertEqual(model_text["turn_id"], "2")
        self.assertEqual(model_text["item_id"], "2:text")
        self.assertEqual(model_text["kind"], "model_text")
        self.assertIsInstance(model_text["seq"], int)
        self.assertTrue(any(event == "turn_completed" for event, _ in events))

    def test_tool_updates_normalize_to_command_events(self) -> None:
        titles: dict[str, str] = {}

        started = normalize_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "status": "in_progress",
                "title": "Shell command",
                "kind": "execute",
            },
            titles,
        )
        finished = normalize_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool-1",
                "status": "completed",
                "kind": "execute",
            },
            titles,
        )

        self.assertEqual(started, ("command_started", {"command": "Shell command", "status": "in_progress", "exit_code": None, "tool_call_id": "tool-1"}))
        self.assertEqual(finished, ("command_finished", {"command": "Shell command", "status": "completed", "exit_code": 0, "tool_call_id": "tool-1"}))

    def test_tool_call_progress_schema_resolves_lifecycle_kind(self) -> None:
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"
        engine.active_prompt_id = 2
        events: list[tuple[str, dict]] = []
        chunks: list[str] = []
        tool_titles: dict[str, str] = {}

        engine._handle_client_message(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tool-1",
                        "status": "in_progress",
                        "title": "Shell command",
                        "kind": "execute",
                    },
                },
            },
            on_event=lambda event, data: events.append((event, data)),
            chunks=chunks,
            tool_titles=tool_titles,
        )

        self.assertEqual(events[0][0], "command_started")
        self.assertEqual(events[0][1]["kind"], "command_started")

    def test_refusal_stop_reason_fails_turn(self) -> None:
        # Mirrors the cursor-acp hardening fix: a refused turn is a failed turn.
        # This base class serves the live kimi-code/mini-agent seats.
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")

    def test_interrupt_sends_session_cancel(self) -> None:
        fake = FakeProcess()
        engine = GeminiAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-1"

        self.assertEqual(engine.interrupt(), "session-1")

        sent = json.loads(fake.stdin.lines[0])
        self.assertEqual(sent["method"], "session/cancel")
        self.assertEqual(sent["params"]["sessionId"], "session-1")
        self.assertNotIn("id", sent)


if __name__ == "__main__":
    unittest.main()


class IdCollisionTests(unittest.TestCase):
    """JSON-RPC ids are per-side namespaces (cursor-acp fix eee0b15; audit LT-1): the
    agent's own outbound request ids can collide with the bridge's request/prompt ids.
    Pre-fix, a colliding client request was consumed as the response — here the non-dict
    result branch reported a SUCCESSFUL early completion while the real prompt was still
    running, and the agent's request was never answered (wedging the child).
    kimi-code-acp and mini-agent-acp inherit this loop."""

    def _engine(self) -> tuple[GeminiAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = GeminiAcpEngine(cwd="/tmp/g", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.session_id = "sess-g"
        return eng, fake

    def _stdin_replies(self, fake: FakeProcess) -> list[dict]:
        return [json.loads(line) for line in fake.stdin.lines if line.strip()]

    def test_agent_request_with_prompt_id_is_answered_not_treated_as_response(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})  # set_mode ack
        # collision: the AGENT's request arrives with id == our prompt id (2)
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/request_permission",
            "params": {"sessionId": "sess-g", "toolCall": {"title": "run tests"}},
        })
        # then the REAL prompt response
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do work", timeout=10, policy="trusted", on_event=None)

        self.assertTrue(result.ok, f"turn failed: {result.error}")
        self.assertNotIn("no result body", result.result)
        answered = [r for r in self._stdin_replies(fake) if r.get("id") == 2 and "result" in r]
        self.assertTrue(answered, f"permission request never answered; stdin: {self._stdin_replies(fake)}")

    def test_agent_request_colliding_with_rpc_request_id_is_answered_not_swallowed(self) -> None:
        eng, fake = self._engine()
        # collision arrives BEFORE the real ack for our request id 1
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {"sessionId": "sess-g", "toolCall": {"title": "read file"}},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"modeId": "yolo"}})

        result = eng.request("session/set_mode", {"sessionId": "sess-g", "modeId": "yolo"}, timeout=5)

        self.assertEqual(result, {"modeId": "yolo"})
        answered = [r for r in self._stdin_replies(fake) if r.get("id") == 1 and "result" in r]
        self.assertTrue(answered, "colliding client request was swallowed instead of answered")


class AcpChildLivenessTest(unittest.TestCase):
    """A dead ACP child must fail fast with its exit code, not time out.

    Backlog item: the base's request()/turn loops had no liveness check, so a CLI
    that exits at spawn (bad flag, missing auth) reported
    `initialize timed out after 60s` — its own explanation stranded in the stderr
    drain. Live specimen: `omp --tools read,grep,find,ls` (pi's vocabulary; omp
    has no find/ls) exits rc=2 instantly and the seat waited the full 60s.
    """

    def _engine(self, fake, timeout_s="60"):
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": timeout_s}, clear=True):
            return GeminiAcpEngine(
                cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake
            )

    def test_dead_child_fails_fast_with_exit_code(self) -> None:
        fake = FakeProcess(exit_code=2)
        engine = self._engine(fake)
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            start = time.monotonic()
            with self.assertRaises(EngineError) as ctx:
                engine.start()
            elapsed = time.monotonic() - start
        msg = str(ctx.exception)
        self.assertIn("exited with code 2", msg)
        self.assertNotIn("timed out", msg)
        # The whole point is not waiting out the 60s budget.
        self.assertLess(elapsed, 15)

    def test_grace_drain_recovers_a_line_the_reader_flushes_after_the_exit(self) -> None:
        """Pin the 0.5s grace drain itself — the thing no other test pins.

        `test_reply_then_exit_is_not_treated_as_death` preloads stdout, so the
        reader has always finished before liveness is consulted; that test
        passes verbatim with the grace drain deleted, which makes it vacuous
        with respect to the grace. Panel finding: verified by mutation, run
        panel-omp-opencode-arc-20260803T125825Z-570c21.

        Here the message lands only AFTER the first empty-queue + dead-child
        observation, which is the only window the grace exists to cover.
        """
        fake = FakeProcess(exit_code=0)
        engine = self._engine(fake)
        engine.process = fake  # type: ignore[assignment]
        engine.messages = queue.Queue()

        late = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}

        def flush_late() -> None:
            # Inside the 0.5s grace, comfortably after the 0.1s poll returns empty.
            time.sleep(0.2)
            engine.messages.put(late)

        timer = threading.Thread(target=flush_late, daemon=True)
        timer.start()
        try:
            message = engine._await_or_detect_death(
                "initialize", deadline=time.monotonic() + 0.1, poll_cap=0.1
            )
        finally:
            timer.join(timeout=2)

        # Without the grace drain this raises instead of returning the message.
        self.assertEqual(message, late)

    def test_live_but_quiet_child_still_times_out(self) -> None:
        # The check must NOT convert ordinary slowness into a spurious death.
        fake = FakeProcess(exit_code=None)
        engine = self._engine(fake, timeout_s="0")
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            with self.assertRaisesRegex(EngineError, "initialize timed out"):
                engine.start()

    def test_reply_then_exit_is_not_treated_as_death(self) -> None:
        # A child that answers and then exits is healthy: the queued reply must
        # be consumed, not discarded in favour of an exit-code error.
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-x"}},
            ],
            exit_code=0,
        )
        engine = self._engine(fake)
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            engine.start()
        self.assertEqual(engine.session_id, "session-x")

    def test_dead_child_mid_turn_fails_without_burning_the_turn_timeout(self) -> None:
        # Two claims in one: the turn must end FAST (not burn the hour-long turn
        # timeout), and it must end as a RETURNED TurnResult carrying whatever
        # streamed before the child died — not a raise that discards it.
        fake = FakeProcess(exit_code=137)
        engine = self._engine(fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-x"
        engine.messages = queue.Queue()
        # set_session_mode_for_policy consumes this one; then the child streams a
        # partial answer and dies silently.
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "session-x",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "half a review"},
                    },
                },
            }
        )
        events: list[tuple[str, dict]] = []
        start = time.monotonic()
        result = engine.run_turn_with_progress(
            "t", timeout=3600, policy="trusted", on_event=lambda e, d: events.append((e, d))
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 15)
        self.assertFalse(result.ok)
        self.assertIn("exited with code 137", result.error or "")
        # The streamed prefix survives the death — this is the whole point.
        self.assertEqual(result.result, "half a review")
        # A terminal progress event still fires, so bridge.py advances the turn
        # index instead of leaving the task hanging on a silent path.
        self.assertIn("turn_completed", [name for name, _ in events])
        self.assertIsNone(engine.active_prompt_id)

    def test_no_process_is_not_reported_as_a_dead_child(self) -> None:
        engine = self._engine(FakeProcess())
        engine.process = None
        self.assertIsNone(engine._dead_child_error("initialize"))
