"""The grok model pin must hold for EVERY session, and must never fail quietly.

Background. Before 8d1538ed the engine called camelCase `session/setModel` —
which grok answers with -32601 Method not found — and swallowed the error, so
`--model X` was a silent no-op for the whole life of the seat. The danger is not
a crashed seat; it is a seat an operator believes is pinned while it serves the
CLI config default. Every test here asserts the SPECIFIC observable (the method
name on the wire, the modelId, the error text), never a bare success/refusal.
"""

import json
import unittest
from unittest import mock

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine

from tests.test_grok_acp import FakeProcess

HANDSHAKE = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}}


def _ok(request_id: int, model: str) -> dict:
    """The shape grok really returns — probed live 2026-08-12 against grok 1.0.3."""
    return {"jsonrpc": "2.0", "id": request_id, "result": {"_meta": {"model": {"Ok": model}}}}


def _engine(fake: FakeProcess, model: str | None) -> GrokAcpEngine:
    return GrokAcpEngine(cwd="/tmp/project", model=model, popen_factory=lambda *a, **k: fake)


def _sent(fake: FakeProcess) -> list[dict]:
    return [json.loads(line) for line in fake.stdin.lines]


class GrokModelPinTest(unittest.TestCase):
    def test_start_pins_the_session_with_snake_case_set_model(self) -> None:
        """The casing IS the bug. camelCase returns -32601, so assert the exact
        method string on the wire rather than merely that start() succeeded."""
        fake = FakeProcess(
            [
                HANDSHAKE,
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s-1"}},
                _ok(3, "grok-4.6"),
            ]
        )
        engine = _engine(fake, "grok-4.6")

        engine.start()

        sent = _sent(fake)
        self.assertEqual(sent[2]["method"], "session/set_model")
        self.assertEqual(sent[2]["params"], {"sessionId": "s-1", "modelId": "grok-4.6"})

    def test_no_model_sends_no_set_model_call(self) -> None:
        fake = FakeProcess([HANDSHAKE, {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s-1"}}])
        engine = _engine(fake, None)

        engine.start()

        self.assertEqual([m["method"] for m in _sent(fake)], ["initialize", "session/new"])

    def test_a_server_that_pins_a_different_model_raises(self) -> None:
        """The whole point: accepting the call is not evidence the pin took."""
        fake = FakeProcess(
            [
                HANDSHAKE,
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s-1"}},
                _ok(3, "grok-4.5"),
            ]
        )
        engine = _engine(fake, "grok-4.6")

        with self.assertRaises(EngineError) as caught:
            engine.start()

        message = str(caught.exception)
        self.assertIn("did not pin the requested model", message)
        self.assertIn("'grok-4.6'", message)
        self.assertIn("'grok-4.5'", message)

    def test_an_unparseable_set_model_reply_raises_rather_than_passing(self) -> None:
        """A reply missing _meta.model.Ok must not read as success. This is the
        exact shape the old code produced by swallowing everything."""
        fake = FakeProcess(
            [
                HANDSHAKE,
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s-1"}},
                {"jsonrpc": "2.0", "id": 3, "result": {}},
            ]
        )
        engine = _engine(fake, "grok-4.6")

        with self.assertRaisesRegex(EngineError, "did not pin the requested model"):
            engine.start()

    def test_a_set_model_error_propagates_instead_of_being_swallowed(self) -> None:
        """Regression guard for the original defect: -32601 must kill start()."""
        fake = FakeProcess(
            [
                HANDSHAKE,
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s-1"}},
                {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found"}},
            ]
        )
        engine = _engine(fake, "grok-4.6")

        with self.assertRaisesRegex(EngineError, "Method not found"):
            engine.start()


class GrokModelPinSurvivesRotationTest(unittest.TestCase):
    """`_rotate_session_if_reused` opens a FRESH session per dispatch and the pin
    is per-session, so a pin applied only in start() lapses from dispatch 2 on.
    """

    def _rotating_engine(self, fake: FakeProcess) -> GrokAcpEngine:
        engine = _engine(fake, "grok-4.6")
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "s-1"
        engine.retire_after_turn = False
        engine._turns_served = 1
        return engine

    def test_rotation_repins_the_new_session(self) -> None:
        fake = FakeProcess()
        engine = self._rotating_engine(fake)
        with mock.patch.object(
            GrokAcpEngine,
            "request",
            side_effect=[{"sessionId": "s-2"}, {"_meta": {"model": {"Ok": "grok-4.6"}}}],
            autospec=True,
        ) as request:
            engine._rotate_session_if_reused()

        methods = [call.args[1] for call in request.call_args_list]
        self.assertEqual(methods, ["session/new", "session/set_model"])
        self.assertEqual(request.call_args_list[1].args[2]["sessionId"], "s-2")
        self.assertEqual(engine.session_id, "s-2")

    def test_a_failed_repin_quarantines_the_engine(self) -> None:
        """Falling back to an unpinned session is the failure mode being prevented."""
        fake = FakeProcess()
        engine = self._rotating_engine(fake)
        with mock.patch.object(
            GrokAcpEngine,
            "request",
            side_effect=[{"sessionId": "s-2"}, {"_meta": {"model": {"Ok": "grok-4.5"}}}],
            autospec=True,
        ):
            with self.assertRaisesRegex(EngineError, "model re-pin after session rotation failed"):
                engine._rotate_session_if_reused()

        self.assertFalse(engine.healthy)

    def test_rotation_without_a_pin_sends_no_set_model(self) -> None:
        fake = FakeProcess()
        engine = _engine(fake, None)
        engine.process = fake  # type: ignore[assignment]
        engine.session_id = "s-1"
        engine.retire_after_turn = False
        engine._turns_served = 1
        with mock.patch.object(
            GrokAcpEngine, "request", side_effect=[{"sessionId": "s-2"}], autospec=True
        ) as request:
            engine._rotate_session_if_reused()

        self.assertEqual([call.args[1] for call in request.call_args_list], ["session/new"])


if __name__ == "__main__":
    unittest.main()
