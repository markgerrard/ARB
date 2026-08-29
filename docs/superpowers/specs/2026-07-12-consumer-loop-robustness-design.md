# Design — Consumer-loop robustness: bounded infra-retry + interruptible backoff

**Status:** DESIGN **v6** (panel₀+r2+r3+r4+r5 folded; scope full-ratified by Mark 2026-07-12; r6 confirm
pending) · **Filed:** `docs/BACKLOG.md § "Consumer-loop robustness…"` (2026-07-12) · **Author:** warm-Opus
orchestrator (inline) · **Scope:** all 5 stream consumers via a shared loop — **Mark ratified the panel-
driven growth (circuit breaker + 2 deadletter migrations + error reclassification) at full scope,
2026-07-12.**

## Round-4 fold log (v4 → v5)

Round-4 confirm: agy `approve/none`, cold-Opus `approve/P2`, codex-sol `nc/P1`. codex-sol (MRO-verified)
+ cold-Opus **independently flagged the same two issues** in v4's own new surface — folded here:

- **[R4-1 — codex-sol P1 + cold-Opus P2] Redis `ResponseError` split was too coarse (§2).** `redis.ResponseError`
  has *transient* subclasses (`ReadOnlyError`, `TryAgainError`, `ClusterDownError`, `MasterDownError`,
  `MovedError`, `AskError` — failover/resharding states). v5 classifies those **transient** (checked
  *before* the plain-`ResponseError`→poison branch); only a *bare* `ResponseError` (WRONGTYPE, syntax) and
  `redis.DataError` are poison. Default bucket for an unknown `RedisError` subclass = **transient**
  (retry-safe). `AuthenticationError`/`AuthorizationError` inherit `ConnectionError` → transient (an
  operator-recovery-friendly retry-until-fixed policy — noted intentional).
- **[R4-2 — codex-sol P1 + cold-Opus P2] Circuit-breaker probe was under-specified (§4).** v4 said "probe
  the sink" without defining the probe or separating a *row-specific* deadletter failure (this one poison
  payload breaks its own INSERT — e.g. an embedded NUL byte) from a *sink-global* one (schema mismatch).
  v5 specifies a **canary probe** and the row-specific/global split so a single un-deadletterable payload
  can't wedge the whole consumer.
- **[R4-3 — cold-Opus P2] Conflict-target + migration placement.** The audit exhaustion INSERT must use
  `ON CONFLICT (stream_entry_id)`, NOT reuse the old `(run_id,seq,content_hash)` target; the `ALTER`s live
  in `schema.sql`, not `run.py setup_schema`.

**Round-4 confirmed sound (kept):** the poison counter, cursor, migrations feasibility (additive,
NULL-distinct), non-verdict `UniqueViolation`→poison, `_ack` set-flag-then-raise, OR-only flag.

## Round-3 fold log (v3 → v4)

Round-3: codex-sol + agy `block/P1`, pi-GLM `nc/P1` ("fundamentally sound"), cold-Opus `approve/P2`. The
panel **verified the v3 core sound** (poison counter, cursor, classify, infra-flag, backoff, exit_code:3
all confirmed) and left three bounded fixes + P2 clarifications:

- **[R3-1 — codex-sol P1] Redis-error classification was too broad.** v3 classed *all* `redis.RedisError`
  as transient, but `redis.ResponseError`/`redis.DataError` (e.g. WRONGTYPE on a run-specific claim key)
  are **deterministic poison** and would retry forever. **v4: split the Redis side like the psycopg side —
  `redis.ConnectionError`/`redis.TimeoutError` → transient; `redis.ResponseError`/`redis.DataError` →
  poison.** (§2)
- **[R3-2 — codex-sol P1] Terminal-sink "retain" reintroduced an unbounded dict.** Under a
  *deterministically-failing* deadletter sink, retaining entries + keeping counters lets `_poison` grow
  with PEL cardinality. **v4: a consumer-wide deadletter-sink circuit breaker** — on sink-poison, clear
  per-entry counters and *suspend poison processing under backoff, probing until the sink recovers*, so
  memory is bounded and the entries are retained-in-PEL (recoverable), not lost. (§4)
