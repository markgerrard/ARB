import inspect
import unittest
from pathlib import Path

from agent_redis_bridge import ctl
from agent_redis_bridge.redis_io import RedisCli, RedisConfig


DISPATCH = Path(__file__).parents[1] / "scripts" / "agent-dispatch"


def _local_redis_cli():
    """A RedisCli against a local test Redis (127.0.0.1:6390 db13), or None if unreachable."""
    try:
        cli = RedisCli(RedisConfig(host="127.0.0.1", port="6390", db="13", prefix="fifotest:"))
        cli.client.ping()
        return cli
    except Exception:
        return None


class FifoRequestQueueTest(unittest.TestCase):
    def test_redis_cli_has_request_tail_push_without_changing_lpush(self):
        self.assertTrue(hasattr(RedisCli, "rpush"))
        self.assertIn("client.rpush", inspect.getsource(RedisCli.rpush))
        self.assertIn("client.lpush", inspect.getsource(RedisCli.lpush))

    def test_ctl_send_uses_rpush_and_controls_stay_on_control_lane(self):
        # Slice 1d-iv: ordinary send no longer rpushs itself — it routes through
        # dispatch_authority (enqueue_pre_minted → publish_and_enqueue), which is
        # the sole redis_cli.rpush site for ordinary requests.
        from agent_redis_bridge import dispatch_authority as da

        self.assertIn("enqueue_pre_minted", inspect.getsource(ctl.send_task))
        self.assertNotIn("redis.lpush(target_agent_id", inspect.getsource(ctl.send_task))
        self.assertIn("redis_cli.rpush(", inspect.getsource(da.publish_and_enqueue))
        self.assertIn("redis.lpush_control(", inspect.getsource(ctl.send_control))

    def test_agent_dispatch_request_enqueue_uses_rpush_only_for_target_inbox(self):
        src = DISPATCH.read_text()
        self.assertIn('RPUSH "${PREFIX}agent:${TO}:inbox"', src)
        self.assertNotIn('LPUSH "${PREFIX}agent:${TO}:inbox"', src)
        self.assertIn('RPUSH "${PREFIX}agent:${FROM}:inbox"', src)


class FifoBehaviorTest(unittest.TestCase):
    """Behavioral FIFO check through the REAL request-producer push (rpush) + bridge pop (blpop).

    Skips without a local Redis. Catches a semantic regression the source-grep tests can't:
    if the producer reverted to lpush, or the bridge popped from the right, this would go red.
    """

    def setUp(self):
        self.cli = _local_redis_cli()
        if self.cli is None:
            self.skipTest("no local redis on 127.0.0.1:6390")
        self.agent = "fifo-behavior-test"
        self.cli.client.delete(self.cli.config.inbox_key(self.agent))

    def tearDown(self):
        if self.cli is not None:
            self.cli.client.delete(self.cli.config.inbox_key(self.agent))

    def test_rpush_then_blpop_yields_fifo(self):
        for body in ("t0", "t1", "t2"):
            self.cli.rpush(self.agent, body)          # the request-producer path
        got = [self.cli.blpop(self.agent, 1) for _ in range(3)]  # the bridge pop path
        self.assertEqual(got, ["t0", "t1", "t2"])     # oldest-first; lpush would give t2,t1,t0


if __name__ == "__main__":
    unittest.main()
