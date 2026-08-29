"""opencode-acp engine — opencode over ACP.

Pins opencode's two divergences from the gemini-acp base: the `acp` subcommand
shape, and the `build`/`plan` session modes (opencode rejects `yolo` with
"mode not found"). `session/set_model` IS supported, so the base's model call is
inherited — that inheritance is pinned too, since dropping it would silently
un-pin every opencode seat's model.
"""

import json
import queue
import unittest

from agent_redis_bridge.engines.opencode_acp import OpencodeAcpEngine

from test_gemini_acp import FakeProcess


class OpencodeAcpCommandShapeTest(unittest.TestCase):
    def test_command_args_shape_is_opencode_acp(self) -> None:
        engine = OpencodeAcpEngine(cwd="/tmp/project", model=None)
        self.assertEqual(engine.command_args(), ["opencode", "acp"])

    def test_command_override_keeps_acp_subcommand(self) -> None:
        engine = OpencodeAcpEngine(
            cwd="/tmp/project", model=None, command="/usr/local/bin/opencode"
        )
        self.assertEqual(engine.command_args(), ["/usr/local/bin/opencode", "acp"])


class OpencodeAcpSessionTest(unittest.TestCase):
    def test_start_handshakes_and_creates_session(self) -> None:
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "ses_opencode"}},
            ]
        )
        engine = OpencodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake)

        engine.start()

        self.assertEqual(engine.session_id, "ses_opencode")
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "initialize")
        self.assertEqual(sent[1]["method"], "session/new")
        self.assertEqual(sent[1]["params"]["cwd"], "/tmp/project")

    def test_model_pin_is_sent_via_set_model(self) -> None:
        # Unlike omp, opencode DOES support session/set_model and takes a
        # provider/model id. Losing this call would leave seats on whatever
        # model opencode defaults to, silently.
        fake = FakeProcess(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "ses_opencode"}},
                {"jsonrpc": "2.0", "id": 3, "result": {}},
            ]
        )
        engine = OpencodeAcpEngine(
            cwd="/tmp/project", model="opencode/big-pickle", popen_factory=lambda *a, **k: fake
        )

        engine.start()

        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[2]["method"], "session/set_model")
        self.assertEqual(sent[2]["params"]["modelId"], "opencode/big-pickle")

    def test_trusted_policy_sends_build_mode(self) -> None:
        fake = FakeProcess()
        engine = OpencodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "ses_opencode"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["method"], "session/set_mode")
        # NOT "yolo": opencode answers `mode not found: yolo` and the turn dies.
        self.assertEqual(sent[0]["params"]["modeId"], "build")

    def test_untrusted_policy_sends_plan_mode(self) -> None:
        fake = FakeProcess()
        engine = OpencodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "ses_opencode"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})

        engine.run_turn_with_progress("Review this", timeout=1, policy="untrusted", on_event=None)

        sent = [json.loads(line) for line in fake.stdin.lines]
        self.assertEqual(sent[0]["params"]["modeId"], "plan")

    def test_refusal_stop_reason_fails_turn(self) -> None:
        fake = FakeProcess()
        engine = OpencodeAcpEngine(cwd="/tmp/project", model=None, popen_factory=lambda *a, **k: fake)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "ses_opencode"
        engine.messages = queue.Queue()
        engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": {}})
        engine.messages.put({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "refusal"}})

        result = engine.run_turn_with_progress("Do a task", timeout=1, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("stopReason=refusal", result.error or "")


class OpencodeAcpRoleProfileTest(unittest.TestCase):
    def test_engine_does_not_consume_the_role_profile(self) -> None:
        # There is no system-prompt flag on `opencode acp`, so the bridge must
        # keep prepending the role profile to the task text.
        self.assertFalse(getattr(OpencodeAcpEngine, "consumes_role_profile", False))


if __name__ == "__main__":
    unittest.main()
