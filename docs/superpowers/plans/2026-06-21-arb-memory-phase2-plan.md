# ARB Memory Phase 2 — Audit Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans.
> TDD; `- [ ]` checkboxes.

**Goal:** A separate-consumer-group audit pipeline — emit helpers + a fire-and-forget audit consumer draining
`arbmem:audit` into `audit_events`, with the spec-panel's evidence-safety fixes (Valkey-INCR seq,
fail-loud-on-collision, payload cap, lag alarm, sink seam).

**Architecture:** `docs/superpowers/specs/2026-06-21-arb-memory-phase2-audit-design.md` (spec-panel folded).
Reuses Phase 0 (`audit_events` table) + Phase 1 (`bus.py` loop/resilience patterns).

**Tech Stack:** Python 3.11+, `redis`, `psycopg`/`pgvector` (the `arb-memory` extra). One consumer thread
(no read lane — fire-and-forget).

> **Plan-panel folded (2026-06-21, PLAN-HOLES — cold-Opus+agy certifiers + codex):** (P0) the central
> collision deny-proof was hollow (asserted only the `"dead-lettered"` string → an impl that drops the
> colliding event passes green = the silent-loss the spec prevents). Fixed (Task 3): add an **additive
> `audit_deadletter` table** (schema.sql), `handle_audit_event` WRITES the colliding event there, and the
> test **queries it back out and asserts it survived (recoverable)**. (P1) `_event` test helper defined
> (Task 1); the lag test uses `XINFO GROUPS` **lag/pending correctly** (it would have crashed); the ts-order
> test injects **anti-correlated ts** and the consumer **persists the emitter's ts** (not `DEFAULT now()`),
> else ORDER BY seq vs ts can't be distinguished; the concurrency test is a sanity check (INCR is
> server-atomic) not a deny-proof. (§6a) the dead-letter destination is the **configured sink**, never
> event-directed.

## Global Constraints
- New module `src/arb_memory/audit.py`. Imported only by the (future) audit service.
- Reuse Phase 1's patterns from `bus.py`: `ensure_group`, the loop/`drain_pending`, XACK-after-commit, and
  the **poison-resilience classification** (parse/content errors → ack-and-drop; infra errors → retry).
- Test isolation: same dedicated redis db (`ARB_MEMORY_REDIS_URL`, db 15) + unique prefix per test; Phase 0
  `scratch` pg fixture; `importorskip`.
- The evidence-safety fixes are load-bearing — copy the spec verbatim: Valkey-INCR seq, content_hash form,
  fail-loud-on-mismatch, `AUDIT_MAX_PAYLOAD_BYTES` cap.
- TDD; commit per task.

## File Structure
- `src/arb_memory/audit.py` — `audit_emit`, `AuditRun`, `next_seq`, `audit_content_hash`, `AuditSink`/
  `PostgresAuditSink`, `handle_audit_event`, `AuditConsumer`, `audit_lag`.
- `tests/arb_memory/test_audit.py`.

---

### Task 1: seq allocator (Valkey-INCR) + content_hash + payload cap

**Files:** Create `src/arb_memory/audit.py`, `tests/arb_memory/test_audit.py`.

**Interfaces — Produces:** `next_seq(redis, run_id, *, prefix=PREFIX) -> int` (`INCR
{prefix}arbmem:audit:run:<run_id>:seq`, set a TTL on first use); `audit_content_hash(run_id, seq, source,
kind, payload) -> str` (`sha256(run_id\0seq\0source\0kind\0` + `json.dumps(payload, sort_keys=True,
separators=(",",":"))`)`; `AUDIT_MAX_PAYLOAD_BYTES = 16384`.

- [ ] **Step 1: failing tests:**
```python
def test_seq_allocator_is_unique_under_concurrency(redis_bus):
    import threading
    seqs, lock = [], threading.Lock()
    def grab():
        s = next_seq(redis_bus, "run1", prefix=redis_bus.prefix)
        with lock: seqs.append(s)
    ts = [threading.Thread(target=grab) for _ in range(50)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(seqs)) == 50                      # all distinct (atomic INCR)

