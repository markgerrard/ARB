# Turn-liveness heartbeat for events:live — design

> Status: built on `fix/agy-print-heartbeat`. Closes the follow-up filed 2026-06-28
> (memory `agy-print-heartbeat-followup`).

## Problem

arb-watch's seat roster freshness is driven by `last_event_ts` on the `events:live` stream
(`arb_memory/visibility.py` builds the roster from that stream). A seat reads **stale** when no
new `events:live` entry has flowed recently — even if its turn is alive and progressing.

This bites **burst-emitting engines**. codex (app-server) emits a continuous event stream, so it
never looks stale. **agy-print is a one-shot `agy --print`** whose granular events arrive in bursts
(SQLite-poll of the conversations DB); between bursts — while agy is blocked on the model API
(observed: process alive, state `S`, ~0% CPU, 8 min into a turn) — no `events:live` entry flows, so
`last_event_ts` ages and the seat reads stale despite being healthy. Verified live on run
`agyp-field3-20260628`: agy emitted 10 events vs codex's 112 in the same panel.

The bridge already has liveness signals, but **neither keeps an active-but-quiet seat fresh in the
roster**: `heartbeat_loop` → `reassert_liveness()` reasserts the **registry** TTL (keeps the seat
*registered*, emits no `events:live`); `_last_stream_heartbeat` updates only on **streaming-response
events**, which agy-print does not produce during model-wait. So freshness tracks *event cadence*,
not *process liveness*.

## Design (engine-agnostic, in the bridge)

While a turn is active, the bridge tees a periodic `turn_heartbeat` to `events:live` so the roster's
`last_event_ts` reflects process liveness. Gated on `run_id` exactly like every other live tee.

- **State:** reuse `active_requests` (already `id → envelope`, holding `run_id`/`sender`); add
  `_last_live_tee_ts: dict[task_id → monotonic]`, set on every `_tee_live_event` and wiped in the
  per-task `finally` (same lifecycle as `_last_stream_heartbeat`). Because a heartbeat-thread tee can
  re-insert a key *after* the per-task `finally` popped it (a snapshot/finally race the review panel
  flagged on all three seats), `_emit_turn_heartbeats` also **prunes throttle keys for turns no longer
  in `active_requests` at the start of each tick** — bounding that residue to a single tick rather than
  leaking until restart, without holding `active_lock` across the (possibly remote) tee.
- **Throttle:** `_emit_turn_heartbeats(now)` emits only when `now - last_live_tee >= heartbeat_interval`
  — so a **chatty** turn (codex) self-throttles to zero heartbeats (its real events keep `last` fresh),
  while a **quiet** turn (agy-print mid model-wait) gets one heartbeat per interval. After emitting it
  advances the throttle on the caller's clock.
- **Hook:** `heartbeat_loop` calls `_emit_turn_heartbeats(time.monotonic())` each tick, in its **own**
  try/except — a tee failure must NOT increment `heartbeat_failures` (which kills the bridge at 3). A
  stale roster row is not fatal; fail-soft.
- **Gating:** no `run_id` → no heartbeat (consistent with `_tee_live_event`, eval, votes).

## What it deliberately does NOT do

- No new thread (reuses `heartbeat_loop`).
- No per-token / hot-path work (the managed-bus backpressure rule stands — heartbeat fires at most
  once per `heartbeat_interval` per active turn).
- Does not change registry liveness, `_last_stream_heartbeat`, or any engine.

## Tests

`tests/test_bridge_turn_heartbeat.py`: emits for a quiet run_id turn; no emit when a real event flowed
within the interval; no emit without run_id; throttle resets so the next tick stays quiet. Non-vacuity
mutation-verified (suppressing the emit reds the two emit-tests).
