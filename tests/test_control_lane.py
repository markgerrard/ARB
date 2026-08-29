import unittest
from pathlib import Path

from agent_redis_bridge.redis_io import RedisConfig


CTL = Path(__file__).parents[1] / "src" / "agent_redis_bridge" / "ctl.py"


class ControlLaneTest(unittest.TestCase):
    def test_config_has_control_key(self):
        cfg = RedisConfig("127.0.0.1", "6379", "15", "agent_scratch:")
        self.assertEqual(cfg.control_key("codex-x-dev"), "agent_scratch:agent:codex-x-dev:control")

    def test_send_control_targets_control_key_not_inbox(self):
        src = CTL.read_text()
        self.assertIn("lpush_control", src)
        send_control_body = src[src.index("def send_control") :]
        self.assertNotIn("redis.lpush(target_agent_id", send_control_body)


if __name__ == "__main__":
    unittest.main()
