# ARB Memory — Phase 2: audit consumer [spec]

**Status:** SPEC (spec-panel folded; ready for the plan). Phase 2 of ARB Memory (Workflow B). Builds on
Phase 0 (the `audit_events` table stub + write-library) and Phase 1 (the bus consumer/loop/resilience
patterns). **Architecture of record:** `docs/decisions/arb-memory-architecture.md` §8.

**Spec-panel verdict (2026-06-21):** cold-Opus + agy + codex (M3 garbled) — **SPEC-HOLES**, converged. For an
EVIDENCE store every finding was a silent-loss path. Folded: **(P0)** `seq` is allocated by a **Valkey-INCR
per-run counter** (§4) — atomic, monotonic, collision-free for any emitter; seats never mint locally → the
`(run_id, seq)` key is unique-by-construction; **(P0)** `ON CONFLICT (run_id, seq)` no longer silently
`DO NOTHING` — it compares `content_hash`: equal → true-redelivery no-op, **different → fail LOUD /
dead-letter** (a real collision is a bug, not evidence to drop) (§5); **(P0)** `MAXLEN` is a high OOM ceiling
plus an **un-drained-overflow alarm** (§2) so a falling-behind consumer is visible, not silent loss; **(P0)**
decision-grain is a **structural payload-size cap** (§3), not prose, so the table can't drift into a
trajectory store; `content_hash` canonical form pinned (§5).

## 0. Scope — IS / IS NOT
**IS:** a **separate consumer group** on the bus that drains an audit stream (fire-and-forget append) into
`audit_events`, plus the emit helpers (orchestrator + seat) that produce the joined event stream. Proven
against local Valkey (db 15 in tests) + the Phase 0 pgvector DB.
**IS NOT:** no object-store / training-export sinks (deferred §9; only the sink SEAM is built), no
containers/MCP/CF (Phase 3), no orchestrator skill-wiring beyond the emit helper.

## 1. Why a separate consumer group (§8)
Audit's contract is the OPPOSITE of memory's: memory is lossy/lean/latency-sensitive; audit is
completeness-oriented, fire-and-forget, latency-indifferent. A crash that's a shrug for memory can be an
incident for audit. **Separate consumer group (own cursor, own process)** so each outage carries its correct
severity, and audit is the EASY direction on the bus — pure append, no request/reply, no correlation-id-on-
read, no timeout machinery (the hard parts Phase 1 already solved are simply absent here).

## 2. Stream + table
- **Audit stream:** `agent_scratch:arbmem:audit` — emitters `XADD` events; the audit consumer (group
  `arbmem-audit`, distinct from `arbmem-memory`) drains.
- **`audit_events` table** (Phase 0 stub, now used): `id, run_id, seq, source, ts, payload jsonb,
  stream_entry_id, content_hash, raw_entry jsonb, UNIQUE(run_id, seq)`. The `(run_id, seq)` unique is the
  idempotency + ordering key.
- **`MAXLEN` = a high OOM ceiling + an un-drained-overflow ALARM (spec-panel P0 — audit is EVIDENCE, not a
  recoverable cache).** A plain count-trim can drop events the consumer hasn't drained yet → silent evidence
  loss. So: set `MAXLEN ~ 1_000_000` (a pure OOM backstop, far above any realistic consumer lag), AND the
  consumer's health-check **emits an alarm** (decision record §6 "liveness must be visible") when the stream
  length approaches the ceiling OR the consumer-group lag (`XINFO GROUPS` pending/lag) exceeds a threshold —
  turning "evidence about to be lost" from silent into a visible signal *before* any trim. Dropping-oldest is
  acceptable ONLY as the last-resort OOM backstop, never as routine backpressure.

## 3. The event schema — one flat envelope, joined by run-id (§8)
```
{ run_id: str,           # minted by the orchestrator at panel start, propagated to every seat it fires
  seq: int,              # monotonic from the ORCHESTRATOR's POV (orderable across clock-skewed boxes)
  source: str,           # "orchestrator" | "<seat-id>"  (the discriminator)
  ts: iso8601,           # wall-clock (provenance only — NOT used for ordering; seq is)
  kind: str,             # "dispatch" | "vote" | "verdict" | "position" | "divergence" | …
  payload: {...} }       # kind-specific
```
- **Orchestrator-level** events = the decisions (dispatch, vote outcomes, certifier verdict).
- **Seat-level** events = each model's position + where it diverged.
- Both together reconstruct *how a panel reached a verdict* → the audit log is a **superset of the
  disagreement corpus**; every panel auto-produces the raw material.
