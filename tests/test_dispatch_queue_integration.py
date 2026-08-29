import json
import os
import threading
import time
import unittest
import uuid

REDIS_URL = os.environ.get("ARB_BRIDGE_TEST_REDIS_URL")


@unittest.skipUnless(REDIS_URL, "no ARB_BRIDGE_TEST_REDIS_URL (real-bus integration)")
class DispatchQueueIntegrationTest(unittest.TestCase):
    """FIFO + no-bounce + queued->running, driven through the REAL inbox_loop against real Redis.

    FIFO comes from the Redis list (RPUSH tail / pop head) + the single-popper gate, NOT from the pool.
    Build a bridge at max_parallel=1 with a gated fake engine (see make_bridge / GatedEngine in
    tests/test_bridge_parallelism.py for the construction pattern), point it at REDIS_URL on a unique
    prefix, then:
      1. RPUSH 3 request envelopes (ids t0,t1,t2) to the seat inbox BEFORE starting the loop.
      2. Run bridge.inbox_loop() in a thread; release the gate so each turn completes.
      3. Assert: 3 replies, all ok, NONE carry 'bridge busy'; reply order is t0,t1,t2 (FIFO).
      4. Assert task:<t1>:status transitioned queued(dispatcher-written)->running->terminal
         (i.e. never stuck at 'queued' after the turn ran).
      5. Cleanup: DEL the prefix keys.
    """

    def test_full_pool_queues_then_runs_in_fifo(self):
        self.skipTest("integration harness: implement per the docstring against ARB_BRIDGE_TEST_REDIS_URL")