- **[R3-3 — codex-sol + agy + pi-GLM P1; cold-Opus P2] Entry_id-idempotent deadletter needs schema.**
  `audit_deadletter` keys on nullable `(run_id, seq, content_hash)` (Postgres allows dup rows when a key
  col is NULL — the malformed-event case); `write_deadletter` has no `stream_entry_id` and only a
  non-unique `ulid` index. `audit_close_/eval_/transcript_deadletter` already have `stream_entry_id`.
  **v4: `ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text` + unique index; same
  for `write_deadletter`; `WriteLoop._deadletter` gains `ON CONFLICT (stream_entry_id) DO NOTHING`; pass
  `entry_id` through.** (§5)
- **[R3-4 — pi-GLM P2] Non-verdict `UniqueViolation` reclassification is a behaviour change.** `audit.py`
  ~406-409 currently `return None`s (infinite retry) on a non-verdict `UniqueViolation`; under §2 an
  `IntegrityError` is **poison** → bounded deadletter (correct — the old infinite-retry *was* Gap 1).
  **v4 §5 calls this out** so the implementer doesn't preserve the `return None`.
- **[R3-5 — agy + pi-GLM P2] Two ordering fixes.** Don't `pop` the poison counter when the *deadletter
  write itself* fails transiently (else the count resets and the entry re-runs `LIMIT` times on heal);
  `_ack` must **set the infra flag *then* raise** so an ack-failure doesn't miss backoff; `infra_this_
  iteration` is **OR-in-True only**, never reset mid-`_tick`. (§1, §7)

**Round-3 confirmed sound (kept):** poison counter boundedness (active-poison only), cursor (read
`> last_pending_id`, wrap at `0` — no skip/revisit), infra-flag placement, exit_code:3, backoff/Event,
conn-factory direction, SQLSTATE-drop.

## Round-2 fold log (v2 → v3)

Round-2 (same roster) blocked v2: agy-print + pi-GLM `P0`, codex-sol `P1`, cold-Opus `P2/approve`. The
panel **verified v2's direction is sound** (shared loop, classify-by-type, infra-signal, fresh WriteLoop
conn all confirmed) but broke the **PEL delivery-count** primitive two independent ways, and found
starvation + terminal-sink gaps. v3 changes:

- **[R2-1 — agy(P0, executed) + pi-GLM(P0) + codex-sol(P1)] The PEL delivery-count cannot be the poison
  budget.** agy *ran* it: `XPENDING <s> <g> - + 1 <entry_id>` treats `<entry_id>` as a **consumer** filter
  → returns empty → count defaults to 1 → poison never exhausts. pi-GLM adds the deeper fact: `XREADGROUP`
  with `"0"` (re-reading own pending) **does not increment** the delivery counter, so even correct syntax
  stays at 1 forever under a `"0"`-recirculation loop. **v3: drop delivery-count/XPENDING entirely →
  in-memory consecutive-*poison* counter** (below), which dissolves this P0 *and* R2-2.
- **[R2-2 — codex-sol P1] Delivery-count counted *all* redeliveries, not poison ones** — 5 transient
  failures would push a later `DataError` past the limit → deadlettered with zero poison-retries.
  **v3: the in-memory counter increments only on *poison*, resets on transient/success** — a poison-only,
  consecutive budget.
- **[R2-3 — codex-sol P1] PEL "0"-read starvation** — always re-reading the *oldest* pending (`count=1`)
  means a persistently-transient oldest entry (retries forever by design) starves every newer pending
  entry. **v3: round-robin pending cursor** (advance by last-seen id, wrap at end) so newer pending
  progress while a stuck-transient entry waits.
- **[R2-4 — codex-sol P1] Terminal-sink failure has no bound** — the exhaustion action is another write to
  the same deadletter table; a deterministic sink failure (schema mismatch) loops forever. **v3: honest
  scoping — if the deadletter write itself fails deterministically, retain + alarm; do NOT claim bounded
  eviction when the sink is broken.**
- **[R2-5 — agy P1 + cold-Opus P2] Backoff signal pollution** — v2 inferred "infra this iteration" from
  the handler's falsy return, but a *poison-retry* also returns falsy → poison would wrongly trip ~31 s of
  backoff, stalling new entries. **v3: a dedicated `infra_this_iteration` flag set only on a *transient*
  catch**, separate from the falsy/truthy retry-vs-ack contract.
- **[R2-6 — pi-GLM P2] `infra_exhausted` result plumbing** — use `exit_code:3` (unused), not `1`
  (conflates `emit_failed`) or `7` (the script's timeout code); the **consumer** writes it via
  `_write_result`→BLPOP, **not** `_report_close_result` (that serves the CLI path).
- **[R2-7 — codex-sol P2 + pi-GLM P2] Deadletter idempotency + full no-ack enumeration** — make every
  consumer's exhaustion-deadletter idempotent by **stream entry_id**; enumerate *all five* no-ack arms
  (generic infra; `AuditConsumer` non-verdict `UniqueViolation` audit.py:406-409; the
  malformed/duplicate-verdict/handler-error deadletter-failed arms).
- **[R2-8 — agy P2 + pi-GLM F7] Wiring** — `MemoryConsumer`/tests instantiate `WriteLoop`/`ReadLoop` with
  an *evaluated* conn; pass the `conn_factory`. Standardize `WriteLoop`'s `not _handle_entry(...)` vs the
  others' `is None`. **Drop the SQLSTATE list** (`40001/40P01/55P03/57014/53300/08006` all inherit
  `OperationalError` — the broad catch covers them; codex-sol + cold-Opus confirmed).

**Round-2 confirmed sound (kept):** the shared recirculating `_tick`, classify-by-type, infra-this-
iteration backoff, fresh-conn WriteLoop deadletter, `_ack` raises, Event migration, exit-code-not-7.

## Panel₀ fold log (v1 → v2)

Panel₀ (codex-sol@high, cold-Opus, pi-GLM, agy-print) **unanimously blocked v1 (P1)**. Two decorrelated
seats independently demolished v1's two central premises; v2 replaces the mechanism. Folded:

- **[F1 — unanimous] v1's Gap-1 mechanism was inert on 4/5 consumers.** Only `CloseConsumer.run()` re-runs
  `drain_pending(limit=1)` each cycle (close.py:107-110); `Audit`/`Eval`/`Transcript`/`WriteLoop.run()`
  drain pending *once* at startup then loop on `step()` with `">"` (new only, audit.py:324 etc.), so an
  unacked PEL entry is never re-read until restart. v1's per-entry counter would reach 1 and never
  exhaust. **v2: a shared recirculating loop** (all consumers drain one pending each cycle) + retry count
  from the **Redis PEL delivery-count**, not an in-memory dict.
- **[F2 — codex-sol + cold-Opus] "deadletter-write is the discriminator" was false.** The failing op and
  the deadletter INSERT hit *different backends/tables/conns* (close_core fails on Redis; deadletter
  writes Postgres), so a transient-but-localized error (deadlock, lock/statement timeout, serialization,
  a Redis blip) exhausts within v1's ~5-15 s window and discards a *valid* entry — a regression vs.
  today's retry-until-heal. **v2: classify by error type**, not by deadletter success (below).
- **[F3 — codex-sol + cold-Opus + pi-GLM] Gap-2 backoff missed PG-down + unbounded budget.** A PG outage
  is caught *inside* `_handle_entry` (returns None), never raises at `run()` level, so v1's backoff never
  fired → hot `psycopg.connect()` storm; and the dict grew one key per new message during the outage.
  **v2: backoff is driven by an "infra error this iteration" signal** surfaced from `_handle_entry`,
  covering both Redis-down and PG-down; the delivery-count approach removes the dict entirely.
- **[F4 — codex-sol + pi-GLM] `WriteLoop` can't deadletter on a poisoned conn.** It holds one persistent
  `conn`, not a `conn_factory` (bus.py:108-118); `_deadletter` reuses it (bus.py:194-206) — and the
  errors that poison it are exactly the retryable ones. **v2: give `WriteLoop` a `conn_factory` for the
  exhaustion deadletter** (mirrors the other four).
- **[F5 — codex-sol] Not every no-ack path was covered.** `AuditConsumer` returns None on a *non-verdict*
  `UniqueViolation` before the generic infra arm (audit.py:406-409); malformed/deadletter arms have their
  own infra returns. **v2: enumerate every no-ack return per consumer** and route each through the shared
  classify/retry path.
- **[F6 — codex-sol + cold-Opus] Ack-ordering.** v1 cleared the retry state before `_ack()`, and most
  consumers swallow ack exceptions (audit.py:378-382 etc.) → a failed XACK leaves the entry pending with
  its count reset. **v2: delivery-count lives in Redis (no clear step); the shared `_ack` must raise on
  failure** so the loop treats a failed XACK as an infra error (→ backoff), not success.
- **[F7 — agy] `WriteLoop` boolean flow.** Its `drain_pending` branches on truthy/falsy return, not
  `is None`. **v2 contract: the shared handler returns falsy on retry, truthy on ack/deadletter.**
- **[F8 — unanimous] Tests must drive `run()`/`xreadgroup`, not `_handle_entry` directly** — else a broken
  loop passes green. **v2: every deny-proof uses the existing template**
  `tests/test_audit_close_consumer.py:155` (`read_modes == ["0", ">", "0"]`), with a fakeredis that
  actually re-delivers unacked PEL entries on `"0"`.

**Confirmed sound by the panel (kept):** the `threading.Event` migration (race-free — `Event.wait()`
returns immediately if already set); `exit_code:1` for CloseConsumer exhaustion (do NOT use 7 — collides
with `arb-audit-close-request`'s timeout code); the "5 clones + ReadLoop shares only Gap 2" scope; mirror
(not extract) is Item 2's call, not this one.

## Problem (unchanged framing)

Five `arb_memory` Redis-stream consumers (`CloseConsumer`, `AuditConsumer`, `EvalConsumer`,
`TranscriptConsumer`, `WriteLoop`; `ReadLoop` shares only Gap 2) each have two robustness gaps:
- **Gap 1** — a *deterministic* `psycopg.Error`/`redis.RedisError` on a specific entry is treated as
  transient (no ack → retry). On CloseConsumer it recirculates forever; on the other four it strands the
  entry in the PEL until restart. Either way it never bounds or dead-letters genuine poison.
- **Gap 2** — no backoff on a persistent backend outage → hot spin (Redis-down at `run()` level; PG-down
  swallowed inside `_handle_entry`).

## Round-5 fold log (v5 → v6) — two root-level simplifications

Round-5 gate: codex-terra (Item 2) + agy + cold-Opus `approve`; codex-sol `nc/P1` — it found the *third*
edge of the same two mechanisms (Redis-poison allowlist; un-persistable payload). v6 stops edge-chasing
and inverts both to **fail-safe**, closing the whole class:

- **[R5-1 — codex-sol P1] Redis poison-by-allowlist is fail-unsafe.** `OutOfMemoryError`/`NoPermissionError`
  also inherit `ResponseError` and were missing from the transient list → an OOM/ACL failure would
  dead-letter valid entries. **v6: invert — only `redis.DataError` is Redis poison; *all* other
  `redis.RedisError` (every `ResponseError` subclass included) → transient.** A missed subclass now fails
  *safe* (retry), and a genuinely-deterministic Redis command bug (e.g. WRONGTYPE) is *global* (hits every
  entry) → surfaces via persistent-transient-retry + backoff + alarm, which is the correct disposition for
  a code bug, not per-entry deadletter. (§2)
- **[R5-2 — codex-sol P1] Row-specific poison still livelocked** (retain → cursor revisits → rebuild
  counter → re-fail → re-alarm forever). **v6: remove the root cause — sanitize deadletter payloads**
  (strip/replace NUL bytes + invalid UTF-8 before the `Jsonb` INSERT) so a deadletter write cannot fail on
  content; the "row-specific" branch then almost never fires. If a sanitized deadletter *still* fails, the
  entry is **acked with a loud `deadletter-unstorable` alarm** — a terminal disposition (the fact-of-
  failure is recorded in the alarm; an alarm is not silent loss), never an infinite respin. (§4)

## Design v6

### 1. Shared recirculating loop with a pending cursor — `src/arb_memory/consumer_loop.py`

Extract the loop the five consumers duplicate into one tested unit (the DRY win *and* the F1 fix). A
`StreamConsumerLoop` mixin provides `run()`:

```
run():
  self._stop.clear()
  drain_pending()                       # startup catch-up (unchanged)
  failures = 0
  while not self._stop.is_set():
      self._infra_this_iteration = False
      self._tick()                      # one new (">") + one pending (cursor); handlers set the flag
      failures = failures + 1 if self._infra_this_iteration else 0
      if self._infra_this_iteration:
          self._stop.wait(backoff_delay(failures))   # interruptible; covers Redis- AND PG-down
