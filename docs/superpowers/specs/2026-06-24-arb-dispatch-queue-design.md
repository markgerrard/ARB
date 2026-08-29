# ARB dispatch queue — design spec

**Status:** spec **v3** (control-lane folded after the impl-review P1)
**Date:** 2026-06-24

**✅ RESOLVED (2026-06-24, 3-seat panel UNANIMOUS) — make it FIFO via Option A.** All three seats
(codex + 2 cold-Opus) agreed SHOULD-BE-FIFO and APPROVE Option A: the request producers (`agent-dispatch`
+ `ctl send`) switch `LPUSH`→`RPUSH` via a NEW request-specific `RedisCli.rpush`; the bridge
pop/reliable-`BLMOVE`/recover machinery is UNCHANGED (RPUSH-tail + pop-head = oldest-first; recover
`RIGHT→LEFT` restores parked work oldest-first, no loss). Do NOT change `RedisCli.lpush` globally —
replies/notifies use it for caller inboxes (`bridge.py:1494/1712`). Control lane stays `LPUSH` (drained
all-at-once, order-insensitive). Option B (flip the bridge pop side) rejected: 3 coupled edits to the
durability spine for no gain.

**⚠ EMPIRICAL FINDING (2026-06-24, post-build) — the queue is LIFO, not FIFO.** This spec repeatedly
calls the inbox "FIFO", but the bridge does `LPUSH` (sender, `agent-dispatch:356`/`ctl.py:194`) + pop from
the **LEFT** (`blpop`/`blmove_to_processing` use `LEFT`, `redis_io.py`). `LPUSH a,b,c` then `BLPOP` returns
`c,b,a` — verified live on local Redis. So it's a **stack**: the most-recently-dispatched task runs first,
and under sustained load old queued tasks can **starve**. This is *pre-existing* inbox behaviour (the gate
change doesn't alter ordering — it only changes *when* the bridge pops), but queueing now makes it
observable and load-bearing. **Open decision for the operator:** accept LIFO, or make it FIFO — the latter
is a small change (dispatcher `RPUSH` instead of `LPUSH`, or bridge pop from the RIGHT) but it interacts
with the reliable-inbox `BLMOVE LEFT→RIGHT` parking + `recover_processing_*` ordering, so it's not a blind
one-liner. The whole review panel missed this by reasoning from the word "FIFO" instead of running it.

**v3 — separate control lane (operator decision after the impl-review caught a P1):** the v2 gate sat
before EVERY `pop_inbox`, but `cancel`/`steer` controls are `lpush`'d to the **same** inbox the bridge
pops (`ctl.py:214`) and routed after the pop (`bridge.py:506`). Under `max_parallel=1`, a running task
holds the only slot → the capacity gate blocks the pop → the cancel meant to interrupt that very task is
never reached. Fix below: controls get their **own key**, drained ungated; the capacity gate applies only
to the request lane. See *"Control lane (v3)"* below.

**v2 — folds the 3-seat design-review panel (codex contributor + 2 cold-Opus certifying; unanimous
NEEDS-CHANGES, all verified against the bridge code):**
- **P1 shutdown wake:** `wait_for_capacity` is now **stop-aware** and `stop_all()` **notifies** waiters —
  a waiter at full capacity no longer hangs up to one `blpop_timeout` on shutdown.
- **P1 dispatcher-timeout / visibility (Open-Q1+Q2, DECIDED — "full"):** the dispatcher writes a per-task
  `queued` status on enqueue and the default `--timeout` is raised, so a deep queue is observable and
  doesn't surface as an opaque exit-124. The earlier "no dispatcher change needed" is **superseded**.
- **Backward-compat — NOT a gate (operator decision 2026-06-24):** the fleet clients are migrated to the
  new queued behaviour as part of the deploy, so there is **no opt-in flag and no fast-reject preservation**
  — single clean flip. The only in-repo change is rewriting the bridge's own
  `tests/test_bridge_parallelism.py:98,127` (they assert the busy reply on capacity exhaustion; that path
  changes), which is part of this change, not a compat concern.
- **Single-popper invariant** documented as the load-bearing precondition the gate depends on.

**Related:**
- Backlog entry: `docs/BACKLOG.md` — *ARB dispatch-queue — queue work to agents instead of bouncing*
- Bridge parallelism doc: `docs/bridge-parallelism.md` (engine pool / `--max-parallel`)
- "bridge busy" failure shape: `skills/using-agent-bridge/SKILL.md`
- Existing capacity tests: `tests/test_engine_pool.py`, `tests/test_bridge_max_parallel.py`

This spec captures investigation findings from a 2026-06-24 dig and proposes an implementation path. It is **not yet a plan** — the open questions at the end need a decision before promotion to `docs/superpowers/plans/`.

---

## Current behavior (verified by code read 2026-06-24)

### Where "bridge busy" comes from

`src/agent_redis_bridge/bridge.py:573-585`:

```python
engine = self.pool.acquire(envelope.id, thread_id=...)
...
if engine is None:
    active_ids = self.pool.active_task_ids()
    busy_summary = ",".join(active_ids) if active_ids else "unknown"
    self.send_reply(envelope, TurnResult(
        ok=False, result="",
        error=f"bridge busy with task {busy_summary}",
    ))
    return False
```

`EnginePool.acquire(task_id, thread_id=None)` (`src/agent_redis_bridge/engine_pool.py:61-94`) returns:

- An engine if pool has idle capacity or hasn't yet reached `max_size`.
- `None` when **the pool is at clean capacity exhaustion** (no thread-id constraint, all slots busy).
- Raises `AffinityMissError` / `AffinityBusyError` / `AffinityAmbiguousError` when a `thread_id` is requested and the affinity rule can't be satisfied.

**Crucial distinction:** the capacity-exhaustion path (returns `None`) is a *transient* condition that resolves when any in-flight task releases. The affinity exceptions are *hard* conflicts — a different task owns the thread, or there's an ambiguity — that won't resolve by waiting and SHOULD continue to reject fast. The queue work only applies to the `None` case.

### What dispatchers see today

`scripts/agent-dispatch:347`:

```bash
raw=$(redis-cli "${REDIS_FLAGS[@]}" BLPOP "${PREFIX}agent:${FROM}:inbox" 5 | tail -n1)
```

The dispatcher BLPOPs the caller's `:inbox` with a 5s timeout (in a loop bounded by the overall `--timeout`/`TIMEOUT` env var). Today it receives the `ok:false bridge busy` reply quickly and must decide whether to retry. **There's no client-side retry; the bash returns the busy error to the caller** — orchestrators get a hard failure on the N+1th dispatch against an N-slot seat.

### Why the main loop is the right hook point

`src/agent_redis_bridge/bridge.py` main loop (around line 385):

```python
raw, parked = self.pop_inbox()           # BLPOPs envelope from agent_id inbox
...
worker_owns_processing = self.handle_raw(raw, processing_raw=raw if parked else None)
```

`pop_inbox()` pulls an envelope synchronously, regardless of pool state. There is no buffer between Redis pop and capacity check, so capacity exhaustion is the same event as the caller-visible rejection. Anywhere we add buffering must sit BEFORE `pop_inbox()` or it duplicates work that Redis is already doing.

The `reliable_inbox` path uses `blmove_to_processing` to park in-flight envelopes in `:processing` for restart-time recovery (`recover_processing_envelopes` at line 452). This is the bridge's durability story: **work that's been popped is durable in `:processing`; work that hasn't been popped is durable in `:inbox`**. The proposed queue design preserves this exactly.

---

## Options considered

| | A: client retry wrapper | B: capacity-gated BLPOP **(recommended)** | C: in-memory pending deque |
|---|---|---|---|
| **Where the change lives** | `scripts/agent-dispatch` | `bridge.py` + `engine_pool.py` | `bridge.py` |
| **LoC estimate** | ~20 bash | ~50 Python + tests | ~30 Python + tests |
| **Durability** | N/A (no queue) | Inherits Redis durability of the `:inbox` list | None — bridge crash loses the queue |
| **FIFO across racing dispatchers** | No (whoever wins the BLPOP race goes first) | Yes (Redis list is FIFO; pop-when-ready preserves order) | No (in-process queue races against new Redis pops) |
| **Bounces through Redis on contention** | Yes (RPUSH→BLPOP per retry) | No | No |
| **Dispatcher-side behaviour change** | None | "Bridge busy" stops firing for capacity → dispatcher block-and-waits until processed | Same as B |
| **Cross-process effect** | Per-dispatcher | Per-bridge-process (every bridge using this code) | Per-bridge-process |

**Why B over C.** Same wall-clock behaviour but C invents a parallel "where does work live" — an in-process deque invisible to the existing durability path. Bridge crash loses the queued tasks. B uses Redis as the queue by simply not popping until ready — zero new durability surface, and the existing `recover_processing_envelopes` startup-time scan continues to do the right thing.

**Why B over A.** A is the cheap intermediate fix but it's a fragile pattern: every dispatcher reimplements queue management, FIFO is lost under contention, and Redis traffic grows linearly with retry depth. The user's existing backlog entry already endorses moving past A.

---

## Recommended design — Option B in detail

### EnginePool — add condition-variable signalling

```python
# engine_pool.py
class EnginePool(Generic[T]):
    def __init__(self, factory, max_size):
        ...
        self._cap_cond = threading.Condition(self._lock)   # NEW

    def release(self, task_id):
        unhealthy = None
        with self._lock:
            engine = self._busy.pop(task_id, None)
            if engine is not None:
                ...                                         # existing health-check logic
            self._cap_cond.notify()                         # NEW: wake one waiter
        ...

    def wait_for_capacity(self, timeout: float | None, stop_event: threading.Event | None = None) -> bool:
        """Block until a slot is free OR stop is signalled, or timeout elapses.

        Returns True if capacity is available, False on timeout or stop. (v2/P1: stop-aware so the
        main loop wakes immediately on shutdown instead of waiting out a full `blpop_timeout`.)
        """
        def _ready():
            return self._has_capacity_unlocked() or (stop_event is not None and stop_event.is_set())
        with self._lock:
            if _ready():
                return self._has_capacity_unlocked()   # False if we woke only because of stop
            self._cap_cond.wait_for(_ready, timeout=timeout)
            return self._has_capacity_unlocked()

    def stop_all(self):
        # v2/P1: existing teardown PLUS wake any capacity waiter so shutdown isn't worst-case
        # one blpop_timeout. The waiter re-checks stop_event via the _ready predicate and exits.
        with self._lock:
            ...                                          # existing engine teardown
            self._cap_cond.notify_all()

    def _has_capacity_unlocked(self) -> bool:
        # idle engines + unspawned-but-allowed slots
        return len(self._idle) > 0 or self._started < self._max_size
```

Notes:
- `release()` uses `notify()` (one slot freed → one waiter wakes); `stop_all()` uses `notify_all()` (no
  slot freed, but every waiter must re-check `stop_event` and leave). Single-waiter today, but `notify_all`
  on stop is correct regardless of waiter count.
- `_cap_cond` shares the existing `self._lock` so the predicate is checked under the same lock that mutates
  `_busy`/`_idle`/`_started`. No new lock ordering.
- The caller passes `stop_event`, so `wait_for_capacity` returns promptly on shutdown even at full capacity;
  it returns `True` only when a real slot exists (a stop-only wake returns `False`).

### Bridge main loop — gate the BLPOP

```python
# bridge.py main loop (around line 385)
while not self.stop_event.is_set():
    if not self.pool.wait_for_capacity(self.args.blpop_timeout, self.stop_event):
        # Timed out, or woke for shutdown — loop to re-check stop_event.
        continue

    raw, parked = self.pop_inbox()
    if raw is None:
        continue
    # On acquire (transition queued->running), overwrite task:<id>:status = running.
    ...
```

`wait_for_capacity` ensures we only pop when we can actually start. Queued work stays in Redis as plain `:inbox` entries until the bridge is ready — durability identical to "the bridge happened to not be running yet."

### Control lane (v3) — controls must NOT be capacity-gated

**Problem (impl-review P1):** `cancel`/`steer` target a *running* task, i.e. they matter precisely when
capacity is full. If they share the request `:inbox`, the capacity gate blocks the bridge from popping
them, so a running task can't be cancelled until it finishes on its own. The gate's "don't pop until
capacity" premise is incompatible with the shared inbox.

**Design — a separate, ungated control key:**
- **New key** `control_key(agent_id)` = `<prefix>agent:<id>:control` (alongside `inbox_key`).
- **Sender:** `ctl.send_control` (`ctl.py:214`) `lpush`es `steer`/`cancel` envelopes to `control_key`
  instead of the request `inbox_key`. (The `ctl send` *request* path at `ctl.py:194` is unchanged.)
  Controls are **not** reliable-parked (no `:processing`) — they're ephemeral interrupts; a control lost
  on a bridge crash is moot because the task it targets dies with the bridge.
- **Bridge loop** services the two lanes asymmetrically — controls first/ungated, requests gated:
  ```python
  while not self.stop_event.is_set():
      # 1. Drain ALL pending controls, ungated (they interrupt running tasks).
      while (c := self.redis.lpop_control(self.agent_id)) is not None:
          self.handle_raw(c)                     # routes steer/cancel via handle_control
      # 2. Gate the REQUEST lane only. Short poll so controls re-drain promptly.
      if not self.pool.wait_for_capacity(self.args.control_poll_timeout, self.stop_event):
          continue
      # 3. Pop a request, blocking for at most control_poll_timeout (NOT blpop_timeout=30s) so the
      #    loop returns to drain controls within one poll period in every case.
      raw, parked = self.pop_inbox(self.args.control_poll_timeout)   # request inbox only (reliable as before)
      if raw is None:
          continue
      # ... existing request handling ...
  ```
- **Responsiveness:** the loop period is `control_poll_timeout` (a new arg, default **0.5s**) in BOTH the
  gate-wait and the request pop, so a newly-arrived control is drained within ≤ one poll period in EVERY
  case (task running, idle-with-capacity, or multi-parallel with a free slot). This is a strict *improvement*
  over today, where a control can wait behind a 30s blocking `BLPOP`. The cost is idle request-inbox polling
  at ~`1/control_poll_timeout` per second (~2/s) instead of every `blpop_timeout` (30s) — trivial (a
  blocking `BLPOP`, not a busy loop; nowhere near the per-token hot-path the managed-bus backpressure rule
  warns about). `pop_inbox` gains a `timeout` param (defaulting to `blpop_timeout` for any non-gated caller).
- **Why not multi-key `BLPOP [control, inbox]`:** it would wake on either lane in one call, but the
  reliable-inbox path uses `BLMOVE` (single-key, no multi-key form), so controls (plain pop) and
  requests (reliable move) can't share one atomic blocking call. The drain-then-gated-poll loop keeps the
  reliable-request path intact and is simpler. (Recorded as the considered-and-rejected alternative.)

**New plumbing:** `RedisCli.lpop_control(agent_id)` (non-blocking `LPOP` on `control_key`),
`RedisCli.lpush_control(agent_id, body)` (used by `ctl.send_control`), `RedisConfig.control_key`, and a
`--control-poll-timeout` bridge arg (default 0.5s). `handle_raw`/`handle_control` are unchanged — only the
delivery key and the loop's drain step are new.

### "Bridge busy" reply — narrow it to affinity-only

The `engine is None` branch at `bridge.py:573-585` becomes unreachable in normal flow because we never call `acquire` without capacity. The branch should stay as a safety net but its error string should reflect that it's now an unexpected condition (e.g. log loudly and reply `bridge-acquire-unexpected-none`). The `AffinityBusyError` / `AffinityAmbiguousError` / `AffinityMissError` branches (lines 558-572) are unchanged — they continue to reject fast because their conditions don't resolve by waiting.

### Dispatcher-side — TWO changes (v2; supersedes "no changes")

The Open-Q1+Q2 decision is **"full"**: make a deep queue observable rather than an opaque longer wait.

1. **Write a `queued` status on enqueue.** After the `LPUSH` to `:inbox`, `agent-dispatch` writes
   `task:<id>:status = {state: "queued", queue_depth: <LLEN :inbox>, enqueued_at: <ts>}`. The bridge
   overwrites this to `running` when it pops+acquires (see main-loop note above), and to the terminal
   state on completion. This makes "queued, be patient" distinguishable from "stuck" at the per-task level.
   - **Single-writer caveat:** the dispatcher writes ONLY the initial `queued` record; once the bridge
     pops, the bridge owns the status key. The two writers never race because the bridge's first write
     happens strictly after the pop, which happens strictly after the dispatcher's enqueue. (Prefer a
     small Redis Lua script doing `LPUSH` + `HSET queued` atomically if you want the status to be
     guaranteed present the instant the envelope is queued.)
