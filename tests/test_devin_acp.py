import json
import os
import queue
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.devin_acp import DevinAcpEngine, normalize_session_update


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


class DevinAcpStartTest(unittest.TestCase):
    def _engine(self) -> tuple[DevinAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        return eng, fake

    def _queue_responses(self, eng: DevinAcpEngine, responses: list[dict]) -> None:
        for r in responses:
            eng.messages.put(r)

    def test_command_args(self) -> None:
        eng, _ = self._engine()
        self.assertEqual(eng.command_args(), ["devin", "acp"])

    def test_start_initializes_session(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123", "configOptions": []}},
        ])
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            eng.start()
        self.assertEqual(eng.session_id, "sess-123")
        init_msg = json.loads(fake.stdin.lines[0])
        self.assertEqual(init_msg["method"], "initialize")
        self.assertEqual(init_msg["params"]["protocolVersion"], 1)
        session_new_msg = json.loads(fake.stdin.lines[1])
        self.assertEqual(session_new_msg["method"], "session/new")
        self.assertEqual(session_new_msg["params"]["cwd"], "/tmp/d")
        self.assertIsInstance(session_new_msg["params"]["mcpServers"], list)

    def test_start_sets_model_via_config_option(self) -> None:
        eng, fake = self._engine()
        eng.model = "swe-1-7-medium"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "sessionId": "sess-123",
                    "configOptions": [
                        {
                            "id": "model",
                            "options": [
                                {"value": "swe-1-7", "name": "SWE-1.7 Max"},
                                {"value": "swe-1-7-medium", "name": "SWE-1.7 Medium"},
                            ],
                        }
                    ],
                },
            },
            {"jsonrpc": "2.0", "id": 3, "result": {}},
        ])
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            eng.start()
        set_model_msg = json.loads(fake.stdin.lines[2])
        self.assertEqual(set_model_msg["method"], "session/set_config_option")
        self.assertEqual(set_model_msg["params"]["configId"], "model")
        self.assertEqual(set_model_msg["params"]["value"], "swe-1-7-medium")

    def test_start_sets_model_by_name(self) -> None:
        eng, fake = self._engine()
        eng.model = "SWE-1.7 Medium"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "sessionId": "sess-123",
                    "configOptions": [
                        {
                            "id": "model",
                            "options": [
                                {"value": "swe-1-7", "name": "SWE-1.7 Max"},
                                {"value": "swe-1-7-medium", "name": "SWE-1.7 Medium"},
                            ],
                        }
                    ],
                },
            },
            {"jsonrpc": "2.0", "id": 3, "result": {}},
        ])
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            eng.start()
        set_model_msg = json.loads(fake.stdin.lines[2])
        self.assertEqual(set_model_msg["params"]["value"], "swe-1-7-medium")

    def test_start_scrubs_bus_credentials_from_child_env(self) -> None:
        """The devin child must not inherit the bridge's bus credentials."""
        captured: dict = {}

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return fake

        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=factory)
        eng.messages = queue.Queue()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123", "configOptions": []}},
        ])
        polluted = {
            "PATH": "/usr/bin",
            "HOME": "/Users/x",
            "AGENT_REDIS_PASSWORD": "hunter2",
            "ARB_MEMORY_REDIS_URL": "rediss://:secret@bus:6379/9",
            "REDISCLI_AUTH": "hunter2",
        }
        with mock.patch.dict(os.environ, polluted, clear=True):
            with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
                eng.start()
        env = captured.get("env")
        self.assertIsNotNone(env, "start() must pass a scrubbed env to the child")
        self.assertIn("PATH", env)
        self.assertIn("HOME", env)
        self.assertNotIn("AGENT_REDIS_PASSWORD", env)
        self.assertNotIn("ARB_MEMORY_REDIS_URL", env)
        self.assertNotIn("REDISCLI_AUTH", env)

    def test_start_refuses_when_model_unresolvable(self) -> None:
        # Contract flipped by Slice 1h (2026-08-01): unresolvable configured
        # model is a named refusal, not a warn-and-continue fallback.
        eng, fake = self._engine()
        eng.model = "nonexistent-model"
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "sessionId": "sess-123",
                    "configOptions": [
                        {
                            "id": "model",
                            "options": [{"value": "swe-1-7", "name": "SWE-1.7 Max"}],
                        }
                    ],
                },
            },
        ])
        with mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            with self.assertRaises(EngineError) as ctx:
                eng.start()
        self.assertIn("nonexistent-model", str(ctx.exception))
        self.assertIn("refusing", str(ctx.exception))
        self.assertEqual(eng.session_id, "sess-123")
        methods = [json.loads(line).get("method") for line in fake.stdin.lines]
        self.assertNotIn("session/set_config_option", methods)


