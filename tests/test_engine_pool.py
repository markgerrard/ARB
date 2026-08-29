from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Callable

from agent_redis_bridge.engine_pool import AffinityAmbiguousError, AffinityBusyError, AffinityMissError, EnginePool


class FakeEngine:
    """Test double for AgentEngine — tracks lifecycle calls."""

    _next_id = 0
    _id_lock = threading.Lock()

    def __init__(self) -> None:
        with FakeEngine._id_lock:
            FakeEngine._next_id += 1
            self.id = FakeEngine._next_id
        self.started = False
        self.stopped = False
        self.healthy = True
        self.thread_id: str | None = None
        self.session_id: str | None = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def is_healthy(self) -> bool:
        return self.healthy


def reset_fake_engine_ids() -> None:
    with FakeEngine._id_lock:
        FakeEngine._next_id = 0


def fake_factory() -> FakeEngine:
    return FakeEngine()


class EnginePoolBasicTest(unittest.TestCase):
    def test_reserve_defers_factory_for_scored_worktree_turn(self) -> None:
        created = []
        pool = EnginePool(factory=lambda: created.append(True) or object(), max_size=1)

        self.assertTrue(pool.reserve("scored-task"))
        self.assertEqual(created, [])
        self.assertIsNone(pool.get("scored-task"))
        pool.release("scored-task")

    def test_reserve_retires_idle_engine_before_fresh_scored_slot(self) -> None:
        created = []
        pool = EnginePool(factory=lambda: created.append(True) or FakeEngine(), max_size=1)
        ordinary = pool.acquire("ordinary")
        assert ordinary is not None
        pool.release("ordinary")

        self.assertTrue(pool.reserve("scored-task"))
        self.assertTrue(ordinary.stopped)
        self.assertEqual(len(pool.active_task_ids()), 1)
        pool.release("scored-task")

    def setUp(self) -> None:
        reset_fake_engine_ids()

    def test_acquire_returns_engine_and_starts_it(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        engine = pool.acquire("task-1")
        assert engine is not None
        self.assertTrue(engine.started)
        self.assertEqual(engine.id, 1)

    def test_acquire_within_capacity_returns_distinct_engines(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=2)
        a = pool.acquire("task-1")
        b = pool.acquire("task-2")
        assert a is not None and b is not None
        self.assertNotEqual(a.id, b.id)

    def test_acquire_beyond_capacity_returns_none(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        first = pool.acquire("task-1")
        second = pool.acquire("task-2")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_release_returns_engine_to_idle_and_reuses(self) -> None:
        factory_calls: list[FakeEngine] = []

        def tracking_factory() -> FakeEngine:
            engine = FakeEngine()
            factory_calls.append(engine)
            return engine

        pool = EnginePool(factory=tracking_factory, max_size=1)
        first = pool.acquire("task-1")
        pool.release("task-1")
        second = pool.acquire("task-2")

        self.assertIs(first, second)
        self.assertEqual(len(factory_calls), 1)

    def test_release_unknown_task_id_is_noop(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        # Should not raise.
        pool.release("never-acquired")

    def test_stop_all_stops_busy_and_idle_engines(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=2)
        busy = pool.acquire("task-1")
        idle_engine = pool.acquire("task-2")
        pool.release("task-2")
        assert busy is not None and idle_engine is not None

        pool.stop_all()

        self.assertTrue(busy.stopped)
        self.assertTrue(idle_engine.stopped)
        # After stop_all, acquire should still be capped at max_size from a fresh state.
        self.assertIsNone(pool.acquire("task-3"))  # already at started == max_size

    def test_acquire_after_release_does_not_exceed_capacity(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        pool.acquire("task-1")
        # Second acquire blocked.
        self.assertIsNone(pool.acquire("task-2"))
        pool.release("task-1")
        # Now slot is free again.
        self.assertIsNotNone(pool.acquire("task-3"))

    def test_release_discards_unhealthy_engine_and_replaces_it(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        first = pool.acquire("task-1")
        assert first is not None
        first.healthy = False

        pool.release("task-1")
        second = pool.acquire("task-2")

        assert second is not None
        self.assertTrue(first.stopped)
        self.assertNotEqual(first.id, second.id)

    def test_release_retires_healthy_retire_after_turn_engine(self) -> None:
        factory_calls: list[FakeEngine] = []

        def tracking_factory() -> FakeEngine:
            engine = FakeEngine()
            factory_calls.append(engine)
            return engine

        pool = EnginePool(factory=tracking_factory, max_size=1)
        first = pool.acquire("task-1")
        assert first is not None
        first.retire_after_turn = True

        pool.release("task-1")
        second = pool.acquire("task-2")

        assert second is not None
        self.assertTrue(first.stopped)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(factory_calls), 2)

    def test_release_reuses_retire_after_turn_false_engine(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        first = pool.acquire("task-1")
        assert first is not None
        first.retire_after_turn = False

        pool.release("task-1")
        second = pool.acquire("task-2")

        self.assertIs(first, second)
        self.assertFalse(first.stopped)

    def test_unhealthy_retire_after_turn_engine_stops_once(self) -> None:
        class CountingStopEngine(FakeEngine):
            def __init__(self) -> None:
                super().__init__()
                self.stop_count = 0

            def stop(self) -> None:
                self.stop_count += 1
                super().stop()

        pool = EnginePool(factory=CountingStopEngine, max_size=1)
        first = pool.acquire("task-1")
        assert first is not None
        first.retire_after_turn = True
        first.healthy = False

        pool.release("task-1")
        second = pool.acquire("task-2")

        assert second is not None
        self.assertEqual(first.stop_count, 1)
        self.assertNotEqual(first.id, second.id)

    def test_retiring_stop_failure_does_not_corrupt_pool_state(self) -> None:
        class BrokenStopEngine(FakeEngine):
            def stop(self) -> None:
                self.stopped = True
                raise OSError("stop failed")

        pool = EnginePool(factory=BrokenStopEngine, max_size=1)
        first = pool.acquire("task-1")
        assert first is not None
        first.retire_after_turn = True

        pool.release("task-1")
        second = pool.acquire("task-2")

        assert second is not None
        self.assertTrue(first.stopped)
        self.assertNotEqual(first.id, second.id)

    def test_affinity_hit_returns_matching_idle_engine_not_lifo_top(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=2)
        first = pool.acquire("task-1")
        second = pool.acquire("task-2")
        assert first is not None and second is not None
        first.session_id = "session-1"
        second.session_id = "session-2"
        pool.release("task-1")
        pool.release("task-2")

        acquired = pool.acquire("task-3", thread_id="session-1")

        self.assertIs(acquired, first)

    def test_affinity_miss_does_not_spawn_or_consume_unrelated_idle_engine(self) -> None:
        factory_calls: list[FakeEngine] = []

        def tracking_factory() -> FakeEngine:
            engine = FakeEngine()
            factory_calls.append(engine)
            return engine

        pool = EnginePool(factory=tracking_factory, max_size=2)
        existing = pool.acquire("task-1")
        assert existing is not None
        existing.session_id = "session-1"
        pool.release("task-1")

        with self.assertRaises(AffinityMissError):
            pool.acquire("task-2", thread_id="missing")

        self.assertEqual(len(factory_calls), 1)
        self.assertIs(pool.acquire("task-3"), existing)

    def test_affinity_busy_carries_owning_task_id(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        engine = pool.acquire("owner-task")
        assert engine is not None
        engine.session_id = "session-1"

        with self.assertRaises(AffinityBusyError) as caught:
            pool.acquire("task-2", thread_id="session-1")

        self.assertEqual(caught.exception.owning_task_id, "owner-task")

    def test_affinity_duplicate_live_owners_fail_closed(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=2)
        busy = pool.acquire("owner-task")
        idle = pool.acquire("idle-task")
        assert busy is not None and idle is not None
        busy.session_id = "session-1"
        idle.session_id = "session-1"
        pool.release("idle-task")

        with self.assertRaises(AffinityAmbiguousError):
            pool.acquire("task-2", thread_id="session-1")

    def test_affinity_miss_after_unhealthy_owner_is_dropped(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        engine = pool.acquire("owner-task")
        assert engine is not None
        engine.session_id = "session-1"
        engine.healthy = False
        pool.release("owner-task")

        with self.assertRaises(AffinityMissError):
            pool.acquire("task-2", thread_id="session-1")

    def test_affinity_uses_thread_id_before_session_id(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=1)
        engine = pool.acquire("task-1")
        assert engine is not None
        engine.thread_id = "thread-1"
        engine.session_id = "session-1"
        pool.release("task-1")

        with self.assertRaises(AffinityMissError):
            pool.acquire("task-2", thread_id="session-1")
        self.assertIs(pool.acquire("task-3", thread_id="thread-1"), engine)


class EnginePoolConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_fake_engine_ids()

    def test_concurrent_acquires_respect_cap(self) -> None:
        pool = EnginePool(factory=fake_factory, max_size=3)
        results: list[FakeEngine | None] = []
        results_lock = threading.Lock()
        start_barrier = threading.Barrier(10)

        def worker(i: int) -> None:
            start_barrier.wait()
            engine = pool.acquire(f"task-{i}")
            with results_lock:
                results.append(engine)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = [e for e in results if e is not None]
        rejected = [e for e in results if e is None]
        self.assertEqual(len(granted), 3)
        self.assertEqual(len(rejected), 7)
        # Each granted engine has a distinct id.
        self.assertEqual(len({e.id for e in granted}), 3)


class WaitForCapacityTest(unittest.TestCase):
    def test_returns_true_immediately_when_idle_capacity(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        self.assertTrue(pool.wait_for_capacity(0.1))

    def test_blocks_at_capacity_then_wakes_on_release(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        pool.acquire("t1")
        self.assertFalse(pool.wait_for_capacity(0.05))
        result = {}

        def waiter():
            result["ok"] = pool.wait_for_capacity(2.0)

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.1)
        pool.release("t1")
        th.join(2.0)
        self.assertTrue(result["ok"])

    def test_blocks_at_capacity_then_wakes_on_retiring_release(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        engine = pool.acquire("t1")
        assert engine is not None
        engine.retire_after_turn = True
        self.assertFalse(pool.wait_for_capacity(0.05))
        result = {}

        def waiter():
            result["ok"] = pool.wait_for_capacity(2.0)

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.1)
        pool.release("t1")
        th.join(2.0)
        self.assertTrue(result["ok"])

    def test_stop_event_wakes_waiter_without_capacity(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        pool.acquire("t1")
        stop = threading.Event()
        result = {}

        def waiter():
            result["ok"] = pool.wait_for_capacity(5.0, stop)

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.1)
        t0 = time.monotonic()
        stop.set()
        pool.stop_all()
        th.join(2.0)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()


class StartFailureCleanupTest(unittest.TestCase):
    """Audit PSK-1/ASK-4: acquire() registered the engine only AFTER start()
    returned, so a failed start (bad model, auth wedge, connect timeout) left a
    live child process + reader thread unreachable by stop_all() — and because
    _started was never incremented, every subsequent dispatch to the seat
    spawned (and leaked) a fresh orphan."""

    class ExplodingStartEngine:
        instances: list["StartFailureCleanupTest.ExplodingStartEngine"] = []

        def __init__(self) -> None:
            self.stopped = False
            type(self).instances.append(self)

        def start(self) -> None:
            raise RuntimeError("thread/start failed: ERR_MODEL_NOT_FOUND")

        def stop(self) -> None:
            self.stopped = True

    def setUp(self) -> None:
        self.ExplodingStartEngine.instances = []

    def test_failed_start_tears_down_the_engine_and_reraises(self) -> None:
        pool = EnginePool(self.ExplodingStartEngine, max_size=1)

        with self.assertRaises(RuntimeError):
            pool.acquire("task-1")

        self.assertEqual(len(self.ExplodingStartEngine.instances), 1)
        self.assertTrue(self.ExplodingStartEngine.instances[0].stopped)

    def test_failed_start_keeps_pool_accounting_and_capacity_intact(self) -> None:
        pool = EnginePool(self.ExplodingStartEngine, max_size=1)

        for attempt in ("task-1", "task-2"):
            with self.assertRaises(RuntimeError):
                pool.acquire(attempt)
            self.assertTrue(self.ExplodingStartEngine.instances[-1].stopped)

        self.assertEqual(pool._started, 0)
        self.assertEqual(pool._busy, {})
        self.assertEqual(pool._idle, [])

    def test_stop_failure_does_not_mask_the_start_error(self) -> None:
        class DoublyBrokenEngine:
            def start(self) -> None:
                raise RuntimeError("original start failure")

            def stop(self) -> None:
                raise OSError("stop also broken")

        pool = EnginePool(DoublyBrokenEngine, max_size=1)

        with self.assertRaises(RuntimeError) as ctx:
            pool.acquire("task-1")
        self.assertIn("original start failure", str(ctx.exception))