2. **Raise the default `--timeout`.** The current default (1800s) can be exceeded by
   `(queue_depth / max_parallel) × per_turn_time`. Raise the default (proposed **3600s**) and document
   the new wait model in `usage()` and the bridge skill. A caller can still narrow it explicitly.

Visible behaviour shift: "bridge busy" stops arriving for capacity exhaustion; instead the dispatch shows
`queued` (with depth) and waits. The operator/orchestrator can read `task:<id>:status` to see queue depth.

---

## Test coverage to add

Place under `tests/test_dispatch_queue.py`:

1. **`wait_for_capacity` blocks until release** — submit `max_size` tasks, assert `wait_for_capacity(0.05)` returns `False`; release one; assert next `wait_for_capacity` returns `True` immediately.
2. **FIFO across racing dispatchers** — Redis-backed integration test: enqueue 4 envelopes into a `max_parallel=2` bridge; assert all 4 execute in dispatch order, none are rejected with `bridge busy`.
3. **AffinityBusyError still rejects fast** — when a thread-id collision occurs, the reply is still `ok:false thread-affinity-busy`, not a queue wait.
4. **`stop_all` wakes a waiter immediately (v2/P1)** — full capacity, a waiter pending in
   `wait_for_capacity(timeout=10, stop_event)`; call `stop_all()` / set `stop_event`; assert the waiter
   returns `False` in well under the 10s timeout (not after it). This is the regression test for the
   shutdown-notify fix — it must FAIL against a `stop_all` that doesn't `notify_all`.