class DevinAcpTurnTest(unittest.TestCase):
    def _engine(self) -> tuple[DevinAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        eng.session_id = "sess-123"
        eng.next_id = 3
        return eng, fake

    def _queue_responses(self, eng: DevinAcpEngine, responses: list[dict]) -> None:
        for r in responses:
            eng.messages.put(r)

    def test_run_turn_sends_prompt_and_returns_result(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"userMessageId": "msg-1", "stopReason": "end_turn", "usage": {"totalTokens": 10}},
            },
        ])
        result = eng.run_turn_with_progress("do work", timeout=10, policy="trusted", on_event=None)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "")
        self.assertEqual(result.stop_reason, "end_turn")
        set_mode_msg = json.loads(fake.stdin.lines[0])
        self.assertEqual(set_mode_msg["method"], "session/set_config_option")
        self.assertEqual(set_mode_msg["params"]["configId"], "mode")
        self.assertEqual(set_mode_msg["params"]["value"], "bypass")
        prompt_msg = json.loads(fake.stdin.lines[1])
        self.assertEqual(prompt_msg["method"], "session/prompt")

    def test_run_turn_human_policy_uses_accept_edits(self) -> None:
        eng, fake = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"userMessageId": "msg-1", "stopReason": "end_turn"},
            },
        ])
        eng.run_turn_with_progress("do work", timeout=10, policy="human", on_event=None)
        set_mode_msg = json.loads(fake.stdin.lines[0])
        self.assertEqual(set_mode_msg["params"]["value"], "accept-edits")

    def test_run_turn_returns_error_for_failed_stop_reason(self) -> None:
        eng, _ = self._engine()
        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {"jsonrpc": "2.0", "id": 4, "result": {"userMessageId": "msg-1", "stopReason": "refusal"}},
        ])
        result = eng.run_turn_with_progress("do work", timeout=10, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("refusal", result.error or "")

    def test_run_turn_aggregates_text_chunks(self) -> None:
        eng, _ = self._engine()
        events: list[tuple[str, dict]] = []

        def on_event(name: str, data: dict) -> None:
            events.append((name, data))

        self._queue_responses(eng, [
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess-123",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello "},
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sess-123",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world"},
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {"userMessageId": "msg-1", "stopReason": "end_turn"},
            },
        ])
        result = eng.run_turn_with_progress("do work", timeout=10, policy="trusted", on_event=on_event)
        self.assertEqual(result.result, "hello world")
        text_events = [name for name, _ in events if name == "model_text"]
        self.assertEqual(len(text_events), 2)