def test_content_hash_stable_and_field_sensitive():
    h = audit_content_hash("r", 1, "orch", "vote", {"a": 1})
    assert h == audit_content_hash("r", 1, "orch", "vote", {"a": 1})
    assert h != audit_content_hash("r", 1, "orch", "vote", {"a": 2})
```
- [ ] **Steps 2-4:** implement. - [ ] **Step 5: commit** — `feat(arb-memory): audit seq allocator + content_hash [P2]`

---

### Task 2: `audit_emit` + `AuditRun` (payload cap enforced)

**Interfaces — Produces:** `audit_emit(redis, *, run_id, seq, source, kind, payload, prefix=PREFIX)` (XADD
`arbmem:audit` MAXLEN ~1_000_000, raises if `len(canonical-json(payload)) > AUDIT_MAX_PAYLOAD_BYTES`);
`AuditRun(redis, run_id)` with `.emit(source, kind, payload)` (next_seq → audit_emit).

- [ ] **Step 1: failing tests:** `test_oversized_payload_rejected` (payload > cap → `audit_emit` raises —
  deny-proof: an uncapped emit accepts it); `test_audit_run_increments_seq` (two `.emit`s → seq 1,2).
- [ ] **Steps 2-4:** implement. - [ ] **Step 5: commit** — `feat(arb-memory): audit_emit + AuditRun + payload cap [P2]`

---

### Task 3: sink seam + `handle_audit_event` (fail-loud on collision)

**Interfaces — Produces:** `AuditSink` (`write(conn, event)`, `required=["postgres"]`); `PostgresAuditSink`;
`handle_audit_event(conn, event, *, sinks) -> str` (`"written"|"duplicate"|"dead-lettered"`).

- [ ] **Step 1: failing tests:**
```python
def test_audit_idempotent_on_redelivery(scratch):
    ev = _event(run_id="r", seq=1, kind="vote", payload={"x": 1})
    assert handle_audit_event(scratch, ev, sinks=[PostgresAuditSink()]) == "written"
    assert handle_audit_event(scratch, ev, sinks=[PostgresAuditSink()]) == "duplicate"  # same hash
    assert scratch.execute("SELECT count(*) FROM audit_events WHERE run_id='r'").fetchone()[0] == 1

def test_seq_collision_fails_loud(scratch):
    handle_audit_event(scratch, _event("r", 1, "vote", {"x": 1}), sinks=[PostgresAuditSink()])
    # a DIFFERENT event forced onto the same (run_id, seq) → dead-lettered, NOT silently dropped
    res = handle_audit_event(scratch, _event("r", 1, "vote", {"x": 999}), sinks=[PostgresAuditSink()])
    assert res == "dead-lettered"
    # original intact
    assert scratch.execute("SELECT payload FROM audit_events WHERE run_id='r' AND seq=1").fetchone()[0] == {"x": 1}
    # CENTRAL (plan-panel P0): the colliding event MUST be RECOVERABLE — query it back out of audit_deadletter
    dl = scratch.execute("SELECT payload, content_hash FROM audit_deadletter "
                         "WHERE run_id='r' AND seq=1").fetchone()
    assert dl is not None and dl[0] == {"x": 999}     # the lost event survived, recoverable — NOT silently gone