5. **Bridge restart preserves queue** — enqueue 4 envelopes against a `max_parallel=1` bridge, let it process 1, kill -9, restart, assert the remaining 3 still execute. (Passes with **zero** new code on top of B — Redis is the queue.)
6. **`queued` status is written and transitions (v2/Q2)** — enqueue against a full `max_parallel=1`
   bridge; assert `task:<id>:status.state == "queued"` with a `queue_depth`; release the running task;
   assert the queued one transitions `queued -> running -> <terminal>`.
7. **Rewrite `tests/test_bridge_parallelism.py:98,127` (v2/backward-compat)** — these currently assert
   `"bridge busy"` fires on capacity exhaustion. Under B that path no longer fires for capacity; rewrite
   them to assert the queued-then-run behaviour, and keep a SEPARATE assertion that `bridge busy` /
   affinity-reject still fires for the *affinity* conflict case (which is unchanged).

Tests 2, 5, 6 are integration-level (real Redis); existing `tests/` helpers cover that.

---

## Open questions — disposition (v2)

1. **Dispatcher overall timeout — RESOLVED (operator decision: "full").** Raise the default `--timeout`
   (proposed 3600s) AND write a per-task `queued` status with `queue_depth`. See *Dispatcher-side* above.
   Both, not either — a deep queue is now observable, not an opaque longer wait.

