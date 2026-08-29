import threading
import time
import unittest
from unittest import mock


class _Pool:
    def __init__(self):
        self.calls = []
        self.cap = True

    def wait_for_capacity(self, timeout, stop_event=None):
        self.calls.append(("wait", timeout, stop_event))
        return self.cap


class CapacityGateTest(unittest.TestCase):
    def test_loop_waits_for_capacity_before_pop(self):
        import agent_redis_bridge.bridge as b

        bridge = b.Bridge.__new__(b.Bridge)
        bridge.pool = _Pool()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(
            blpop_timeout=1,
            control_poll_timeout=0.01,
            once=False,
            max_message_bytes=10_000,
            heartbeat_ttl=60,
        )
        bridge.agent_id = "codex-x-dev"
        bridge.redis = mock.Mock(lpop_control=lambda agent_id: None)
        popped = {"n": 0}

        def fake_pop(timeout=None):
            self.assertEqual(timeout, bridge.args.control_poll_timeout)
            popped["n"] += 1
            bridge.stop_event.set()
            return (None, False)

        bridge.recover_processing_envelopes = lambda: None
        bridge.pop_inbox = fake_pop
        bridge.inbox_loop()
        self.assertEqual(bridge.pool.calls[0][0], "wait")
        self.assertIs(bridge.pool.calls[0][2], bridge.stop_event)
        self.assertEqual(popped["n"], 1)

    def test_no_capacity_skips_pop(self):
        import agent_redis_bridge.bridge as b

        bridge = b.Bridge.__new__(b.Bridge)
        bridge.pool = _Pool()
        bridge.pool.cap = False
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(
            blpop_timeout=0,
            control_poll_timeout=0.01,
            once=False,
            max_message_bytes=10_000,
            heartbeat_ttl=60,
        )
        bridge.agent_id = "codex-x-dev"
        bridge.redis = mock.Mock(lpop_control=lambda agent_id: None)
        bridge.recover_processing_envelopes = lambda: None
        calls = {"pop": 0}

        def fake_pop():
            calls["pop"] += 1
            return (None, False)

        bridge.pop_inbox = fake_pop

        def stopper():
            time.sleep(0.05)
            bridge.stop_event.set()

        threading.Thread(target=stopper).start()
        bridge.inbox_loop()
        self.assertEqual(calls["pop"], 0)


class ControlStarvationTest(unittest.TestCase):
    def test_controls_drain_even_at_full_capacity(self):
        import agent_redis_bridge.bridge as b

        bridge = b.Bridge.__new__(b.Bridge)

        class _NoCap:
            def wait_for_capacity(self, timeout, stop_event=None):
                return False

        bridge.pool = _NoCap()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(
            blpop_timeout=30,
            control_poll_timeout=0.01,
            once=False,
            max_message_bytes=10_000,
            heartbeat_ttl=60,
        )
        bridge.agent_id = "codex-x-dev"
        bridge.recover_processing_envelopes = lambda: None
        handled = []
        controls = ['{"id":"c1","kind":"cancel","payload":{}}']

        def fake_lpop_control(agent_id):
            return controls.pop(0) if controls else None

        bridge.redis = mock.Mock(lpop_control=fake_lpop_control)

        def fake_handle_raw(raw, processing_raw=None):
            handled.append(raw)
            bridge.stop_event.set()
            return False

        bridge.handle_raw = fake_handle_raw
        bridge.pop_inbox = lambda timeout=None: (None, False)

        def stopper():
            time.sleep(0.05)
            bridge.stop_event.set()

        threading.Thread(target=stopper).start()
        bridge.inbox_loop()
        self.assertEqual(len(handled), 1)
        self.assertIn("cancel", handled[0])


class ControlDrainFailSoftTest(unittest.TestCase):
    def test_control_lpop_error_does_not_escape_loop(self):
        import agent_redis_bridge.bridge as b

        bridge = b.Bridge.__new__(b.Bridge)
        bridge.pool = _Pool()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(
            blpop_timeout=30,
            control_poll_timeout=0.01,
            once=False,
            max_message_bytes=10_000,
            heartbeat_ttl=60,
        )
        bridge.agent_id = "codex-x-dev"
        bridge.recover_processing_envelopes = lambda: None

        def fake_lpop_control(agent_id):
            raise RuntimeError("redis blip")

        bridge.redis = mock.Mock(lpop_control=fake_lpop_control)
        bridge.pop_inbox = lambda timeout=None: (None, False)

        def stopper():
            time.sleep(0.05)
            bridge.stop_event.set()

        threading.Thread(target=stopper).start()
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            bridge.inbox_loop()
        self.assertTrue(any("[bridge-error] control-fail redis blip" in line for line in logs.output))


class ControlLaneKindCheckTest(unittest.TestCase):
    def test_request_in_control_lane_is_dropped_before_handle_raw(self):
        import agent_redis_bridge.bridge as b

        bridge = b.Bridge.__new__(b.Bridge)
        bridge.pool = _Pool()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(
            blpop_timeout=30,
            control_poll_timeout=0.01,
            once=False,
            max_message_bytes=10_000,
            heartbeat_ttl=60,
        )
        bridge.agent_id = "codex-x-dev"
        bridge.recover_processing_envelopes = lambda: None
        controls = ['{"id":"req-in-control","kind":"request","payload":{}}']

        def fake_lpop_control(agent_id):
            return controls.pop(0) if controls else None

        bridge.redis = mock.Mock(lpop_control=fake_lpop_control)
        bridge.handle_raw = mock.Mock()
        bridge.pop_inbox = lambda timeout=None: (None, False)

        def stopper():
            time.sleep(0.05)
            bridge.stop_event.set()

        threading.Thread(target=stopper).start()
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
            bridge.inbox_loop()
        bridge.handle_raw.assert_not_called()
        self.assertTrue(any("[bridge-error] control-lane-non-control dropped" in line for line in logs.output))
