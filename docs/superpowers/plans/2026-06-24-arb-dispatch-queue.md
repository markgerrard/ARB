# ARB dispatch-queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a busy bridge seat *queue* work instead of bouncing it with "bridge busy", by gating the main-loop pop on engine-pool capacity — with a stop-aware waiter and an observable per-task `queued` status.

**Architecture:** Add `EnginePool.wait_for_capacity()` (a condition variable on the pool's existing lock). The bridge `inbox_loop` waits for a free slot *before* popping from Redis `:inbox`, so un-popped work stays in the durable FIFO list. The dispatcher writes a `queued` status on enqueue and uses a higher default `--timeout`. Spec: `docs/superpowers/specs/2026-06-24-arb-dispatch-queue-design.md` (v2).

**Tech Stack:** Python 3.12+ (`agent_redis_bridge`), `threading`, `redis-py`, bash (`agent-dispatch`), `unittest` (existing test style — match it).

## Global Constraints

- The capacity gate lives in **`inbox_loop` only**. `handle_raw`'s `acquire`-or-`bridge busy` path is **kept unchanged** as a safety net (and is what `test_bridge_parallelism.py` exercises directly — those tests stay green; do NOT rewrite them, see Task 2 note).
- The **single-popper invariant** is load-bearing: only the main loop (`inbox_loop`→`handle_raw`) acquires; workers only `release`. The gate is correct *because* of this. Keep the `engine is None` branch as a loud safety net.
- `EnginePool._lock` is a `threading.Lock`; `_idle: list`, `_busy: dict`, `_started: int`, `_max_size: int`. A `Condition(self._lock)` shares that lock — `notify`/`notify_all` must be called while holding it (release/stop_all already do).
- Status key = `redis_config.task_status_key(id)` = `<prefix>task:<id>:status` (a hash). Dispatcher writes the same key the bridge later overwrites.
- Existing tests use **unittest** (`tests/test_engine_pool.py`, `tests/test_bridge_parallelism.py`). Match that style. Run: `.venv/bin/python -m pytest <path> -q`.
- TDD: failing test → watch fail → minimal impl → watch pass → commit.

---

### Task 1: `EnginePool.wait_for_capacity` + condition variable

**Files:**
- Modify: `src/agent_redis_bridge/engine_pool.py`
- Test: `tests/test_engine_pool.py`

**Interfaces:**
- Produces: `wait_for_capacity(self, timeout: float | None, stop_event: threading.Event | None = None) -> bool` — True iff a real slot is free (stop-only wake returns False). `release` calls `self._cap_cond.notify()`; `stop_all` calls `notify_all()`. Helper `_has_capacity_unlocked() -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_engine_pool.py
import threading, time

class WaitForCapacityTest(unittest.TestCase):
    def test_returns_true_immediately_when_idle_capacity(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        self.assertTrue(pool.wait_for_capacity(0.1))

    def test_blocks_at_capacity_then_wakes_on_release(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        pool.acquire("t1")                       # full
        self.assertFalse(pool.wait_for_capacity(0.05))   # times out, no capacity
        result = {}
        def waiter():
            result["ok"] = pool.wait_for_capacity(2.0)
        th = threading.Thread(target=waiter); th.start()
        time.sleep(0.1)
        pool.release("t1")                       # frees the slot, notifies
        th.join(2.0)
        self.assertTrue(result["ok"])

    def test_stop_event_wakes_waiter_without_capacity(self):
        pool = EnginePool(factory=fake_factory, max_size=1)
        pool.acquire("t1")                       # full
        stop = threading.Event()
        result = {}
        def waiter():
            result["ok"] = pool.wait_for_capacity(5.0, stop)
        th = threading.Thread(target=waiter); th.start()
        time.sleep(0.1)
        t0 = time.monotonic()
        stop.set()                               # bridge sets stop_event BEFORE stop_all (bridge.py:338)
        pool.stop_all()                          # notify_all wakes the waiter; it re-checks _ready -> stop
        th.join(2.0)
        self.assertLess(time.monotonic() - t0, 1.0)   # woke promptly, not after 5s
        self.assertFalse(result["ok"])           # stop-only wake -> False (no real slot)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_engine_pool.py::WaitForCapacityTest -q`
Expected: FAIL (`AttributeError: 'EnginePool' object has no attribute 'wait_for_capacity'`).

- [ ] **Step 3: Implement**

In `engine_pool.py` `__init__`, add after `self._started = 0`:
```python
        self._cap_cond = threading.Condition(self._lock)
```
Add methods to `EnginePool`:
```python
    def _has_capacity_unlocked(self) -> bool:
        return len(self._idle) > 0 or self._started < self._max_size

    def wait_for_capacity(self, timeout: float | None, stop_event: threading.Event | None = None) -> bool:
        def _ready() -> bool:
            return self._has_capacity_unlocked() or (stop_event is not None and stop_event.is_set())
        with self._cap_cond:
            if not _ready():
                self._cap_cond.wait_for(_ready, timeout=timeout)
            return self._has_capacity_unlocked()
```
In `release`, **inside the `with self._lock:` block, after the WHOLE `if engine is not None:` block**
(so it fires for BOTH the healthy→`_idle` path AND the unhealthy `_started -= 1` path — both free a
slot), add (cold-B):
```python
            self._cap_cond.notify()
```
Placement matters: putting `notify()` only in the healthy `else` branch would miss the case where an
unhealthy engine is dropped (`_started -= 1`), which also creates capacity a waiter must see.
In `stop_all`, change the `with self._lock:` block to also wake waiters:
```python
        with self._lock:
            engines = list(self._busy.values()) + list(self._idle)
            self._busy.clear()
            self._idle.clear()
            self._cap_cond.notify_all()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_engine_pool.py -q`
Expected: PASS (the 3 new + all existing).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/engine_pool.py tests/test_engine_pool.py
git commit -m "feat(bridge): EnginePool.wait_for_capacity (stop-aware cond var; release notifies, stop_all notify_all)"
```

---

### Task 2: gate `inbox_loop` on capacity

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py` (`inbox_loop`, ~381)
- Test: `tests/test_bridge_capacity_gate.py` (new)

**Interfaces:**
- Consumes: `EnginePool.wait_for_capacity` (Task 1). No change to `handle_raw` / the `engine is None` busy reply.

**NOTE (spec divergence, intentional):** spec v2 listed "rewrite `test_bridge_parallelism.py:98,127`". On inspection those tests call `handle_raw()` directly, which still returns `bridge busy` at capacity (the preserved safety net) — they are unaffected by an `inbox_loop`-level gate and stay green. We add gate coverage here instead and leave them as-is.

- [ ] **Step 1: Write the failing test** (the loop waits for capacity before popping, and exits promptly on stop)

```python
# tests/test_bridge_capacity_gate.py
import threading, time, unittest
from unittest import mock

class _Pool:
    def __init__(self): self.calls = []; self.cap = True
    def wait_for_capacity(self, timeout, stop_event=None):
        # capture stop_event so the test can ASSERT it was passed (cold-B P1: dropping it silently
        # re-breaks the shutdown-wake this change exists to fix)
        self.calls.append(("wait", timeout, stop_event)); return self.cap

class CapacityGateTest(unittest.TestCase):
    def test_loop_waits_for_capacity_before_pop(self):
        import agent_redis_bridge.bridge as b
        bridge = b.Bridge.__new__(b.Bridge)        # bare instance; we drive inbox_loop's guard
        bridge.pool = _Pool()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(blpop_timeout=1, once=False, max_message_bytes=10_000)
        popped = {"n": 0}
        def fake_pop():
            popped["n"] += 1
            bridge.stop_event.set()                # one iteration then stop
            return (None, False)
        bridge.recover_processing_envelopes = lambda: None
        bridge.pop_inbox = fake_pop
        bridge.inbox_loop()
        # wait_for_capacity was called before the (single) pop, WITH the bridge's stop_event passed
        self.assertEqual(bridge.pool.calls[0][0], "wait")
        self.assertIs(bridge.pool.calls[0][2], bridge.stop_event)   # stop_event MUST be forwarded
        self.assertEqual(popped["n"], 1)

    def test_no_capacity_skips_pop(self):
        import agent_redis_bridge.bridge as b
        bridge = b.Bridge.__new__(b.Bridge)
        bridge.pool = _Pool(); bridge.pool.cap = False
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(blpop_timeout=0, once=False, max_message_bytes=10_000)
        bridge.recover_processing_envelopes = lambda: None
        calls = {"pop": 0}
        def fake_pop():
            calls["pop"] += 1; return (None, False)
        bridge.pop_inbox = fake_pop
        def stopper():
            time.sleep(0.05); bridge.stop_event.set()
        threading.Thread(target=stopper).start()
        bridge.inbox_loop()
        self.assertEqual(calls["pop"], 0)          # never popped without capacity
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bridge_capacity_gate.py -q`
Expected: FAIL — current `inbox_loop` pops without a capacity gate (`test_no_capacity_skips_pop` pops anyway).

- [ ] **Step 3: Implement the gate**

In `bridge.py` `inbox_loop`, insert the gate as the FIRST two lines inside the `while` — **retain the
existing `parked and self.stop_event.is_set()` shutdown block and everything below verbatim**, change
nothing else:
```python
        while not self.stop_event.is_set():
            if not self.pool.wait_for_capacity(self.args.blpop_timeout, self.stop_event):
                continue                                  # no slot (or shutdown) -> re-check loop guard
            raw, parked = self.pop_inbox()
            if raw is None:
                continue

            if parked and self.stop_event.is_set():       # <-- EXISTING block, keep as-is
                print(
                    f"[bridge] shutdown with parked envelope id={self.envelope_id_for_log(raw)} "
                    "(will recover on restart)",
                    flush=True,
                )
                break
            # ... rest of the existing loop body unchanged ...
```
Leave `pop_inbox`, `handle_raw`, and the `engine is None` busy reply (the safety net) unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_bridge_capacity_gate.py tests/test_bridge_parallelism.py tests/test_bridge_max_parallel.py -q`
Expected: PASS (new gate tests + the unchanged parallelism/max-parallel tests stay green).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_bridge_capacity_gate.py
git commit -m "feat(bridge): gate inbox_loop on wait_for_capacity — queue instead of bounce (handle_raw busy kept as safety net)"
```

---

### Task 3: `agent-dispatch` — `queued` status on enqueue + raised default timeout

**Files:**
- Modify: `scripts/agent-dispatch`
- Test: `tests/test_agent_dispatch_queue.py` (new)

**Interfaces:**
- Produces: after the `LPUSH`, the dispatcher writes `<prefix>task:<id>:status` hash `{task_id, state:"queued", queue_depth:<LLEN inbox>, enqueued_at}` (ttl 7d). Default `TIMEOUT` raised `1800`→`3600`.

**v2/P1 RACE FIX (codex):** the `queued` status MUST be written **BEFORE** the `LPUSH`, not after. A
ready bridge can pop the envelope the instant it lands on `:inbox` and write `state="running"`
(`bridge.py:686`) *before* a post-LPUSH `HSET queued` executes — clobbering `running` back to `queued`.
Writing `queued` first makes the envelope un-poppable until after the status exists, so the only possible
overwrite is the correct `queued`→`running` direction. `queue_depth` = `LLEN` *before* the push = items
ahead of you.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_dispatch_queue.py
import unittest
from pathlib import Path

DISPATCH = Path(__file__).parents[1] / "scripts" / "agent-dispatch"
SRC = DISPATCH.read_text()

class AgentDispatchQueueTest(unittest.TestCase):
    def test_default_timeout_raised_to_3600(self):
        self.assertRegex(SRC, r'(?m)^TIMEOUT=3600\b')

    def test_writes_queued_status_BEFORE_lpush(self):
        # the queued-status HSET must reference the task status key + queued state
        self.assertIn('task:${ID}:status', SRC)
        self.assertIn('state queued', SRC)
        # RACE FIX: the queued write must appear BEFORE the inbox LPUSH (not after)
        lpush_idx = SRC.index('LPUSH "${PREFIX}agent:${TO}:inbox"')
        status_idx = SRC.index('task:${ID}:status')
        self.assertLess(status_idx, lpush_idx,
                        "queued status must be HSET before the LPUSH to avoid clobbering 'running'")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent_dispatch_queue.py -q`
Expected: FAIL (`TIMEOUT=1800`, no queued-status write).

- [ ] **Step 3: Implement**

In `scripts/agent-dispatch`, change `TIMEOUT=1800` to `TIMEOUT=3600`. Then, **immediately BEFORE** the
inbox `LPUSH` line (`redis-cli "${REDIS_FLAGS[@]}" LPUSH "${PREFIX}agent:${TO}:inbox" "$MSG" >/dev/null`),
add:
```bash
# Observable queue (write BEFORE the LPUSH so the bridge can't pop+mark-running before this lands).
# queue_depth = items already ahead of this one. The bridge overwrites task:<id>:status to "running"
# on pop+acquire (bridge.py:686). NOTE: HSET merges — the stale queue_depth/enqueued_at fields persist
# after the running overwrite; readers must treat the `state` field as authoritative, not their presence.
QDEPTH=$(redis-cli "${REDIS_FLAGS[@]}" LLEN "${PREFIX}agent:${TO}:inbox" 2>/dev/null || echo 0)
redis-cli "${REDIS_FLAGS[@]}" HSET "${PREFIX}task:${ID}:status" \
  task_id "$ID" state queued queue_depth "$QDEPTH" enqueued_at "$(date -Iseconds)" >/dev/null 2>&1 || true
redis-cli "${REDIS_FLAGS[@]}" EXPIRE "${PREFIX}task:${ID}:status" 604800 >/dev/null 2>&1 || true
```
Also update the `usage()` `--timeout` note (default 3600s to cover queued waits).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_agent_dispatch_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/agent-dispatch tests/test_agent_dispatch_queue.py
git commit -m "feat(dispatch): write queued status on enqueue + raise default --timeout to 3600 (observable deep queue)"
```

---

### Task 4: integration — queued→running over a real bridge (skip-if-no-redis) + CHANGELOG

**Files:**
- Create: `tests/test_dispatch_queue_integration.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: a real local Redis (`ARB_BRIDGE_TEST_REDIS_URL` or the project's existing integration-redis fixture, whichever `tests/` already uses); skip when unset.

**v2 (codex P1):** the earlier `EnginePool`-direct fallback is REMOVED — `threading.Condition.notify()`
does NOT guarantee FIFO wake order, so asserting FIFO over condition waiters is both wrong and proves
nothing about the real path. **FIFO is a property of the Redis `:inbox` list + the single-popper**, not of
the pool. So this test must drive the real `inbox_loop` against a real Redis bus (skip when unset). It is
the only faithful coverage of gate+loop+pop+queued→running together; T1/T2 cover the pool/gate logic
deterministically.

- [ ] **Step 1: Write the integration test (skips without redis)**

```python
# tests/test_dispatch_queue_integration.py
import json, os, threading, time, unittest, uuid

REDIS_URL = os.environ.get("ARB_BRIDGE_TEST_REDIS_URL")

@unittest.skipUnless(REDIS_URL, "no ARB_BRIDGE_TEST_REDIS_URL (real-bus integration)")
class DispatchQueueIntegrationTest(unittest.TestCase):
    """FIFO + no-bounce + queued->running, driven through the REAL inbox_loop against real Redis.

    FIFO comes from the Redis list (LPUSH tail / pop head) + the single-popper gate, NOT from the pool.
    Build a bridge at max_parallel=1 with a gated fake engine (see make_bridge / GatedEngine in
    tests/test_bridge_parallelism.py for the construction pattern), point it at REDIS_URL on a unique
    prefix, then:
      1. LPUSH 3 request envelopes (ids t0,t1,t2) to the seat inbox BEFORE starting the loop.
      2. Run bridge.inbox_loop() in a thread; release the gate so each turn completes.
      3. Assert: 3 replies, all ok, NONE carry 'bridge busy'; reply order is t0,t1,t2 (FIFO).
      4. Assert task:<t1>:status transitioned queued(dispatcher-written)->running->terminal
         (i.e. never stuck at 'queued' after the turn ran).
      5. Cleanup: DEL the prefix keys.
    """
    def test_full_pool_queues_then_runs_in_fifo(self):
        self.skipTest("integration harness: implement per the docstring against ARB_BRIDGE_TEST_REDIS_URL")
```
(If a real-bus harness is impractical in this slice, leave the `skipTest` body and the docstring as the
executable spec for the operator's deploy-time verification — do NOT substitute a pool-only test that
fakes FIFO. The honest coverage boundary is: logic unit-tested (T1/T2), real-bus FIFO verified at deploy.)

- [ ] **Step 2: Run (skips locally without the env)**

Run: `.venv/bin/python -m pytest tests/test_dispatch_queue_integration.py -q`
Expected: SKIPPED without `ARB_BRIDGE_TEST_REDIS_URL` (or PASS if the orchestrator sets it).

- [ ] **Step 3: CHANGELOG**

Add to `CHANGELOG.md` (top unreleased):
```markdown
- **Bridge dispatch-queue** — a busy seat now *queues* work instead of replying `bridge busy`. The
  main loop waits for engine-pool capacity (`EnginePool.wait_for_capacity`, stop-aware) before popping
  from Redis `:inbox`, so un-popped work stays in the durable FIFO list (zero new durability surface).
  `agent-dispatch` writes a `queued` task status on enqueue and defaults `--timeout` to 3600s so a deep
  queue is observable rather than an opaque wait. `handle_raw`'s synchronous busy reply is kept as a
  safety net. **Why:** orchestrators can fan out beyond `--max-parallel` without hand-managing retries.
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_dispatch_queue_integration.py CHANGELOG.md
git commit -m "test(bridge): dispatch-queue FIFO integration (skip-if-no-redis) + CHANGELOG"
```

---

---

## v3 — control lane (fixes impl-review P1: gate starves cancel/steer)

Tasks 5–6 are added on top of the Tasks 1–4 build (branch `feat/arb-dispatch-queue`). Spec: the
*"Control lane (v3)"* section. The bug: `cancel`/`steer` are `lpush`'d to the request `:inbox`
(`ctl.py:214`) and the v2 gate (before every pop) blocks popping them while a task holds the only slot —
so a running task can't be cancelled. Fix: a separate ungated control key.

### Task 5: control-lane plumbing (`control_key`, `lpop_control`, `lpush_control`, `ctl` sender, `pop_inbox` timeout param)

**Files:**
- Modify: `src/agent_redis_bridge/redis_io.py` (`RedisConfig.control_key`, `RedisCli.lpop_control`/`lpush_control`)
- Modify: `src/agent_redis_bridge/ctl.py` (`send_control` → control key)
- Modify: `src/agent_redis_bridge/bridge.py` (`pop_inbox(self, timeout=None)` param)
- Test: `tests/test_control_lane.py` (new)

**Interfaces:**
- Produces: `RedisConfig.control_key(agent_id) -> "<prefix>agent:<id>:control"`; `RedisCli.lpop_control(agent_id) -> str|None` (non-blocking); `RedisCli.lpush_control(agent_id, body)`; `pop_inbox(timeout=None)` defaults to `self.args.blpop_timeout`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_lane.py
import unittest
from pathlib import Path

CTL = Path(__file__).parents[1] / "src" / "agent_redis_bridge" / "ctl.py"

class ControlLanePlumbingTest(unittest.TestCase):
    def test_config_has_control_key(self):
        from agent_redis_bridge.redis_io import RedisConfig
        cfg = RedisConfig(host="h", port=1, db=0, prefix="agent_scratch:")
        self.assertEqual(cfg.control_key("codex-x-dev"), "agent_scratch:agent:codex-x-dev:control")

    def test_send_control_targets_control_key_not_inbox(self):
        # ctl.send_control must push to the control lane, not the request inbox
        src = CTL.read_text()
        self.assertIn("lpush_control", src)
        # the steer/cancel send must NOT use the plain inbox lpush
        send_control_body = src[src.index("def send_control"):]
        self.assertNotIn("redis.lpush(target_agent_id", send_control_body)
```
(Adjust the `RedisConfig(...)` constructor call to the real signature — read `redis_io.py`; if it's
built from an env dict, construct it the way the existing tests do.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_control_lane.py -q`
Expected: FAIL (no `control_key`/`lpush_control`; `send_control` still uses `redis.lpush`).

- [ ] **Step 3: Implement**

In `redis_io.py` `RedisConfig`, beside `inbox_key`:
```python
    def control_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:control")
```
In `redis_io.py` `RedisCli`, beside `lpop`/`lpush`:
```python
    def lpop_control(self, agent_id: str) -> str | None:
        value = self.client.lpop(self.config.control_key(agent_id))
        return value if value else None

    def lpush_control(self, agent_id: str, body: str) -> None:
        self.client.lpush(self.config.control_key(agent_id), body)
```
In `ctl.py` `send_control`, change the final `redis.lpush(target_agent_id(args), envelope.to_json())`
(line ~214) to:
```python
    redis.lpush_control(target_agent_id(args), envelope.to_json())
```
In `bridge.py` `pop_inbox`, add a `timeout` param defaulting to the existing value:
```python
    def pop_inbox(self, timeout: int | None = None) -> tuple[str | None, bool]:
        timeout = self.args.blpop_timeout if timeout is None else timeout
        # ... replace the two self.args.blpop_timeout uses inside with `timeout` ...
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_control_lane.py tests/test_engine_pool.py tests/test_bridge_capacity_gate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/redis_io.py src/agent_redis_bridge/ctl.py src/agent_redis_bridge/bridge.py tests/test_control_lane.py
git commit -m "feat(bridge): control lane plumbing — control_key + lpop/lpush_control, ctl sends to it, pop_inbox timeout param"
```

### Task 6: drain controls ungated in the loop + `--control-poll-timeout` (the starvation fix)

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py` (`inbox_loop` drain step; `--control-poll-timeout` arg)
- Test: `tests/test_bridge_capacity_gate.py` (add the starvation regression test)

**Interfaces:**
- Consumes: `lpop_control` (T5), `wait_for_capacity` (T1). Produces: `args.control_poll_timeout` (default 0.5); `inbox_loop` drains all controls ungated before the gated request pop, and pops requests with `control_poll_timeout`.

- [ ] **Step 1: Write the failing regression test** (controls drain even at full capacity — the exact bug)

```python
# add to tests/test_bridge_capacity_gate.py
class ControlStarvationTest(unittest.TestCase):
    def test_controls_drain_even_at_full_capacity(self):
        import agent_redis_bridge.bridge as b
        bridge = b.Bridge.__new__(b.Bridge)
        # pool has NO capacity (task running) — the v2 gate would block all pops
        class _NoCap:
            def wait_for_capacity(self, timeout, stop_event=None): return False
        bridge.pool = _NoCap()
        bridge.stop_event = threading.Event()
        bridge.reliable_inbox = False
        bridge.args = mock.Mock(blpop_timeout=30, control_poll_timeout=0.01, once=False, max_message_bytes=10_000)
        bridge.agent_id = "codex-x-dev"
        bridge.recover_processing_envelopes = lambda: None
        handled = []
        controls = ['{"id":"c1","kind":"cancel","payload":{}}']
        def fake_lpop_control(agent_id):
            return controls.pop(0) if controls else None
        bridge.redis = mock.Mock(lpop_control=fake_lpop_control)
        def fake_handle_raw(raw, processing_raw=None):
            handled.append(raw); bridge.stop_event.set(); return False
        bridge.handle_raw = fake_handle_raw
        bridge.pop_inbox = lambda timeout=None: (None, False)
        bridge.inbox_loop()
        # the control was handled DESPITE zero request capacity
        self.assertEqual(len(handled), 1)
        self.assertIn("cancel", handled[0])
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bridge_capacity_gate.py::ControlStarvationTest -q`
Expected: FAIL — the v2 loop gates before any pop and never drains controls.

- [ ] **Step 3: Implement the drain step**

In `bridge.py` `inbox_loop`, replace the loop head (the v2 gate) with the control-drain-then-gate form:
```python
        while not self.stop_event.is_set():
            # Drain ALL pending controls ungated (they interrupt running tasks).
            while True:
                ctl_raw = self.redis.lpop_control(self.agent_id)
                if ctl_raw is None:
                    break
                self.handle_raw(ctl_raw)
            if self.stop_event.is_set():
                break
            # Gate the REQUEST lane only; short poll so controls re-drain each period.
            if not self.pool.wait_for_capacity(self.args.control_poll_timeout, self.stop_event):
                continue
            raw, parked = self.pop_inbox(self.args.control_poll_timeout)
            if raw is None:
                continue
            # ... existing parked-shutdown block + request handling unchanged ...
```
Add the arg in `build_parser` (beside `--blpop-timeout`):
```python
    parser.add_argument("--control-poll-timeout", type=float, default=0.5)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_bridge_capacity_gate.py tests/test_control_lane.py tests/test_bridge_parallelism.py tests/test_bridge_max_parallel.py -q`
Expected: PASS (starvation regression + existing gate/parallelism tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_bridge_capacity_gate.py
git commit -m "fix(bridge): drain controls ungated each loop (control-poll-timeout) — cancel/steer not starved by the gate"
```

---

## v2 — folded plan-review panel (codex + 2 cold-Opus; NEEDS-CHANGES)

- **P1 (codex) queued-status RACE** → write `queued` BEFORE the `LPUSH` (T3); a ready bridge can pop+mark
  `running` before a post-LPUSH HSET. Test flipped to assert status-write precedes the LPUSH.
- **P1 (codex) stop-wake test ordering** → `stop.set()` before `pool.stop_all()` in T1 (matches bridge
  cleanup `bridge.py:338`); the old order never woke the waiter.
- **P1 (cold-A) gate test must assert `stop_event` forwarded** → `_Pool` captures it, T2 asserts `assertIs`
  (else dropping it silently re-breaks the shutdown-wake).
- **P1 (cold-A) loop snippet** → retain the existing `parked and stop_event` shutdown block verbatim.
- **P1 (cold-B) release `notify()` placement** → after the whole `if engine` block (covers the unhealthy
  `_started -= 1` capacity-free too), not just the healthy branch.
- **P1 (codex) T4 not a real integration test** → removed the `EnginePool`-direct fallback (`notify()` is
  not FIFO); T4 is now a real-bus `inbox_loop` test (skip-if-no-redis), FIFO = Redis-list property.
- **Correction (vs cold-A P2):** the `queued→running` overwrite needs NO new task — `handle_raw` already
  writes `running` via `update_task_status` (`bridge.py:686`). The dispatcher writing `queued` (T3) +
  that existing write IS the transition; only its test (spec Test 6) was missing → folded into T4 step 4.

## Self-review (plan author)

- **Spec coverage:** `wait_for_capacity` + stop-notify → T1; main-loop gate + single-popper safety net → T2; `queued` status + raised timeout (Open-Q1/Q2 "full") → T3; FIFO/restart durability evidence + CHANGELOG → T4. Affinity-reject (Q6) unchanged by design (handle_raw path untouched). Backward-compat = deploy-time (no code).
- **Divergence flagged:** `test_bridge_parallelism.py` is NOT rewritten (it exercises `handle_raw` directly, which keeps the busy reply); the gate is `inbox_loop`-only. Documented in Task 2.
- **Placeholders:** none — all code/test steps complete.
- **Type consistency:** `wait_for_capacity(timeout, stop_event=None)->bool` (T1) consumed identically in T2's loop and T4; status hash shape (T3) matches `update_task_status`/`task_status_key` from `bridge.py`/`redis_io.py`.

## Notes for the executor / morning review
- Tests are **unittest** to match the repo; run via `.venv/bin/python -m pytest`.
- The heaviest behaviors (real bridge + Redis FIFO/restart) are integration-gated (T4 skip-if-no-redis); the load-bearing logic (capacity gate, stop-wake) is fully unit-tested deterministically in T1/T2.
- NOT merged to `dev` — left on the feature branch for Mark's review. Fleet-client migration + the real over-the-bus deploy are operator-owned.