2. **Visibility of queued vs running — RESOLVED (operator decision: "full").** Add the `queued` status
   entry on enqueue (dispatcher-side, or a `LPUSH`+`HSET` Lua script for atomicity). Not "just `LLEN`."

3. **Per-seat vs shared queue — DEFER (documented).** Per-seat (each bridge owns its inbox). Cross-seat
   spillover would need a consumer-group on a shared list; out of scope for this slice.

4. **Priority / ordering — DEFER (documented).** Plain FIFO via `LPUSH`/`BLPOP`; priority (multiple lists
   / sorted sets) is out of scope. FIFO matches the fan-out use case.

5. **At-least-once vs at-most-once on requeue — CLOSED: N/A for B.** We never pop without intent to
   process, so there's no in-process pending set to requeue. (Only relevant to the rejected Option C.)

6. **Affinity-busy after queue — RESOLVED: reject (documented).** A queued thread-id envelope that waits,
   pops, then hits `AffinityBusyError` is **rejected**, not requeued (requeue invites starvation against
   the slot-holder). Verified leak-free: the reject path's `inbox_loop` finally drains `:processing`
   (`bridge.py:411-415`), so the envelope is consumed cleanly.

---

## Load-bearing invariant (v2 — state it in the plan)

**The main loop is the SOLE acquirer.** `acquire` for real envelopes is called only from the main loop
(`bridge.py:554`) and from single-threaded warmup (`:375`); worker threads only ever `release` (`:803`).
That is *why* the `engine is None` capacity branch becomes unreachable after gating — nothing can steal a
slot between `wait_for_capacity()` returning True and `acquire()` taking it. **Keep the `engine is None`
branch as a loud safety net** (log + reply `bridge-acquire-unexpected-none`); if a future change ever adds
a second acquiring thread (e.g. a control-plane dispatch), the gate becomes a TOCTOU and this branch fires
again — that's the tripwire.