- **Decision-grain, NOT trajectory-grain — ENFORCED structurally (spec-panel P0), not by prose** (decision
  record §10, settled): the seat's final position + vote, not its full reasoning stream. The emit path
  **caps `payload` at a structural size limit** (`AUDIT_MAX_PAYLOAD_BYTES`, e.g. 16 KB) — an oversized
  payload is **rejected at `audit_emit`** (raise) so the producer can't quietly stream a trajectory into the
  decision log; the table stays a queryable decision log by construction. The trajectory firehose is a
  separate future capture (§9) with its own store + grain.

## 4. Emit helpers (the producer side)
- `audit_emit(redis, *, run_id, seq, source, kind, payload, prefix=PREFIX)` — `XADD … MAXLEN ~ 50000` a flat
  event. Fire-and-forget (does NOT wait).
- **`seq` allocation — Valkey-INCR per run (spec-panel P0, the single most important pin).** seq is allocated
  by `INCR agent_scratch:arbmem:audit:run:<run_id>:seq` (with a TTL on the counter key) — an **atomic,
  monotonic, collision-free** counter ANY emitter (orchestrator or seat) increments to get its next seq. No
  emitter ever mints a seq locally → `(run_id, seq)` is **unique-by-construction** → the idempotency key (§5)
  is a true redelivery-dedup, never a false-dedup of two different events. This replaces the ambiguous
  "orchestrator stamps OR seats mint" and removes the seat↔orchestrator round-trip while still giving one
  total order per run (consistent with §8's "monotonic, orderable across clock-skew"; the shared counter,
  not wall-clock, is the order).
- `AuditRun(redis, run_id)` — a helper holding `run_id` + the INCR allocator; exposes
  `emit(source, kind, payload)` (INCR seq → `audit_emit`). Seats receive `run_id` and call the same INCR.
  Phase 2 ships the helper; the orchestrator/skill wiring is Phase 3.

## 5. The audit consumer (fire-and-forget, at-least-once, idempotent)
- Reuse Phase 1's loop/resilience pattern (one loop — there is no read lane): `ensure_group` (group
  `arbmem-audit`), startup `drain_pending`, live `XREADGROUP … >`, **XACK after the Postgres commit**.
- **Idempotent insert — fail LOUD on a real collision (spec-panel P0).** `content_hash` = sha256 over the
  canonical event (`run_id\0seq\0source\0kind\0` + canonical-json(payload), sorted keys, no whitespace).
  `INSERT … ON CONFLICT (run_id, seq) DO UPDATE … WHERE excluded.content_hash = audit_events.content_hash`
  is a no-op for a TRUE redelivery (same hash); but if a row already exists at `(run_id, seq)` with a
  **different** `content_hash`, that is a seq-collision bug (two different events) — the consumer must
  **fail loud: dead-letter the event** (to `arbmem:audit:deadletter` + log an error) and NOT silently drop it
  (`DO NOTHING` would have hidden unrecoverable evidence loss). With the Valkey-INCR allocator (§4) a real
  collision should be impossible — so reaching the dead-letter path is itself the alarm that the allocator
  was bypassed. One transaction; XACK after commit.
- **Record-shaped (§8):** the consumer stores `stream_entry_id` (the bus entry id), `content_hash`
  (sha256 of the canonical event), and `raw_entry` (the verbatim bus fields) alongside the structured
  columns — so the immutable-object-store sink + training-export sink become later *additions*, never
  refactors.
- **Poison-resilience (Phase 1 lesson):** a malformed/parse-error or deterministic content-error audit event
  is logged + **acked-and-dropped** (an audit event is not worth crash-looping the audit consumer over —
  and dropping one debugging-tier event is acceptable); only INFRA errors (redis/psycopg operational) retry.

