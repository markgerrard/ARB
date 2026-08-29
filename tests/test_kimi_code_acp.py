import json
import queue
import unittest

from agent_redis_bridge.engines.kimi_code_acp import KimiCodeAcpEngine

from test_gemini_acp import FakeProcess


class KimiCodeAcpEngineTest(unittest.TestCase):
    def test_command_args_shape_is_kimi_acp(self) -> None:
        engine = KimiCodeAcpEngine(cwd="/tmp/project", model=None)
        self.assertEqual(engine.command_args(), ["kimi", "acp"])

    def test_command_override_keeps_acp_subcommand(self) -> None:
        engine = KimiCodeAcpEngine(cwd="/tmp/project", model=None, command="/opt/kimi/bin/kimi")
        self.assertEqual(engine.command_args(), ["/opt/kimi/bin/kimi", "acp"])

    def test_start_handshakes_and_creates_session(self) -> None:
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-kimi"}},
            ]
        )
        engine = KimiCodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

        engine.start()

        self.assertEqual(engine.session_id, "session-kimi")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "initialize")
        self.assertEqual(sent[1]["method"], "session/new")
        self.assertEqual(sent[1]["params"]["cwd"], "/tmp/project")

    def test_trusted_policy_sends_yolo_set_mode(self) -> None:
        # Kimi advertises modes via configOptions but NOT agentCapabilities.sessionModes;
        # the inherited session/set_mode(yolo) is still sent and kimi accepts it
        # (verified empirically 2026-06-04 — see the module docstring). Without yolo,
        # kimi gates every tool call behind session/request_permission.
        fake = FakeProcess()
        engine = KimiCodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-kimi"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/set_mode")
        self.assertEqual(sent[0]["params"]["modeId"], "yolo")
        self.assertEqual(sent[1]["method"], "session/prompt")

    def test_refusal_stop_reason_fails_turn(self) -> None:
        # Inherited from the gemini-acp base failure set: a refused turn must be
        # ok=False, not a success whose "result" is refusal prose.
        fake = FakeProcess()
        engine = KimiCodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "session-kimi"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")


if __name__ == "__main__":
    unittest.main()