class DevinAcpRequestPermissionTest(unittest.TestCase):
    def _engine(self) -> DevinAcpEngine:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.session_id = "sess-123"
        eng.messages = queue.Queue()
        return eng

    def _ask(self, *, session_id: str = "sess-123", request_id: int = 42) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "options": [
                    {"optionId": "deny", "kind": "reject_once"},
                    {"optionId": "allow", "kind": "allow_once"},
                ],
            },
        }

    def test_trusted_policy_selects_allow_option(self) -> None:
        eng = self._engine()
        eng._respond_to_client_request(self._ask(), policy="trusted")
        reply = json.loads(eng.process.stdin.lines[0])
        self.assertEqual(reply["id"], 42)
        self.assertEqual(reply["result"]["outcome"]["outcome"], "selected")
        self.assertEqual(reply["result"]["outcome"]["optionId"], "allow")

    def test_human_policy_cancels_permission(self) -> None:
        eng = self._engine()
        eng._respond_to_client_request(self._ask(), policy="human")
        reply = json.loads(eng.process.stdin.lines[0])
        self.assertEqual(reply["result"]["outcome"]["outcome"], "cancelled")

    def test_foreign_session_ask_denied_even_when_trusted(self) -> None:
        """An ask carrying a sessionId other than the engine's current session
        is structurally unauthorizable — deny fail-closed regardless of policy
        (grok D3b parity)."""
        eng = self._engine()
        eng._respond_to_client_request(self._ask(session_id="sess-OTHER"), policy="trusted")
        reply = json.loads(eng.process.stdin.lines[0])
        self.assertEqual(reply["result"]["outcome"]["outcome"], "cancelled")

    def test_inter_turn_ask_denied_by_default(self) -> None:
        """policy=None (no active turn) always denies, even with an allow option."""
        eng = self._engine()
        eng._respond_to_client_request(self._ask(), policy=None)
        reply = json.loads(eng.process.stdin.lines[0])
        self.assertEqual(reply["result"]["outcome"]["outcome"], "cancelled")

    def test_request_path_denies_asks_regardless_of_prior_turn_policy(self) -> None:
        """Authorization is turn-scoped, never engine-state: an ask arriving
        during a plain request() (inter-turn) is denied even if a previous
        trusted turn ran (the sticky-policy P2)."""
        eng = self._engine()
        eng.policy = "trusted"  # simulate residue from a prior trusted turn
        eng.messages.put(self._ask(request_id=99))
        eng.messages.put({"jsonrpc": "2.0", "id": 3, "result": {}})
        eng.next_id = 3
        eng.request("session/set_config_option", {}, timeout=5, allow_empty_result=True)
        ask_reply = json.loads(eng.process.stdin.lines[1])
        self.assertEqual(ask_reply["id"], 99)
        self.assertEqual(ask_reply["result"]["outcome"]["outcome"], "cancelled")


class ScriptedQueue:
    """Serves a fixed script of messages; the EMPTY sentinel raises queue.Empty
    once (simulating the reader-thread race at process exit)."""

    EMPTY = object()

    def __init__(self, script: list) -> None:
        self.script = list(script)

    def get(self, timeout: float | None = None) -> dict:
        if not self.script:
            raise queue.Empty
        item = self.script.pop(0)
        if item is self.EMPTY:
            raise queue.Empty
        return item