## 6. The sink seam (§8 — build the socket, ship one cord)
- `AuditSink` interface = `write(event) -> None` + `required` (a list, `["postgres"]` now). The consumer
  builds a **sink list of length one** (`[PostgresAuditSink]`) and a **named ack policy**: "XACK after the
  required sinks succeed; required = [postgres]". The object-store sink + training-export sink are later
  ADDITIONS to the list — NOT a refactor. **Do NOT build the object-store sink now** (§9 deferral).

## 7. Tests (deny-proof style; local redis db 15 + Phase 0 pgvector)
- `test_audit_event_roundtrip` — `audit_emit` → drain → row in `audit_events` with the right
  run_id/seq/source/kind/payload + record-shaped columns populated.
- `test_audit_idempotent_on_redelivery` — same `(run_id, seq)` **and same content_hash** delivered twice →
  one row (true redelivery). Deny-proof: a non-idempotent insert raises a unique violation → red.
- `test_seq_collision_fails_loud` (spec-panel P0) — two DIFFERENT events forced onto the same `(run_id, seq)`
  (different content_hash) → the consumer **dead-letters + raises/logs an error**, does NOT silently keep
  only one. Deny-proof: a `ON CONFLICT DO NOTHING` impl silently drops the second → the dead-letter is empty
  and the event is gone → red.
- `test_seq_allocator_is_unique_under_concurrency` — N concurrent emitters INCR the same run's counter →
  N distinct seqs (no dup, no gap-causing race). Deny-proof: a local/in-memory per-emitter counter collides
  → duplicate seqs → red.
- `test_oversized_payload_rejected` (spec-panel P0) — `audit_emit` with a payload > `AUDIT_MAX_PAYLOAD_BYTES`
  → raises (the decision log can't drift into a trajectory store). Deny-proof: an uncapped emit accepts it.
- `test_audit_xack_after_commit` — crash (DB failure) before commit → entry NOT acked → redelivered →
  succeeds on retry. Deny-proof: ack-before-commit loses the event on crash.
- `test_audit_poison_dropped` — a malformed audit event → logged + acked-and-dropped, consumer survives,
  next event processed, no restart crash-loop. Deny-proof: no-catch → thread dies/crash-loops.
- `test_seq_orders_within_a_run` — emit out-of-wall-clock-order but in seq order → query
  `ORDER BY seq` reconstructs the orchestrator's order (not ts). Deny-proof: ordering by ts mis-orders under
  skew.
- `test_run_join_superset_of_disagreement` — emit orchestrator dispatch + 3 seat positions + a verdict for
  one run_id → query by run_id reconstructs the full panel (the superset-of-disagreement-corpus claim).
- `test_sink_list_is_length_one_with_named_ack` — structural: the consumer's sink list is `[postgres]`, the
  ack policy names its required sinks; adding a sink is a list append (a test asserts the seam shape so a
  future object-store sink is an addition not a refactor).

## 8. Opens — resolved by the panel (all load-bearing closed pre-build)
- **`seq` allocation — CLOSED:** Valkey-INCR per run (§4). Unique-by-construction; no orchestrator
  round-trip; one total order per run.
- **`content_hash` form — PINNED (§5):** `sha256(run_id\0seq\0source\0kind\0 + canonical-json(payload))`.
- **`MAXLEN` vs evidence — CLOSED (§2):** high OOM ceiling (~1M) + un-drained/lag alarm; trim is last-resort
  OOM backstop only, never routine. The §1 "audit earns durability" invariant honored by making loss
  *visible before it happens*.
- **`(run_id, seq)` idempotency — CLOSED (§5):** unique-by-construction via the allocator; `ON CONFLICT`
  fails LOUD (dead-letter) on a content_hash mismatch rather than silently dropping. No false-dedup path.
- **Decision-grain drift — CLOSED (§3):** structural `AUDIT_MAX_PAYLOAD_BYTES` cap at emit, not prose.
- **§6a (codex):** audit writes ONLY to configured sinks after validation; event content can NEVER direct a
  write target or an ack (mirrors Phase 1's no-arbitrary-write bar). Stated as a build constraint.
