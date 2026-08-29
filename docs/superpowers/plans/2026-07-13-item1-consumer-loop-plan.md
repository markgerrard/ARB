# PLAN — Item 1 consumer-loop robustness (build ON prep; TDD, luna@high)

**Spec:** `docs/superpowers/specs/2026-07-13-item1-consumer-loop-SPEC.md` v2 · **Design (mechanism):**
`2026-07-12-consumer-loop-robustness-design.md` v6 · **Effort:** high · **Prereq:** `feat/prep-slice`
merged to `dev`. **Worktree:** `--worktree item1`. **Branch:** `feat/item1-consumer-loop`.
**Env:** `uv run --extra arb-memory pytest tests/arb_memory`. Tests **drive `run()`** (template
`tests/test_audit_close_consumer.py:155`), never `_handle_entry` directly.

## Ordering (each step: RED test → GREEN impl)

1. **`consumer_loop.py` — `StreamConsumerLoop` mixin + Event migration + cursor** (5 write-like consumers;
   `ReadLoop` gets Event+backoff ONLY).
   - RED: `test_consumer_loop.py` — `run()` drives `read_modes` incl. cursor-advanced pending; `stop()`
     joins < 2 s; a `ReadLoop` test proves cleanup-only pending is preserved (no recirculation, no-stale-
     reply `test_bus_pel.py:107-122` extended to `run()`); a **run-driven proof that an acked malformed
     *terminal* entry (returns truthy) does NOT halt subsequent pending work** (plan panel P1 / spec truthy-
     terminal test).
   - GREEN: mixin `run()`/`_tick()`/`step()`/`drain_pending()`/`stop()`; migrate all 5 + ReadLoop off
     `self.running`.
2. **`classify_infra_error` (fail-safe)** — unit tests FIRST.
   - RED: psycopg split; **redis `DataError`→poison**; bare `ResponseError`/`ReadOnlyError`/`OutOfMemory`/
     `NoPermission`/`Connection`/unknown `RedisError` → **transient** (the fail-safe deny-proofs).
   - GREEN: `redis.DataError` + deterministic psycopg → poison; everything else → transient.
3. **Consecutive-poison counter + bounded retry** (all 5 no-ack arms, incl. `AuditConsumer` non-verdict
   `UniqueViolation`).
   - RED (run-driven): poison → deadletter-after-LIMIT+ack; **transient-then-poison** → poison still gets
     full LIMIT; transient → no deadletter; **starvation** (transient-A + poison-B → B progresses);
     **a bare `redis.ResponseError` entry retries + backs off and is NEVER dead-lettered** (run-driven, not
     just the classify unit test — plan panel P1; the fail-safe proof that prevents an allowlist regression).
   - GREEN: `self._poison` dict; increment-on-poison / pop-on-transient-or-success; route every no-ack arm.
4. **Interruptible backoff + `_ack` set-flag-then-raise** (Redis- AND PG-down).
   - RED: PG-down (conn_factory raises OperationalError) → backoff fires, no deadletter, no hot spin;
     Redis-down (`xreadgroup` raises) → backoff; ack-failure → entry stays pending + backoff.
   - GREEN: `backoff_delay`; `self._stop.wait(...)`; dedicated `infra_this_iteration` (OR-only);
     `_ack` sets flag then raises.
5. **Terminal-sink circuit breaker + canary + sanitize** (deadletter helpers).
   - RED: sanitize (NUL-byte payload deadletters clean); poison deadletter INSERT → canary-fail opens
     circuit (bounded `_poison`, recovers on canary success); canary-pass → row-unstorable ack+alarm
     (terminal, no respin, other entries progress).
   - GREEN: sanitize payloads; canary probe; circuit state; row-specific ack+alarm; all terminals call the
     prep `_publish_result` hook.
6. **Migrations + idempotent deadletter** (`schema.sql`).
   - RED: `ADD COLUMN IF NOT EXISTS stream_entry_id` idempotent on fresh+existing; ack-failure replay →
     exactly one row via `ON CONFLICT (stream_entry_id)`.
   - GREEN: ALTER `audit_deadletter` + `write_deadletter`; every exhaustion INSERT + `deadletter_duplicate_verdict`
     pass `entry_id` + `ON CONFLICT (stream_entry_id)`.
7. **CloseConsumer exhaustion result** — `close_result {outcome:"infra_exhausted", exit_code:3}` via
   `_write_result` (RED: awaited close returns exit 3 on poison-exhaust, not a 90 s hang).

## Deny-proofs (must go RED when the guard is deleted)
poison counter removed → poison test hangs; classify split removed → transient wrongly dead-letters;
`infra_this_iteration` flag removed → poison wrongly backs off / PG-down spins; sanitize removed → NUL
test reds; bare-`ResponseError` classified poison → the fail-safe deny-proof reds.

## Evidence contract (paste — per-step, plan panel P1)
For EACH step: the exact targeted `pytest` command + its **failing (RED)** output before impl and
**passing (GREEN)** after. Each deny-proof shown **RED-when-guard-removed** then restored GREEN. Finish
with the full `pytest tests/arb_memory` (green + counts) and the branch SHA. Config:
`ARB_CONSUMER_POISON_RETRY_LIMIT=5`, `ARB_CONSUMER_BACKOFF_CAP_S=30`.

## Integration note (orchestrator)
Item 1 + Item 2 both touch `WriteLoop`, but at different extension points (Item 1: dispositions calling
the hook; Item 2: the hook body). Merge Item 1 first (it owns the dispositions), then Item 2. Deploy incl.
the 2 migrations is **paused for Mark's review**.
