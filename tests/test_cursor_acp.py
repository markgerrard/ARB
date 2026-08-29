import io
import json
import os
import queue
import threading
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = iter(lines or [])
        self.stderr = iter([])
        self.terminated = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return self.returncode


class QueueOnJoinThread:
    def __init__(self, eng: CursorAcpEngine, message: dict) -> None:
        self.eng = eng
        self.message = message

    def join(self, timeout: float | None = None) -> None:
        self.eng.messages.put(self.message)


class CursorAcpStartTest(unittest.TestCase):
    def _engine(self) -> tuple[CursorAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def _queue_responses(self, eng: CursorAcpEngine, responses: list[dict]) -> None:
        for r in responses:
            eng.messages.put(r)

    def test_command_args(self) -> None:
        eng, _ = self._engine()
        self.assertEqual(eng.command_args(), ["agent", "acp"])

    def test_default_init_timeout_is_60(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            eng = CursorAcpEngine(cwd="/tmp/c", model=None)

        self.assertEqual(eng._init_timeout, 60)

    def test_initialize_uses_init_timeout(self) -> None:
        fake = FakeProcess()
        with mock.patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "0"}, clear=True):
            eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=lambda *a, **k: fake)
            with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
                with self.assertRaisesRegex(EngineError, "initialize timed out after 0s"):
                    eng.start()

    def test_start_initializes_session(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123"}},
        ])
        eng.start()
        self.assertEqual(eng.session_id, "sess-123")
        # Verify initialize was sent
        init_msg = json.loads(fake.stdin.lines[0])
        self.assertEqual(init_msg["method"], "initialize")
        self.assertEqual(init_msg["params"]["protocolVersion"], 1)
        self.assertTrue(init_msg["params"]["clientCapabilities"]["_meta"]["parameterizedModelPicker"])

    def test_start_sets_model_when_provided(self) -> None:
        eng, fake = self._engine()
        eng.model = "claude-sonnet-4-6"
        full_id = "claude-sonnet-4-6[thinking=true,context=200k,effort=medium]"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123", "models": {
                "currentModelId": "default[]",
                "availableModels": [
                    {"modelId": "default[]", "name": "Auto"},
                    {"modelId": full_id, "name": "claude-sonnet-4-6"},
                ],
            }}},
            {"jsonrpc": "2.0", "id": 4, "result": {}},
        ])
        eng.start()
        # Exactly one set_model, resolved deterministically to the full bracketed id.
        self.assertEqual(len(fake.stdin.lines), 4)
        set_model_msg = json.loads(fake.stdin.lines[3])
        self.assertEqual(set_model_msg["method"], "session/set_model")
        self.assertEqual(set_model_msg["params"]["modelId"], full_id)

    def test_start_raises_when_model_unresolvable(self) -> None:
        # Bare name not in availableModels and not bracketed -> explicit operator pin fails loud.
        eng, fake = self._engine()
        eng.model = "nonexistent-model"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123", "models": {
                "availableModels": [{"modelId": "default[]", "name": "Auto"}],
            }}},
        ])
        with self.assertRaisesRegex(EngineError, "could not resolve model"):
            eng.start()
        methods = [json.loads(line).get("method") for line in fake.stdin.lines]
        self.assertNotIn("session/set_model", methods)

    def test_start_raises_when_set_model_errors(self) -> None:
        eng, _ = self._engine()
        eng.model = "default"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123", "models": {
                "availableModels": [{"modelId": "default[]", "name": "default"}],
            }}},
            {"jsonrpc": "2.0", "id": 4, "error": {"code": -32603, "message": "bad model"}},
        ])
        with self.assertRaisesRegex(EngineError, "set_model"):
            eng.start()

    def test_start_sets_fast_false_by_default_when_available(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {
                "sessionId": "sess-123",
                "configOptions": [{"id": "fast", "currentValue": "true"}],
            }},
            {"jsonrpc": "2.0", "id": 4, "result": {"configOptions": [{"id": "fast", "currentValue": "false"}]}},
        ])
        eng.start()
        set_config_msg = json.loads(fake.stdin.lines[3])
        self.assertEqual(set_config_msg["method"], "session/set_config_option")
        self.assertEqual(set_config_msg["params"], {"sessionId": "sess-123", "configId": "fast", "value": "false"})

    def test_start_sets_fast_true_when_requested(self) -> None:
        fake = FakeProcess()
        eng = CursorAcpEngine(cwd="/tmp/c", model=None, fast=True, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {
                "sessionId": "sess-123",
                "configOptions": [{"id": "fast", "currentValue": "false"}],
            }},
            {"jsonrpc": "2.0", "id": 4, "result": {"configOptions": [{"id": "fast", "currentValue": "true"}]}},
        ])
        eng.start()
        set_config_msg = json.loads(fake.stdin.lines[3])
        self.assertEqual(set_config_msg["params"]["value"], "true")

    def test_start_skips_fast_config_when_not_offered(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123", "configOptions": []}},
        ])
        eng.start()
        methods = [json.loads(line)["method"] for line in fake.stdin.lines]
        self.assertNotIn("session/set_config_option", methods)

    def test_start_logs_but_does_not_raise_when_fast_config_fails(self) -> None:
        eng, _ = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {
                "sessionId": "sess-123",
                "configOptions": [{"id": "fast", "currentValue": "true"}],
            }},
            {"jsonrpc": "2.0", "id": 4, "error": {"code": -32603, "message": "fast failed"}},
        ])
        with self.assertLogs("agent_redis_bridge.engines.cursor_acp", level="WARNING") as logs:
            eng.start()
        self.assertIn("set_config_option", "\n".join(logs.output))
        self.assertEqual(eng.session_id, "sess-123")

    def test_start_raises_when_session_missing(self) -> None:
        eng, _ = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": None}},
        ])
        with self.assertRaises(Exception) as ctx:
            eng.start()
        self.assertIn("sessionId", str(ctx.exception))


