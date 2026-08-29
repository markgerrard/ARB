import unittest
from unittest import mock

from agent_redis_bridge.engines._agy_gate import assert_agy_host_allowed
from agent_redis_bridge.engines.agy_print import AgyPrintEngine
from agent_redis_bridge.engines.agy_tmux import AgyTmuxEngine
from agent_redis_bridge.engines.base import EngineError

GATE_PLATFORM = "agent_redis_bridge.engines._agy_gate.platform.system"


class AgyHostGateTest(unittest.TestCase):
    def test_darwin_allowed(self) -> None:
        assert_agy_host_allowed("Darwin")

    def test_non_darwin_rejected(self) -> None:
        for system in ("Linux", "Windows", ""):
            with self.assertRaises(EngineError) as ctx:
                assert_agy_host_allowed(system)
            self.assertIn("gated to macOS", str(ctx.exception))

    def test_print_engine_refuses_construction_off_mac(self) -> None:
        with mock.patch(GATE_PLATFORM, return_value="Linux"):
            with self.assertRaises(EngineError):
                AgyPrintEngine(cwd="/tmp/project")

    def test_tmux_engine_refuses_construction_off_mac(self) -> None:
        with mock.patch(GATE_PLATFORM, return_value="Linux"):
            with self.assertRaises(EngineError):
                AgyTmuxEngine(cwd="/tmp/project")

    def test_print_engine_refuses_turn_off_mac(self) -> None:
        # A daemon constructed on an allowed host must still refuse the turn
        # if the platform check stops passing (defence for stale processes).
        with mock.patch(GATE_PLATFORM, return_value="Darwin"):
            engine = AgyPrintEngine(cwd="/tmp/project")
        with mock.patch(GATE_PLATFORM, return_value="Linux"):
            with self.assertRaises(EngineError):
                engine.run_turn_with_progress("task", on_event=None)

    def test_print_engine_constructs_on_darwin(self) -> None:
        with mock.patch(GATE_PLATFORM, return_value="Darwin"):
            AgyPrintEngine(cwd="/tmp/project")


if __name__ == "__main__":
    unittest.main()