class DevinAcpTurnHardeningTest(unittest.TestCase):
    def _engine(self) -> tuple[DevinAcpEngine, FakeProcess]:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        eng.messages = queue.Queue()
        eng.session_id = "sess-123"
        eng.next_id = 3
        return eng, fake

    def _ask(self, request_id: int) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-123",
                "toolCall": {"title": "rm -rf /"},
                "options": [{"optionId": "allow", "kind": "allow_once"}],
            },
        }

    def test_deny_budget_exhaustion_cancels_turn(self) -> None:
        eng, fake = self._engine()
        eng.deny_budget = 2
        eng.messages.put({"jsonrpc": "2.0", "id": 3, "result": {}})  # set-mode reply
        for rid in (100, 101, 102):
            eng.messages.put(self._ask(rid))
        result = eng.run_turn_with_progress("do work", timeout=3, policy="human", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("deny budget", result.error or "")
        self.assertFalse(eng.healthy)
        # every ask still got a fail-closed answer
        cancelled = [
            json.loads(line)
            for line in fake.stdin.lines
            if '"outcome":"cancelled"' in line.replace(" ", "")
        ]
        self.assertEqual({r["id"] for r in cancelled}, {100, 101, 102})

    def test_turn_drains_last_chance_message_after_process_exit(self) -> None:
        """The prompt response can race the process exit: the first poll comes
        up empty while the reader thread is still flushing. The turn loop must
        take the last-chance drain (cursor parity), not report a crash."""
        eng, fake = self._engine()
        fake.returncode = 0  # process already exited
        eng.messages = ScriptedQueue([
            {"jsonrpc": "2.0", "id": 3, "result": {}},  # set-mode reply
            ScriptedQueue.EMPTY,  # first turn-loop poll misses
            {"jsonrpc": "2.0", "id": 4, "result": {"stopReason": "end_turn"}},
        ])
        result = eng.run_turn_with_progress("do work", timeout=3, policy="trusted", on_event=None)
        self.assertTrue(result.ok)
        self.assertEqual(result.stop_reason, "end_turn")

    def test_process_exit_marks_engine_unhealthy(self) -> None:
        eng, fake = self._engine()
        fake.returncode = 1
        eng.messages.put({"jsonrpc": "2.0", "id": 3, "result": {}})  # set-mode reply
        result = eng.run_turn_with_progress("do work", timeout=3, policy="trusted", on_event=None)
        self.assertFalse(result.ok)
        self.assertIn("exited", result.error or "")
        self.assertFalse(eng.healthy)


class DevinAcpNormalizeTest(unittest.TestCase):
    def test_agent_message_chunk(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hi"},
        })
        self.assertEqual(event, ("model_text", {"delta": "hi"}))

    def test_long_thought_emits(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "this is a longer thought."},
        })
        self.assertEqual(event, ("model_thinking", {"delta": "this is a longer thought."}))

    def test_short_thought_suppressed(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "ok"},
        })
        self.assertIsNone(event)

    def test_tool_call(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "git status",
            "status": "in_progress",
        })
        self.assertEqual(event[0], "command_started")
        self.assertEqual(event[1]["tool_call_id"], "tc-1")

    def test_tool_call_completed(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "title": "git status",
            "status": "completed",
        })
        self.assertEqual(event[0], "command_finished")
        self.assertEqual(event[1]["exit_code"], 0)

    def test_session_info_update(self) -> None:
        event = normalize_session_update({
            "sessionUpdate": "session_info_update",
            "title": "my task",
        })
        self.assertEqual(event, ("session_info", {"title": "my task"}))


if __name__ == "__main__":
    unittest.main()


class DevinSetModelNamedRefusalTest(DevinAcpStartTest.__bases__[0]):
    """Slice 1h (ARB-B17 light-path, owner ruling 2026-08-01): a configured
    model that cannot be applied is a NAMED refusal, never a silent fallback
    to the session-default family."""

    def test_configured_model_failure_refuses_named(self) -> None:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model="not-a-real-model", popen_factory=lambda *a, **k: fake)
        eng.process = fake
        import queue as _q
        eng.messages = _q.Queue()
        for r in [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123", "configOptions": [
                {"id": "model", "options": [{"value": "swe-1-7", "name": "SWE-1.7 Max"}]},
            ]}},
        ]:
            eng.messages.put(r)
        from unittest import mock as _mock
        with _mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            with self.assertRaises(EngineError) as ctx:
                eng.start()
        msg = str(ctx.exception)
        self.assertIn("not-a-real-model", msg)
        self.assertIn("refusing", msg)

    def test_no_model_configured_still_starts_on_session_default(self) -> None:
        fake = FakeProcess()
        eng = DevinAcpEngine(cwd="/tmp/d", model=None, popen_factory=lambda *a, **k: fake)
        eng.process = fake
        import queue as _q
        eng.messages = _q.Queue()
        for r in [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-123", "configOptions": []}},
        ]:
            eng.messages.put(r)
        from unittest import mock as _mock
        with _mock.patch("agent_redis_bridge.engines._acp_base.start_stderr_drain", return_value=None):
            eng.start()
        self.assertEqual(eng.session_id, "sess-123")
