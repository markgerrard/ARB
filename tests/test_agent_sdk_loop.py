import asyncio
import time
import unittest

from agent_redis_bridge.engines.agent_sdk_loop import LoopThread


class LoopThreadTest(unittest.TestCase):
    def test_submit_runs_on_loop_from_another_thread(self):
        loop_thread = LoopThread()
        loop_thread.start()
        try:
            self.assertEqual(loop_thread.submit(self._echo(7)).result(timeout=5), 7)
        finally:
            loop_thread.stop(timeout=5)

    def test_control_coro_runs_while_long_coro_in_flight(self):
        loop_thread = LoopThread()
        loop_thread.start()
        try:
            slow = loop_thread.submit(self._sleep(2))
            time.sleep(0.2)
            fast = loop_thread.submit(self._echo("interrupt"))
            self.assertEqual(fast.result(timeout=2), "interrupt")
            self.assertFalse(slow.done())
            slow.result(timeout=5)
        finally:
            loop_thread.stop(timeout=5)

    async def _echo(self, value):
        return value

    async def _sleep(self, seconds):
        await asyncio.sleep(seconds)
        return "slept"
