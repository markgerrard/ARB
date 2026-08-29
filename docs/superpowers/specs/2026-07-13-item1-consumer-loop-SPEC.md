# SPEC — Item 1: consumer-loop robustness (builds ON the shared prep slice)

**Status:** SPEC **v2** (spec panel₀ folded; confirm pending) · **Design:**
`docs/superpowers/specs/2026-07-12-consumer-loop-robustness-design.md` **v6** (mechanism/why authoritative
there) · **Prereq:** `2026-07-13-shared-prep-slice-SPEC.md` merged · **Author:** warm-Opus (inline).

**Spec panel₀ folded:** ReadLoop gets Event+backoff only (not recirculation — sol P1); terminal
dispositions call prep's `_publish_result` hook so Item 2's `{failed}` covers Item 1's new terminals
(cold-Opus/pi-GLM P1); `deadletter_duplicate_verdict` added to the migration sweep (agy P2); Redis
fail-safe test coverage widened to prevent an allowlist regression (sol P1); truthy-terminal loop test
added (terra P2).

Adds the robustness logic onto the extracted `consumer_loop.py`. Scope = all 5 consumers, ratified full by
Mark 2026-07-12.

## Deliverables

### 0. `src/arb_memory/consumer_loop.py` extraction + Event migration + pending cursor (lands WITH the retry logic below)
The shared `StreamConsumerLoop` mixin all 5 consumers (+`ReadLoop` for the backoff hook) use — extracted
here (not in prep) because unifying the loop only makes sense *with* the bounded retry:
```python
class StreamConsumerLoop:
    # subclass provides: self._stop (threading.Event), redis, stream, group, consumer, block_ms,
    #                    _handle_entry(entry_id, fields) -> truthy(ack'd) / falsy(retry)
    def run(self): ...     # drain_pending() once; loop: reset infra flag; _tick(); backoff if infra
    def _tick(self): ...   # step(">") then one CURSOR-advanced pending read; handlers OR-in infra flag
    def step(self): ...;  def drain_pending(self, *, limit=None): ...;  def stop(self): ...
```
- **Event migration:** all 5 write-like consumers **+ `ReadLoop`**: `self.running: bool` →
  `self._stop = threading.Event()`; `stop()` → `set()`; loop `while not self._stop.is_set()`.
- **Pending cursor + recirculation applies to the 5 write-like consumers ONLY.** The per-cycle pending read
  advances by `last_pending_id` (`XREADGROUP … "<id>"`), wraps to `"0"` on empty — so all 5 recirculate
  pending each cycle (fixes the 4 `">"`-only consumers) AND a stuck-transient oldest entry can't starve
  newer pending. (Design §1.)
- **`ReadLoop` gets ONLY the Event + interruptible backoff** (spec panel: sol P1) — **NOT** the cursor/
  recirculation. ReadLoop's pending is *cleanup-only* (acks stale reads without replying, bus.py:257-342);
  its `_handle_entry` terminally acks malformed and returns falsy, which is incompatible with the mixin's
  falsy=retry contract. Keep ReadLoop's `drain_pending` cleanup-only; preserve the no-stale-reply
  behaviour + its `test_bus_pel.py:107-122` test (extended to drive `run()`).
- **Terminal dispositions call the prep `_publish_result` hook** (prep §3): every terminal arm below
  (success, dedup, poison-exhaustion deadletter, **row-unstorable ack**, malformed deadletter) invokes the
  hook so Item 2's `{failed}`/receipt publish covers ALL of them — not just the classic deadletter path
  (spec panel: cold-Opus P1-2, pi-GLM P1-2).

### 1. `classify_infra_error(exc) -> "transient" | "poison"` (`consumer_loop.py`)
Fail-safe (design §2):
- **poison** ⇐ `psycopg.DataError`, non-handled `psycopg.IntegrityError`, `psycopg.ProgrammingError`,
  and **only** `redis.DataError`.
- **transient** ⇐ everything else that's `psycopg.OperationalError`/`InterfaceError` or `redis.RedisError`
  (every `ResponseError` subclass incl. `ReadOnlyError`/`OutOfMemoryError`/`NoPermissionError` → transient).
- Default for an unrecognized error type routed here: raise (not our infra error → let it propagate).

### 2. Consecutive-poison counter + bounded retry (`consumer_loop.py` + each `_handle_entry`)
Per-consumer `self._poison: dict[entry_id, int]`. On a handled entry:
- transient error ⇒ `self._poison.pop(entry_id, None)`; set `infra_this_iteration=True`; return falsy (retry).
- poison error ⇒ `n = self._poison[entry_id] = get(entry_id,0)+1`; if `n >= POISON_RETRY_LIMIT` →
  deadletter (below) then ack + `del`; else return falsy.