```
`_event(run_id, seq, kind, payload, source="orchestrator")` is a test helper (in test_audit.py) building the
flat envelope with a computed content_hash + a stream_entry_id.

- [ ] **Step 0: add the additive `audit_deadletter` table to `schema.sql`** (plan-panel P0 — pin the
  destination, don't leave "table OR stream"): `audit_deadletter(id bigserial PK, run_id text, seq bigint,
  source text, kind text, payload jsonb, content_hash text, conflicting_hash text, ts timestamptz DEFAULT
  now())`. Additive only — does NOT touch `artefacts`/`hints`/`audit_events`. Idempotent `CREATE … IF NOT
  EXISTS`.
- [ ] **Steps 2-4:** implement: compute content_hash; `INSERT … ON CONFLICT (run_id, seq) DO NOTHING
  RETURNING id`; on no-row-returned, `SELECT content_hash WHERE (run_id, seq)` — if equal → `"duplicate"`;
  if different → **INSERT the colliding event INTO `audit_deadletter`** (the configured sink, never
  event-directed) + log an error → return `"dead-lettered"` (NEVER silently discard; the event is recoverable
  from `audit_deadletter`). Record-shaped columns populated. Sink list length-one, `required=["postgres"]`.
  Deny-proof: a plain `DO NOTHING` impl (or one that returns "dead-lettered" without writing the deadletter)
  → the `audit_deadletter` query is empty → `test_seq_collision_fails_loud` red.
- [ ] **Step 5: commit** — `feat(arb-memory): audit sink seam + fail-loud collision [P2]`

---

### Task 4: `AuditConsumer` (loop, XACK-after-commit, poison-resilience) + roundtrip + at-least-once

**Interfaces — Produces:** `AuditConsumer(redis, conn_factory, *, prefix=PREFIX)` — one loop reusing the
Phase 1 pattern (ensure_group `arbmem-audit`, drain_pending, live `>`, XACK after commit, poison
classification). `start()`/`stop()`.

- [ ] **Step 1: failing tests:** `test_audit_event_roundtrip` (emit → drain → row with all columns);
  `test_audit_xack_after_commit` (DB failure → not acked → redelivered → succeeds; deny-proof: ack-before-
  commit loses it); `test_audit_poison_dropped` (malformed event → ack-and-drop, consumer survives; deny-
  proof: no-catch → thread dies); `test_seq_orders_within_a_run` (plan-panel P1: the consumer **persists the
  EMITTER's `ts`** from the event, not `DEFAULT now()`; the test emits seq 1,2,3 with **anti-correlated** ts
  (decreasing) → `ORDER BY seq` reconstructs orchestrator order. Deny-proof: an `ORDER BY ts` impl reverses
  → red — only distinguishable because the emitter ts is persisted).
- [ ] **Steps 2-4:** implement (mirror `bus.py` WriteLoop, single lane); **persist `event["ts"]` into
  `audit_events.ts`** (the column must take the emitter's ts, not the DB default). - [ ] **Step 5: commit** —
  `feat(arb-memory): AuditConsumer loop + at-least-once [P2]`

---

### Task 5: lag alarm + run-join + wrap-up

**Interfaces — Produces:** `audit_lag(redis, *, prefix=PREFIX) -> dict` (stream length + group pending/lag
via `XINFO`); a `check_audit_health()` that returns an alarm flag when lag exceeds a threshold.

- [ ] **Step 1: tests:** `test_lag_alarm_fires_when_behind` (plan-panel P1: `audit_lag` reads the group's
  pending via `XINFO GROUPS <stream>` → the `lag`/`pending` field for group `arbmem-audit` — NOT stream
  length alone; use the correct XINFO field so it doesn't crash on a fresh group. `ensure_group` first, then
  XADD events that the group hasn't read → `audit_lag` reports pending ≥ threshold → alarm true. Deny-proof:
  a stream-length-only check is satisfied by a drained stream and misses a wedged-but-behind consumer →
  red); `test_run_join_superset_of_disagreement` (emit orchestrator dispatch + 3 seat positions + verdict for
  one run_id → query by run_id reconstructs the full panel).
- [ ] **Step 2:** run the full Phase 2 suite green (local redis db 15 + pgvector); `pytest tests/
  --collect-only -q` intact; `git diff --name-only` only `src/arb_memory/**` + `tests/arb_memory/**` (+
  `schema.sql` IF a `audit_deadletter` table is added — confirm it's additive, not touching existing tables).
- [ ] **Step 3: commit** — `feat(arb-memory): audit lag alarm + run-join + Phase 2 green [P2]`

---

## Self-Review
- **Spec coverage:** §4 seq → T1; §3 payload cap → T2; §5 sink+collision → T3; §5 consumer/at-least-once →
  T4; §2 lag alarm + §3 run-join → T5. ✓
- **Deny-proofs real:** T1 (in-mem counter → dup seq red), T3 (DO NOTHING → collision vanishes red), T4
  (ack-before-commit → lost red; no-catch → crash red; ts-order → skew mis-order red), T5 (length-only →
  misses wedge red). ✓
- **Evidence-safety verbatim from spec:** Valkey-INCR, fail-loud collision, payload cap, lag alarm. ✓
- **No bridge-core contamination:** new module; the T5 diff check (audit_deadletter additive). ✓