class CursorAcpTurnTest(unittest.TestCase):
    def _engine(self) -> tuple[CursorAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        eng.session_id = "sess-123"
        return eng, fake

    def test_run_turn_success(self) -> None:
        eng, fake = self._engine()
        events: list[tuple[str, dict]] = []

        def on_event(name: str, data: dict) -> None:
            events.append((name, data))

        # run_turn_with_progress calls set_session_mode_for_policy first,
        # which sends session/set_mode and waits for its response.
        # next_id starts at 1, so set_mode gets id=1, prompt gets id=2.
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        # Queue streaming chunks then the prompt response
        eng.messages.put({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-123",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello"}},
            },
        })
        eng.messages.put({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-123",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " world"}},
            },
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Say hi", timeout=10, policy="trusted", on_event=on_event)

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "Hello world")
        self.assertEqual(
            [e[0] for e in events],
            ["turn_started", "model_text", "model_text", "turn_completed"],
        )
        self.assertEqual(events[-1][1]["stop_reason"], "end_turn")
        self.assertTrue(events[-1][1]["ok"])

    def test_run_turn_error_stop_reason(self) -> None:
        eng, _ = self._engine()
        # set_mode response (id=1), prompt response (id=2)
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "error"}})

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("error", result.error.lower())

    def test_run_turn_refusal_stop_reason_fails(self) -> None:
        eng, _ = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertEqual(result.stop_reason, "refusal")

    def test_run_turn_null_prompt_result_fails_and_emits_failed_completion(self) -> None:
        eng, _ = self._engine()
        events: list[tuple[str, dict]] = []
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": None})

        result = eng.run_turn_with_progress(
            "Do X", timeout=10, policy="trusted", on_event=lambda n, d: events.append((n, d))
        )

        self.assertFalse(result.ok)
        self.assertIn("malformed", result.error)
        self.assertEqual(events[-1][0], "turn_completed")
        self.assertFalse(events[-1][1]["ok"])

    def test_run_turn_process_death_fails_promptly(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        fake.returncode = 1

        result = eng.run_turn_with_progress("Do X", timeout=0.2, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("process exited", result.error)

    def test_run_turn_prefers_queued_final_response_over_process_death(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        fake.returncode = 1

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="trusted", on_event=None)

        self.assertTrue(result.ok)

    def test_run_turn_uses_reader_thread_last_chance_after_process_death(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        fake.returncode = 1
        eng.reader_thread = QueueOnJoinThread(eng, {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do X", timeout=0.2, policy="trusted", on_event=None)

        self.assertTrue(result.ok)

    def test_run_turn_timeout_cancels_prompt_and_marks_engine_unhealthy(self) -> None:
        eng, fake = self._engine()
        # set_mode response must be queued or the set_mode call itself times out first
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        result = eng.run_turn_with_progress("Do X", timeout=0, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        self.assertFalse(eng.healthy)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertIn(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "sess-123"}},
            sent,
        )

    def test_run_turn_empty_success_returns_empty_result(self) -> None:
        eng, _ = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "")
        self.assertEqual(result.stop_reason, "end_turn")

    def test_tool_call_events(self) -> None:
        eng, _ = self._engine()
        events: list[tuple[str, dict]] = []

        def on_event(name: str, data: dict) -> None:
            events.append((name, data))

        # set_mode response (id=1), prompt response (id=2)
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-123",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "ls",
                    "kind": "execute",
                    "status": "pending",
                },
            },
        })
        eng.messages.put({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-123",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-1",
                    "status": "completed",
                    "rawOutput": {"exitCode": 0, "stdout": "foo\n"},
                },
            },
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("List files", timeout=10, policy="trusted", on_event=on_event)

        self.assertTrue(result.ok)
        self.assertEqual(
            [e[0] for e in events],
            ["turn_started", "command_started", "command_finished", "turn_completed"],
        )
        self.assertEqual(events[1][1]["command"], "ls")
        self.assertEqual(events[1][1]["kind"], "command_started")
        self.assertEqual(events[2][1]["command"], "ls")
        self.assertEqual(events[2][1]["kind"], "command_finished")
        self.assertEqual(events[2][1]["exit_code"], 0)

    def test_tool_call_started_deduped(self) -> None:
        # Cursor sends pending -> in_progress -> completed for one tool call;
        # only ONE command_started should surface (matching one command_finished).
        eng, _ = self._engine()
        events: list[tuple[str, dict]] = []
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})  # set_mode
        for status in ("pending", "in_progress"):
            eng.messages.put({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": "sess-123", "update": {
                    "sessionUpdate": "tool_call", "toolCallId": "tc-9", "title": "echo",
                    "kind": "execute", "status": status,
                }},
            })
        eng.messages.put({
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "sess-123", "update": {
                "sessionUpdate": "tool_call_update", "toolCallId": "tc-9", "status": "completed",
            }},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        eng.run_turn_with_progress("go", timeout=10, policy="trusted",
                                   on_event=lambda n, d: events.append((n, d)))
        names = [e[0] for e in events]
        self.assertEqual(names.count("command_started"), 1)
        self.assertEqual(names.count("command_finished"), 1)

    def test_completed_only_tool_call_counts_as_tool_call(self) -> None:
        eng, _ = self._engine()
        events: list[tuple[str, dict]] = []
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        eng.messages.put({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-123",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-completed",
                    "status": "completed",
                },
            },
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress(
            "go", timeout=10, policy="trusted", on_event=lambda n, d: events.append((n, d))
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual([name for name, _ in events], ["turn_started", "command_finished", "turn_completed"])

    def _client_request_turn(self, eng, client_request: dict) -> list[dict]:
        # set_mode takes id 7, the client request arrives mid-turn, the prompt
        # takes id 8 and is answered so the turn completes promptly (no timeout).
        eng.next_id = 7
        eng.messages.put({"jsonrpc": "2.0", "id": 7, "result": {}})
        eng.messages.put(client_request)
        eng.messages.put({"jsonrpc": "2.0", "id": 8, "result": {"stopReason": "end_turn"}})
        fake = eng.process
        result = eng.run_turn_with_progress("Do X", timeout=10, policy="trusted", on_event=None)
        self.assertTrue(result.ok)
        return [json.loads(line) for line in fake.stdin.lines]

    def test_permission_request_auto_approved(self) -> None:
        eng, _ = self._engine()
        responses = self._client_request_turn(eng, {
            "jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
            "params": {"sessionId": "sess-123", "toolCallId": "tc-1", "options": [
                {"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
            ]},
        })
        perm_resp = [r for r in responses if r.get("id") == 99]
        self.assertEqual(len(perm_resp), 1)
        self.assertEqual(perm_resp[0]["result"]["outcome"]["optionId"], "allow-once")

    def test_permission_echoes_offered_allow_id(self) -> None:
        # Non-tautological: the allow option has an arbitrary id; the engine must
        # echo what the server offered, not a hardcoded "allow-once".
        eng, _ = self._engine()
        responses = self._client_request_turn(eng, {
            "jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
            "params": {"options": [
                {"optionId": "proceed-xyz", "kind": "allow_always", "name": "Always allow"},
                {"optionId": "stop", "kind": "reject_once", "name": "Reject"},
            ]},
        })
        perm_resp = [r for r in responses if r.get("id") == 99][0]
        self.assertEqual(perm_resp["result"]["outcome"]["optionId"], "proceed-xyz")

    def test_permission_no_allow_option_cancels(self) -> None:
        eng, _ = self._engine()
        responses = self._client_request_turn(eng, {
            "jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
            "params": {"options": [{"optionId": "reject", "kind": "reject_once"}]},
        })
        perm_resp = [r for r in responses if r.get("id") == 99][0]
        self.assertEqual(perm_resp["result"]["outcome"]["outcome"], "cancelled")

    def test_human_policy_uses_ask_mode_and_cancels_permission(self) -> None:
        eng, fake = self._engine()
        eng.next_id = 7
        eng.messages.put({"jsonrpc": "2.0", "id": 7, "result": {}})
        eng.messages.put({
            "jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
            "params": {"options": [{"optionId": "allow-once", "kind": "allow_once"}]},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 8, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="human", on_event=None)

        self.assertTrue(result.ok)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["params"]["modeId"], "ask")
        perm_resp = [r for r in sent if r.get("id") == 99][0]
        self.assertEqual(perm_resp["result"]["outcome"]["outcome"], "cancelled")
        self.assertNotIn("optionId", perm_resp["result"]["outcome"])

    def test_cursor_ask_question_cancelled(self) -> None:
        eng, _ = self._engine()
        responses = self._client_request_turn(eng, {
            "jsonrpc": "2.0", "id": 88, "method": "cursor/ask_question",
            "params": {"toolCallId": "tc-1", "questions": []},
        })
        ask_resp = [r for r in responses if r.get("id") == 88]
        self.assertEqual(len(ask_resp), 1)
        self.assertEqual(ask_resp[0]["result"]["outcome"]["outcome"], "cancelled")

    def test_cursor_create_plan_accepted(self) -> None:
        eng, _ = self._engine()
        responses = self._client_request_turn(eng, {
            "jsonrpc": "2.0", "id": 87, "method": "cursor/create_plan",
            "params": {"toolCallId": "tc-1", "plan": "do X", "todos": []},
        })
        plan_resp = [r for r in responses if r.get("id") == 87]
        self.assertEqual(len(plan_resp), 1)
        self.assertEqual(plan_resp[0]["result"]["outcome"]["outcome"], "accepted")

    def test_human_policy_cancels_plan(self) -> None:
        eng, fake = self._engine()
        eng.next_id = 7
        eng.messages.put({"jsonrpc": "2.0", "id": 7, "result": {}})
        eng.messages.put({
            "jsonrpc": "2.0", "id": 87, "method": "cursor/create_plan",
            "params": {"toolCallId": "tc-1", "plan": "do X", "todos": []},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 8, "result": {"stopReason": "end_turn"}})

        result = eng.run_turn_with_progress("Do X", timeout=10, policy="human", on_event=None)

        self.assertTrue(result.ok)
        plan_resp = [json.loads(line) for line in fake.stdin.lines if json.loads(line).get("id") == 87][0]
        self.assertEqual(plan_resp["result"]["outcome"]["outcome"], "cancelled")

    def test_pre_turn_permission_request_cancels_by_default(self) -> None:
        eng, fake = self._engine()
        eng.next_id = 2
        eng.messages.put({
            "jsonrpc": "2.0", "id": 1, "method": "session/request_permission",
            "params": {"options": [{"optionId": "allow-once", "kind": "allow_once"}]},
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {}})

        result = eng.request("ping", {}, timeout=10, allow_empty_result=True)

        self.assertEqual(result, {})
        perm_resp = [
            json.loads(line)
            for line in fake.stdin.lines
            if json.loads(line).get("id") == 1 and "result" in json.loads(line)
        ][0]
        self.assertEqual(perm_resp["result"]["outcome"]["outcome"], "cancelled")

    def test_request_process_death_raises_promptly(self) -> None:
        eng, fake = self._engine()
        fake.returncode = 1

        with self.assertRaisesRegex(EngineError, "process exited"):
            eng.request("ping", {}, timeout=0.2)


class CursorAcpNormalizeTest(unittest.TestCase):
    def test_agent_message_chunk_to_model_text(self) -> None:
        event = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
        result = CursorAcpEngine.__module__
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        self.assertEqual(normalize_session_update(event), ("model_text", {"delta": "hi"}))

    def test_agent_thought_chunk_to_model_thinking(self) -> None:
        event = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}}
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        self.assertEqual(normalize_session_update(event), ("model_thinking", {"delta": "thinking"}))

    def test_tool_call_to_command_started(self) -> None:
        event = {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "ls",
            "kind": "execute",
            "status": "pending",
        }
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        name, data = normalize_session_update(event)
        self.assertEqual(name, "command_started")
        self.assertEqual(data["command"], "ls")
        self.assertNotIn("kind", data)

    def test_tool_call_update_completed(self) -> None:
        titles = {"tc-1": "ls"}
        event = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "kind": "execute",
        }
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        name, data = normalize_session_update(event, titles)
        self.assertEqual(name, "command_finished")
        self.assertEqual(data["exit_code"], 0)
        self.assertNotIn("kind", data)

    def test_tool_call_update_completed_diff_includes_path(self) -> None:
        path = "/abs/path/to/hello.txt"
        titles = {"tc-1": "Edit File"}
        event = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "content": [
                {
                    "type": "diff",
                    "path": path,
                    "oldText": "-- /dev/null\n",
                    "newText": "++ b//abs/path/to/hello.txt\nhello world",
                }
            ],
        }
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        name, data = normalize_session_update(event, titles)
        self.assertEqual(name, "command_finished")
        self.assertIn(path, data["command"])
        self.assertEqual(data["path"], path)

    def test_tool_call_update_completed_without_diff_path_is_unchanged(self) -> None:
        titles = {"tc-1": "Edit File"}
        event = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "content": [{"type": "text", "text": "done"}],
        }
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        name, data = normalize_session_update(event, titles)
        self.assertEqual(name, "command_finished")
        self.assertEqual(data["command"], "Edit File")
        self.assertNotIn("path", data)

    def test_tool_call_update_failed(self) -> None:
        titles = {"tc-1": "ls"}
        event = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "failed",
        }
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        name, data = normalize_session_update(event, titles)
        self.assertEqual(name, "command_finished")
        self.assertEqual(data["exit_code"], 1)

    def test_unknown_update(self) -> None:
        event = {"sessionUpdate": "something_new"}
        from agent_redis_bridge.engines.cursor_acp import normalize_session_update
        self.assertEqual(normalize_session_update(event), ("session_update_unknown", {"sessionUpdate": "something_new"}))


