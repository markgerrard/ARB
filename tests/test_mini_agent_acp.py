import json
import queue
import unittest

from agent_redis_bridge.engines.mini_agent_acp import MiniAgentAcpEngine

from test_gemini_acp import FakeProcess


class MiniAgentAcpEngineTest(unittest.TestCase):
    def test_command_args_is_single_binary_no_acp_flag(self) -> None:
        engine = MiniAgentAcpEngine(cwd="/tmp/project", model=None)
        self.assertEqual(engine.command_args(), ["mini-agent-acp"])

    def test_command_override(self) -> None:
        engine = MiniAgentAcpEngine(cwd="/tmp/project", model=None, command="/opt/bin/mini-agent-acp")
        self.assertEqual(engine.command_args(), ["/opt/bin/mini-agent-acp"])

    def test_start_handshakes_and_creates_session(self) -> None:
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-mini"}},
            ]
        )
        engine = MiniAgentAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

        engine.start()

        self.assertEqual(engine.session_id, "session-mini")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "initialize")
        self.assertEqual(sent[1]["method"], "session/new")
        self.assertEqual(sent[1]["params"]["cwd"], "/tmp/project")

    def test_trusted_policy_does_not_send_set_mode(self) -> None:
        # mini-agent's agentCapabilities don't advertise sessionModes; the engine
        # overrides set_session_mode_for_policy to a no-op — mode posture comes
        # from ~/.mini-agent/config/config.yaml, never a session/set_mode call.
        # The prompt therefore goes out FIRST (request id 1, not 2).
        fake = FakeProcess()
        engine = MiniAgentAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-mini"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/prompt")
        self.assertNotIn("session/set_mode", [message["method"] for message in sent])

    def test_refusal_stop_reason_fails_turn(self) -> None:
        # Inherited from the gemini-acp base failure set: a refused turn must be
        # ok=False, not a success whose "result" is refusal prose.
        fake = FakeProcess()
        engine = MiniAgentAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-mini"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")


if __name__ == "__main__":
    unittest.main()