```

`_tick()` does `step()` (new, `">"`) then one **cursor-advanced** pending read (R2-3): instead of always
re-reading the oldest with `XREADGROUP …"0"` `count=1`, read `XREADGROUP …"<last_pending_id>"` so the
scan round-robins through the PEL and wraps at the end. A persistently-transient oldest entry no longer
starves newer pending entries. All five consumers now recirculate pending each cycle (F1).

**`infra_this_iteration` is a dedicated flag (R2-5), NOT the handler's return value.** `_handle_entry`
sets `self._infra_this_iteration = True` only when it catches a **transient** error (or `xreadgroup`
raises `RedisError`); a *poison-retry* returns falsy for the ack-flow but does **not** set the flag, so
poison retries never trigger backoff. This decouples "the backend is sick" (→ backoff) from "don't ack
this entry" (→ retry).

### 2. Error classification (replaces the false "discriminator", F2; SQLSTATE list dropped, R2-8)

A shared `classify_infra_error(exc) -> "transient" | "poison"`, generalizing `_is_retryable_write_error`
(bus.py:233):

- **transient** — `psycopg.OperationalError`, `psycopg.InterfaceError`, and **every `redis.RedisError`
  except `redis.DataError`** (R5-1 — fail-safe inversion). The psycopg side covers the contention SQLSTATEs
  (`40001`/`40P01`/`55P03`/`57014`/`53300`/`08006` — all inherit `OperationalError` in psycopg3), so **no
  explicit SQLSTATE list**. The Redis side is now **denylist-poison, not allowlist-transient** — there is
  no subclass list to be incomplete, so a `ReadOnlyError`/`TryAgainError`/`ClusterDownError`/
  `OutOfMemoryError`/`NoPermissionError`/failover/ACL error can never be mis-binned as poison. Retry
  forever with backoff.
- **poison** — any other `psycopg.Error` (`DataError`, non-handled `IntegrityError`, `ProgrammingError`)
  **and only `redis.DataError`** on the Redis side (a genuine client-side data-encoding error, entry-
  specific). Bounded-retry → deadletter.

Fail-safe by construction: a *deterministic Redis command bug* (e.g. WRONGTYPE on a claim key) is
**global** — it hits every entry, so it surfaces as persistent-transient → backoff + alarm (the correct
disposition for a code bug), never as a per-entry deadletter of a *valid* entry. Classifies by *what the
error is*; a miss fails safe (retry), never unsafe (discard a valid entry). Dodges F2 (never keys off a
cross-backend write's success).

### 3. Bounded retry via an in-memory consecutive-poison counter (replaces delivery-count, R2-1/R2-2)

The PEL delivery-count cannot serve as the budget (R2-1: wrong `XPENDING` semantics; `"0"` re-reads don't
increment it. R2-2: it counts transient redeliveries too). Instead: a small in-memory
`self._poison: dict[entry_id, int]` on the consumer:

- On a handle raising a **poison** error: `n = self._poison[entry_id] = self._poison.get(entry_id,0)+1`.
  If `n >= POISON_RETRY_LIMIT` → deadletter-with-reason (`infra-poison-exhausted (<n>): <err>`), ack,
  `del self._poison[entry_id]`. Else return falsy (retry).
- On a handle that **succeeds** or raises **transient**: `self._poison.pop(entry_id, None)` — the counter
  is **consecutive-poison only**, so transient redeliveries never push a later poison over the limit
  (R2-2), and a self-healing entry resets.
- **Bounded:** only *active-poison* entries hold a key, and they are evicted within `LIMIT` cycles;
  transient/healthy entries never accumulate. Restart resets the dict — acceptable (a poison entry stays
  in the PEL and is re-counted; bounded blast, panel-accepted).

### 4. Terminal-sink circuit breaker with a canary probe (R2-4 + R3-2 + R4-2)

Exhaustion writes to the same deadletter table that may itself be failing. On a poison error from the
exhaustion-deadletter INSERT, **discriminate row-specific from sink-global** with a **canary probe** — a
trivial, payload-independent write to the same table (e.g. `INSERT … (stream_entry_id='__canary__', …)
ON CONFLICT DO NOTHING`, immediately rolled back / or a dedicated sentinel row):

- **Canary succeeds ⇒ row-specific poison** (this one entry's payload breaks its own INSERT — e.g. an
  embedded NUL byte). Do NOT open the global circuit. **First, this is largely prevented at source
  (R5-2):** every deadletter helper **sanitizes its payload** (strip/replace NUL bytes + invalid UTF-8
  before the `Jsonb`), so a content-driven INSERT failure is rare by construction. If a *sanitized*
  deadletter still fails for this one entry, **ack it with a loud `deadletter-unstorable` alarm** — a
  terminal disposition (the fact-of-failure lives in the alarm; not silent loss), **never a retain-and-
  respin** (which livelocked in v5: the cursor would revisit, rebuild the counter, re-fail, re-alarm
  forever). Other entries continue normally.
- **Canary fails ⇒ sink-global poison** (schema mismatch, table gone). **Open the circuit:** clear *all*
  per-entry poison counters (dict can't grow), alarm (`deadletter-sink-poison`), set
  `infra_this_iteration` (→ backoff), and **suspend poison deadlettering**. While open, re-run the canary
  each cycle under backoff; **close and resume** on the first canary success. Entries stay retained-in-PEL
  (recoverable), memory bounded (no residue while open), no hot loop (backoff-capped).

If the exhaustion-deadletter INSERT fails **transiently**, do not probe and do not pop the counter (R3-5)
— retry with the count intact. Bounded eviction is promised only when the sink is writable; a broken sink
(global) degrades to visible alarm + backoff, never silent loss and never unbounded memory. Row-specific
poison is **sanitized, then — if a sanitized write still fails — alarmed-and-acked (terminal, R5-2)**, never
retained-and-respun (which livelocked in v5).

### 5. No-ack enumeration + idempotent deadletter + schema migrations (F5, R2-7, R3-3, R3-4)

Enumerate **all five** no-ack `return None` arms per consumer — the generic infra arm; `AuditConsumer`'s
non-verdict `UniqueViolation` (audit.py:406-409); the malformed-, duplicate-verdict-, and
handler-error-deadletter-failed arms — and funnel each through `classify_infra_error` + the poison
counter. **R3-4:** the non-verdict `UniqueViolation` arm currently `return None`s (infinite retry); an
`IntegrityError` classifies **poison** → bounded deadletter — this is the intended behaviour change (the
old infinite-retry was an instance of Gap 1); the implementer must NOT preserve the `return None`.

**Idempotent-by-`entry_id` deadletter needs schema (R3-3).** `audit_close_/eval_/transcript_deadletter`
already carry `stream_entry_id`. Two don't:
- `ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text` + a unique index on it
  (its current `(run_id, seq, content_hash)` uniqueness is void for malformed rows where those are NULL).
- `ALTER TABLE write_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text` + unique index (it has no
  entry-id column and only a non-unique `ulid` index).
- Every consumer's exhaustion-deadletter INSERT uses `ON CONFLICT (stream_entry_id) DO NOTHING` and passes
  the stream `entry_id`, so a "deadletter committed, XACK failed" replay inserts exactly one row.
Migrations are additive `ADD COLUMN IF NOT EXISTS` (matching the live `audit_events` pattern) — see
Rollout for the deploy gate.

### 6. `WriteLoop` conn-factory (F4) + return contract (F7, R2-8)

`WriteLoop` gains a `conn_factory` (like the other four) and uses a **fresh** conn for the exhaustion
deadletter, so a poisoned primary conn can't block eviction. `MemoryConsumer` and the tests must pass the
`conn_factory` **callable**, not an evaluated `conn_factory()` (bus.py:347-348; test_bus_pel.py). The
shared handler returns falsy on retry / truthy on ack-or-deadletter; standardize `WriteLoop`'s
`not _handle_entry(...)` and the others' `is None` onto one predicate.

### 7. Interruptible backoff via `threading.Event` (kept, panel-approved) + flag ordering (R3-5)

Migrate all five (+ReadLoop) from `self.running` bool to `self._stop = threading.Event()`; `stop()` →
`set()`; backoff = `self._stop.wait(backoff_delay(failures))`. `backoff_delay(n, base=0.5, cap=30) =
min(base*2**n, cap)`, pure/deterministic. `infra_this_iteration` is reset to `False` once at the top of
each `_tick`, and handlers only **OR-in `True`** (never reset it mid-tick, so a transient `step()` isn't
masked by a later clean pending read — R3-5). On XACK failure `_ack` **sets the flag *then* raises** (F6,
R3-5) so an ack-failure both retries the entry (idempotent re-handle) and engages backoff.

### 8. CloseConsumer exhaustion result (corrected, R2-6)

On poison-exhaustion, the **consumer** writes `close_result {outcome:"infra_exhausted", exit_code:3}` via
`_write_result`→BLPOP (so the blocking caller returns immediately instead of waiting 90 s). `3` is unused
(**not `1`** — conflates `emit_failed`; **not `7`** — the script's timeout code). This is a
consumer-side result-shape change; `_report_close_result` (the CLI path) is untouched.

### Tunables
`ARB_CONSUMER_POISON_RETRY_LIMIT` (default `5`), `ARB_CONSUMER_BACKOFF_CAP_S` (default `30`), base `0.5`.

## Testing (TDD; every consumer-loop test drives `run()`, F8)

- **Unit** — `classify_infra_error` (type → transient/poison, incl. the SQLSTATE-carrying
  `OperationalError` subclasses); `backoff_delay` monotonic-then-capped; the consecutive-poison counter
  (poison→poison exhausts; transient/success resets).
- **Per-consumer integration via `run()`** (template: `tests/test_audit_close_consumer.py:155`, asserts
  `read_modes` and calls `consumer.run()`; fakeredis re-delivers unacked PEL and models real delivery
  semantics — the counter is in-memory, so no `XPENDING` fidelity is required):
  - **poison** (`DataError`) → re-delivered `LIMIT` times then dead-lettered + acked, PEL drains
    (+ `close_result infra_exhausted exit_code=3` for Close).
  - **transient then poison** (R2-2 deny-proof) → `LIMIT`× OperationalError then a `DataError` → the
    `DataError` still gets its **full** `LIMIT` poison-retries (proves the counter is poison-only, not
    total redeliveries).
  - **transient** (OperationalError k×, then success) → no deadletter, retries, acks.
  - **starvation** (R2-3 deny-proof) → transient pending A + poison pending B → B progresses to deadletter
    while A stays pending (proves the cursor, not always-oldest).
  - **PG-down** (conn_factory raises OperationalError) → backoff fires (no hot spin), no deadletter.
  - **Redis-down** (`xreadgroup` raises) → backoff at `run()` level.
  - **deadletter-sink circuit** (R3-2) → deadletter INSERT raises a *poison* error → circuit opens:
    counters cleared, alarm logged, entries retained-in-PEL, `_poison` stays bounded across many failing
    entries; a later successful probe closes the circuit and resumes.
  - **redis classification** (R5-1, fail-safe) → `redis.DataError` → poison → bounded-retry → deadletter;
    **`redis.ResponseError` (incl. WRONGTYPE), `ReadOnlyError`, `OutOfMemoryError`, `NoPermissionError`,
    `ConnectionError` → transient → retry, NO exhaustion** (a miss must fail safe, never deadletter a valid
    entry).
  - **deadletter idempotency (R3-3)** → "deadletter committed, XACK failed" replay → asserts exactly one
    row via `ON CONFLICT (stream_entry_id)` on `audit_deadletter` + `write_deadletter` (post-migration).
- **Deny-proofs** — delete the poison counter → poison test hangs; delete the classify split → transient
  test wrongly dead-letters; delete the dedicated `infra_this_iteration` flag → poison test wrongly backs
  off (R2-5) / PG-down test spins.
- **Ack-failure idempotency** — deadletter commits but `XACK` raises → entry stays pending AND exactly one
  durable deadletter row after recovery (F6, R2-7).

## Rollout / integration

- All consumers ship in one image, redeploy together. **Deploy paused for user review.** The two additive
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS stream_entry_id` migrations (R3-3) run against prod `arbmemory`
  as part of the deploy — additive/backfill-free (matching the live `audit_events` column-add pattern),
  but they touch a prod schema, so they are **explicitly part of the paused deploy-review gate**, not run
  ahead of it.
- **Shared foundation with Item 2 (F-joint):** both items edit `bus.py`'s `WriteLoop`/`handle_write_intent`.
  v2 recommends landing a **small shared prep slice first** — extract `consumer_loop.py` + pin
  `handle_write_intent`'s new structured-receipt return contract (Item 2 needs the receipt; Item 1 needs
  the conn-factory + classify) — then Item 1 and Item 2 build on it in parallel with minimal overlap. The
  orchestrator integrates; see Item 2 design §"joint contract".
- Assertions read Redis stream / PEL state, never a Postgres poll (async lag) — `audit-close-1-design.md §8`.