class CursorAcpResolveModelTest(unittest.TestCase):
    def _engine(self, available) -> CursorAcpEngine:
        eng = CursorAcpEngine(cwd="/tmp", model=None, popen_factory=lambda *a, **k: None)
        eng.available_models = available
        return eng

    AVAIL = [
        {"modelId": "default[]", "name": "Auto"},
        {"modelId": "claude-sonnet-4-6[thinking=true,context=200k,effort=medium]", "name": "claude-sonnet-4-6"},
    ]

    def test_resolve_by_bare_name(self) -> None:
        eng = self._engine(self.AVAIL)
        self.assertEqual(
            eng._resolve_model_id("claude-sonnet-4-6"),
            "claude-sonnet-4-6[thinking=true,context=200k,effort=medium]",
        )

    def test_resolve_by_exact_model_id(self) -> None:
        eng = self._engine(self.AVAIL)
        full = "claude-sonnet-4-6[thinking=true,context=200k,effort=medium]"
        self.assertEqual(eng._resolve_model_id(full), full)

    def test_resolve_unlisted_bracketed_id_passthrough(self) -> None:
        eng = self._engine(self.AVAIL)
        self.assertEqual(eng._resolve_model_id("some-future-model[fast=true]"), "some-future-model[fast=true]")

    def test_resolve_unknown_bare_returns_none(self) -> None:
        eng = self._engine(self.AVAIL)
        self.assertIsNone(eng._resolve_model_id("not-a-real-model"))