## Implementation pointers (for the plan session)

- **Files touched:** `engine_pool.py` (`wait_for_capacity` + cond var + `stop_all` notify), `bridge.py`
  (gate `pop_inbox`, pass `stop_event`, `queued->running` status write, narrow `engine is None`),
  `scripts/agent-dispatch` (write `queued` status on enqueue, raise default `--timeout`),
  `tests/test_bridge_parallelism.py` (rewrite the two `bridge busy` capacity assertions).
- **Baseline tests pass:** `pytest tests/test_engine_pool.py tests/test_bridge_max_parallel.py -q` → 19/19 (verified 2026-06-24).
- **No schema changes** (Redis keys unchanged; `task:<id>:status` gains a `queued` state value).
- **Backward-compat — not a gate (operator decision):** only `tests/test_bridge_parallelism.py:98,127`
  key on `"bridge busy"` locally — rewrite per Test 7. Fleet clients (`project-g-*` and one other legacy client) are migrated to the
  new behaviour **at deploy time by the operator**, so no opt-in flag and no compat shim are built.
  (Clients matching the old fast-reject string, or running tight liveness timeouts against seats, are
  updated as part of that deploy — operator-owned, not a code gate here.)
- **Rollout:** single clean PR; the contract flip ships with the deploy that migrates the fleet clients.

---

## Cross-references

- Memory: `[[arb-dispatch-queue-backlog]]` in the project-b memory (the requester-side context for *why* this matters).
- Memory: `[[arb-bridge-seats]]` for the seat-setup convention this change targets.
- Methodology: ARB-side tri-model review pattern (cold-Opus + GLM + agy, with the implementor as non-certifying reviewer) per the gate methodology corrections in `[[arb-bridge-seats]]`. Recommended for this change because it touches the core dispatch path.