- success ⇒ `pop`; ack.
- Enumerate **all five** no-ack arms per consumer (generic infra; `AuditConsumer` non-verdict
  `UniqueViolation` audit.py:406-409 — now IntegrityError→poison; the malformed/dup-verdict/handler-error
  deadletter-failed arms) and route each through classify. (Design §5.)

### 3. Interruptible backoff (`consumer_loop.py`)
`backoff_delay(n, base=0.5, cap=ARB_CONSUMER_BACKOFF_CAP_S) = min(base*2**n, cap)`; wait via
`self._stop.wait(backoff_delay(failures))`; `failures` resets on a clean iteration. `_ack` **sets
`infra_this_iteration` then raises** on XACK failure. (Design §7.)

### 4. Terminal-sink circuit breaker + canary + sanitize (`consumer_loop.py`, per-consumer deadletter helpers)
- Deadletter helpers **sanitize** payloads (strip/replace NUL + invalid UTF-8 before `Jsonb`).
- On a poison error from the exhaustion-deadletter INSERT: run a **canary** (payload-independent write to
  the same table, rolled back / sentinel). Canary-fails ⇒ open circuit (clear all counters, alarm
  `deadletter-sink-poison`, suspend poison-deadlettering, probe each cycle, close on canary success).
  Canary-succeeds ⇒ row-specific: **ack + `deadletter-unstorable` alarm** (terminal, never respin).
- Transient deadletter-INSERT failure ⇒ don't pop counter, retry. (Design §4.)

### 5. Deadletter idempotency + migrations (`schema.sql`)
- `ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text` + unique index.
- `ALTER TABLE write_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text` + unique index.
- Every exhaustion-deadletter INSERT uses `ON CONFLICT (stream_entry_id) DO NOTHING` and passes the stream
  `entry_id`. (`audit_close_/eval_/transcript_deadletter` already have the column.) ALTERs live in
  `schema.sql`, NOT `run.py setup_schema`.
- **Also sweep `deadletter_duplicate_verdict` (audit.py:202)** — it writes to `audit_deadletter` on a
  duplicate verdict and currently takes no `entry_id`; change to `deadletter_duplicate_verdict(conn,
  entry_id, event)` + insert `stream_entry_id` + `ON CONFLICT (stream_entry_id) DO NOTHING`, else it
  violates the new unique index (spec panel: agy P2).

### 6. CloseConsumer exhaustion result
On poison-exhaustion the consumer writes `close_result {outcome:"infra_exhausted", exit_code:3}` via
`_write_result` (BLPOP path), NOT `_report_close_result`. `3` is unused. (Design §8.)

### Config
`ARB_CONSUMER_POISON_RETRY_LIMIT=5`, `ARB_CONSUMER_BACKOFF_CAP_S=30`.

## Acceptance criteria
- All 5 consumers bound poison, back off on Redis- AND PG-down (no hot spin), and never dead-letter a
  valid entry on a transient/failover error.
- Migrations additive + idempotent; deadletter idempotent by `stream_entry_id`.
- `uv run --extra arb-memory pytest tests/arb_memory` green.
- **Deny-proofs present and RED when the guard is removed** (below).

## Tests (drive `run()`, per design §Testing — template `test_audit_close_consumer.py:155`)
Per consumer where applicable: poison→deadletter-after-LIMIT+ack; **transient-then-poison** (poison still
gets full LIMIT — proves consecutive-poison); transient→no-deadletter; **starvation** (transient-A +
poison-B → B progresses); PG-down→backoff-no-deadletter; Redis-down→backoff; **sink circuit** (poison
deadletter INSERT → canary-fail opens/recovers; canary-pass row-specific ack+alarm); **sanitize** (NUL-byte
payload → deadletters clean); **ack-failure idempotency** (deadletter commits, XACK raises → exactly one
row after recovery). Unit: `classify_infra_error` (psycopg split; redis **`DataError`→poison** and — the
fail-safe deny-proofs that prevent an allowlist regressing, spec panel sol P1 — **bare `redis.ResponseError`
(incl. WRONGTYPE), `ReadOnlyError`, `OutOfMemoryError`, `NoPermissionError`, `ConnectionError`, and an
otherwise-unrecognized `RedisError` subclass all → transient**); a **run-driven** deny-proof asserting a
bare-`ResponseError` entry retries + backs off and is NEVER dead-lettered. `backoff_delay`.
- **Truthy-terminal loop test** (spec panel terra P2): a `run()`-driven pending-drain proving an
  acked-malformed *terminal* entry (returns truthy) does not halt subsequent pending work.

**Deny-proofs:** remove poison counter → poison test hangs; remove classify split → transient wrongly
dead-letters; remove dedicated infra flag → poison wrongly backs off; remove sanitize → NUL test reds.

## Deploy
All consumers one image, redeploy together **incl. the 2 migrations against prod `arbmemory`** — **paused
for Mark's deploy-review gate** (additive, but touches prod schema). No `docker-compose.yml` change expected.