class CursorAcpSelectAllowTest(unittest.TestCase):
    def _select(self, options):
        from agent_redis_bridge.engines.cursor_acp import _select_allow_option
        return _select_allow_option({"options": options})

    def test_prefers_allow_once_over_allow_always(self) -> None:
        self.assertEqual(self._select([
            {"optionId": "a2", "kind": "allow_always"},
            {"optionId": "a1", "kind": "allow_once"},
        ]), "a1")

    def test_falls_back_to_option_id_substring(self) -> None:
        # No `kind` -> match an allow-ish optionId (covers cursor's "allow-once").
        self.assertEqual(self._select([
            {"optionId": "allow-once"},
            {"optionId": "reject"},
        ]), "allow-once")

    def test_does_not_match_name_substring(self) -> None:
        # Free-text name is not consulted: "shallow" contains "allow" but must not match.
        self.assertIsNone(self._select([
            {"optionId": "opt-1", "name": "shallow copy"},
            {"optionId": "opt-2", "name": "Reject"},
        ]))

    def test_rejects_negative_allow_substrings(self) -> None:
        # "disallow" and "do not allow" contain "allow" but are negatives.
        self.assertIsNone(self._select([{"optionId": "disallow"}]))
        self.assertIsNone(self._select([{"optionId": "block", "kind": "do_not_allow"}]))

    def test_none_when_no_allow(self) -> None:
        self.assertIsNone(self._select([{"optionId": "no", "kind": "reject_once"}]))

    def test_none_when_options_missing(self) -> None:
        from agent_redis_bridge.engines.cursor_acp import _select_allow_option
        self.assertIsNone(_select_allow_option({}))


if __name__ == "__main__":
    unittest.main()


class IdCollisionTests(unittest.TestCase):
    """JSON-RPC ids are per-side namespaces: the agent's own outbound request ids can
    collide with the bridge's prompt id. Root cause of the 2026-07-07 'null/malformed
    prompt result' failures — an agent session/request_permission whose id equaled the
    prompt id was mistaken for the prompt response, killing complete turns mid-git-flow."""

    def _engine(self) -> tuple[CursorAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        eng.session_id = "sess-123"
        return eng, fake

    def test_agent_request_with_prompt_id_is_answered_not_treated_as_response(self) -> None:
        eng, fake = self._engine()
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})  # set_mode ack
        # collision: the AGENT's request arrives with id == our prompt id (2)
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-123",
                "toolCall": {"title": "git diff"},
                "options": [
                    {"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "reject", "kind": "reject_once"},
                ],
            },
        })
        # then the REAL prompt response
        eng.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        result = eng.run_turn_with_progress("Do work", timeout=10, policy="trusted", on_event=None)
        self.assertTrue(result.ok, f"turn failed: {result.error}")
        # the colliding permission request must have been ANSWERED on stdin
        replies = [json.loads(line) for line in fake.stdin.lines if line.strip()]
        answered = [r for r in replies if r.get("id") == 2 and "result" in r and "outcome" in str(r.get("result"))]
        self.assertTrue(answered, f"permission request never answered; stdin: {replies}")

    def test_agent_request_colliding_with_rpc_request_id_is_answered_not_swallowed(self) -> None:
        # The eee0b15 fix covered the prompt loop only; request() (initialize,
        # session/set_mode every turn, ...) still matched on id alone (audit CUR-1).
        # Pre-fix, allow_empty_result callers returned {} "success" off the colliding
        # request and the agent's permission request was never answered.
        eng, fake = self._engine()
        eng.messages.put({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-123",
                "toolCall": {"title": "read file"},
                "options": [
                    {"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "reject", "kind": "reject_once"},
                ],
            },
        })
        eng.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"modeId": "yolo"}})

        result = eng.request(
            "session/set_mode", {"sessionId": "sess-123", "modeId": "yolo"},
            timeout=5, allow_empty_result=True,
        )

        self.assertEqual(result, {"modeId": "yolo"})
        replies = [json.loads(line) for line in fake.stdin.lines if line.strip()]
        answered = [r for r in replies if r.get("id") == 1 and "result" in r and "outcome" in str(r.get("result"))]
        self.assertTrue(answered, f"permission request never answered; stdin: {replies}")


class ReaderThreadHardeningTest(unittest.TestCase):
    """Audit CUR-2 (panel-confirmed): the stdout reader had no exception guard —
    one bad byte (strict decode) killed it silently — and is_healthy() ignored
    reader-thread death, so a deaf engine was recycled to poison the next task."""

    def _engine(self) -> tuple[CursorAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def test_read_stdout_exception_flips_unhealthy_instead_of_silent_thread_death(self) -> None:
        class ExplodingStdout:
            def __iter__(self):
                return self

            def __next__(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        eng, fake = self._engine()
        fake.stdout = ExplodingStdout()

        eng._read_stdout()  # must swallow and mark, not raise out of the thread

        self.assertFalse(eng.healthy)

    def test_is_healthy_false_when_reader_thread_died(self) -> None:
        import threading as _threading

        eng, fake = self._engine()
        dead = _threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        eng.reader_thread = dead

        self.assertFalse(eng.is_healthy())

    def test_start_decodes_stdout_with_replace_errors(self) -> None:
        captured: dict = {}

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        eng = CursorAcpEngine(cwd="/tmp/c", model=None, popen_factory=factory)
        eng.messages = queue.Queue()
        for r in [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "sess-123"}},
        ]:
            eng.messages.put(r)

        eng.start()

        self.assertEqual(captured.get("errors"), "replace")
        self.assertEqual(captured.get("encoding"), "utf-8")
