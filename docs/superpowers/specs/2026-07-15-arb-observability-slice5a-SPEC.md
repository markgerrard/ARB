# SPEC — ARB Observability Slice 5a: span data layer + retention

**Status:** SPEC **v3 — folded from spec panel r1** (run `panel-slice5a-spec-r1-20260715T000658Z-0a2f87`,
terra needs-changes/P1, agy/grok/cold-Opus approve; orchestrator-folded). ·

> **v2 → v3 fold.** (1) **Emit routing made implementable** (terra P1; grok verified the same surface
> at P2): claude-tail tees eval events via `Tailer._emit_eval` → direct `xadd` (`tailer.py:446-458`),
> gated by the event-type sets `LIVE_AND_TRACE_EVENTS`/`LIVE_ONLY_EVENTS` (`tailer.py:22-29`) and
> `turn_index` stamping by `TURN_INDEXED_EVENTS` (`tailer.py:449-451`) — `turn_finalized` /
> `turn_finality_retracted` are in NEITHER, so v2's "no new emit surface" claim was false. D8 now
> REQUIRES the explicit routing change + an e2e assertion. (2) **D2.9 orphan scope pins** (grok +
> cold-Opus converged P2): orphan = pair-keyed finish edges ONLY; `task_finished` closing zero open
> children is a normal success no-op; a D5 `turn_finalized` UPDATE that matches zero rows because the
> sticky `IS DISTINCT FROM 'retracted'` predicate excluded a PRESENT retracted row is a refuse-reopen
> no-op — distinguishable from a true orphan by row presence; three unit assertions added. (3)
> Wording/citation nits: pi_rpc/agy-print emit `turn_completed` on success (the derived close is for
> STILL-OPEN children on their recovery paths); `eval_event_raw_inserted_at_idx` is `run.py:116`; the
> shared transaction lives in `PostgresEvalSink.write`. (4) Deploy pin added: exactly ONE active
> `EvalConsumer` instance per consumer group (multi-instance same-group is the residual reorder
> surface under UPDATE-only — grok).

> **v1 → v2 fold.** (1) **span_deadletter write path restored** (grok + cold-Opus P1 — the settled
> orphan-finish/no-FIFO/dual-except folds had been dropped; D2.5's finish-INSERT arm contradicted
> them): finish edges are now UPDATE-only — an orphan finish (no existing row at equal epoch)
> deadletters, never creates a partial row; new D2.9 + tests + silent-drop deny-proof. (2)
> **Transaction wiring pinned** (terra P1 — `PostgresEvalSink.write` owns its own transaction,
> `eval.py:23`, and sinks run sequentially, `eval.py:145`, so a separate sink CANNOT share the raw
> insert's transaction): D2 is normatively an EXTENSION of `PostgresEvalSink.write` inside its
> existing `conn.transaction()`; the "new sink" option is removed; injected-failure rollback test
> added. (3) **Retention-role privileges made exact + testable** (terra P1; agy/cold-Opus P2):
> DELETE + column-level `SELECT (inserted_at)` on the two raw tables, purge tests MUST run connected
> as the role; consumer role additionally gets SELECT on the span tables (ledger reads). (4) **D8
> contradiction fixed** (agy P1; grok/cold-Opus P2 — `finality_evidence` is NOT already present).
> (5) **`transcript_io` gains an `inserted_at` index with the F5 fix** (cold-Opus P2 — only `ts` is
> indexed today). (6) **Terminal-model tests restored to the design's full list; `turn_finalized`
> emit routing named** (grok P2s).
**Design:** `docs/superpowers/specs/2026-07-14-arb-observability-slice5a-design-v3.md` **v8 — CONVERGED
(Mark-ratified 2026-07-14)** — mechanism/why authoritative there; this spec translates it, does not
reopen settled folds (incl. the Mark-ratified state-based O5 handshake and the warm-terminal descope). ·
**Upstream contract:** `docs/superpowers/specs/2026-07-13-arb-observability-slice5a-0-capture-normalization-SPEC.md`
§ "Contract" (O1–O5, O-gate) — merged `536f741`, fleet-deployed. · **Precondition:** M2 owner-fenced
recovery `e2d16b0`. · **Author:** cold-Opus spec author (first draft). · **Roadmap parent:**
[[arb-observability-roadmap]] Slice 5.

Slice 5a = **the span data layer + retention**: it projects the durable `eval_event_raw` stream (already
written by `EvalConsumer`, `eval.py:23-42`) into three timing-span tables + a deadletter, honouring the
five capture obligations 5a-0 handed forward (O1–O5) and the live gate (O-gate). It adds nightly
retention purges with a pinned 56-day contract, least-privilege grants for the new tables, and the O5
terminal-turn latency finalizer in the claude-tail service. **5a owns the projection SQL and the
producer-close handshake; it does NOT re-open 5a-0's capture code.** Per [[cross-slice-claims-need-citation]]
every claim about code 5a does not own is grounded in a `file:line` citation.

---

## Scope

**In scope.** (1) Span DDL: `eval_turn`, `eval_tool_call`, `eval_task`, `span_deadletter`. (2)
Epoch-ledger-gated span projection inside `EvalConsumer._handle_entry`'s transaction
(`eval.py:124-181`). (3) O4 per-producer latency bases incl. claude-tail `clock_invalid` fail-closed.
(4) The O5 terminal-turn finalizer in the claude-tail service (`service.py`). (5) Projection of
`turn_finalized` / `turn_finality_retracted`. (6) Nightly retention purges + the F5 `transcript.py`
fix. (7) Grants for the new span tables. (8) Three allowlist additions.

**Non-goals (explicit).**
- **5b dashboard is OUT** — this slice makes `recovered` a distinct, queryable outcome; rendering it is 5b.
- **Token / cost accounting is OUT** — no `prompt_tokens`/`completion_tokens` rollups (they exist in the
  allowlist for a later slice; 5a projects timing only).
- **No table partitioning** (settled v2 fold; retention is `DELETE`-batched, `eval.py:211-231`).
- **Historical `eval_event_raw` rows are OUT** — absent-`attempt_epoch` events skip span projection (Q2).
- **Warm-session terminal-turn O5 recovery is DESCOPED with rationale** (design v8 fold): no citable
  irrevocable session-end fence exists for a warm interactive session — sessions flap and resume by
  design (`service.py:118-124` persists a draining record on any mid-life deregister; `service.py:274-277`
  drops it without a finish on re-registration), so "draining record exists" proves a registry gap, not
  producer closure. Warm terminal turns project NULL/`open` — honest, countable, visible in 5b as `open`.
  O5 stays IMPLEMENTED for its motivating bulk class: single-dispatch cold-seat terminal turns.

---

## Deliverables (each independently testable)

### D1 — Span table DDL

Create (idempotent `CREATE TABLE IF NOT EXISTS`, in `arb_memory` schema alongside `eval_event_raw`,
`run.py:93-127`) exactly the design's v8 schemas:

```
eval_tool_call: run_id, task_id, seat_id, orchestrator, attempt_epoch, turn_index, tool_call_id,
                tool_name, started_at, finished_at, latency_ms, latency_basis, exit_code, ok, outcome,
                started_stream_id, finished_stream_id, inserted_at, updated_at
                UNIQUE (run_id, task_id, tool_call_id)
                outcome ∈ {open, finished, timeout, incomplete, clock_invalid}

eval_turn:      run_id, task_id, seat_id, orchestrator, attempt_epoch, turn_index, started_at,
                completed_at, latency_ms, latency_basis, tool_call_count, ok, outcome, close_basis,
                finality_evidence, inserted_at, updated_at
                UNIQUE (run_id, task_id, turn_index)
                outcome ∈ {open, finished, timeout, incomplete, clock_invalid, recovered}
                close_basis ∈ {turn_completed, task_finish_derived, turn_timeout, turn_finalized, none}

eval_task:      run_id, task_id, seat_id, orchestrator, attempt_epoch, started_at, finished_at,
                duration_ms, turn_count, tool_call_count, ok, outcome, inserted_at, updated_at
                UNIQUE (run_id, task_id)
                outcome ∈ {open, finished, timeout, incomplete}

span_deadletter: id, run_id, task_id, event_type, raw_entry jsonb, error, stream_entry_id UNIQUE, ts
```

- `latency_basis ∈ {sent_at, event_ts}`. `finality_evidence` is text; the only value the projection
  writes on close is `'retracted'` (from `turn_finality_retracted`) or the finalizer's evidence token
  (`'fd_quiescence'`) — it MUST NOT be a free-text column beyond these bounded tokens.
- **`attempt_epoch` is a COLUMN, not key material** (O1/O2 replace, never accrete) — the UNIQUE keys carry
  no epoch dimension.
- Enum columns are `text` with a `CHECK` constraint listing the exact members above (an out-of-vocabulary
  outcome must fail the insert, not silently store).

### D2 — Epoch-ledger-gated span projection (inside the consumer transaction)

Every turn/tool/task event's span projection happens **inside the same transaction** as the raw insert.
**Wiring (normative, r0 terra P1):** the projection is an EXTENSION of `PostgresEvalSink.write`
(`eval.py:23-42`) inside its existing `conn.transaction()` block. It is NOT a separate sink:
`PostgresEvalSink.write` opens and commits its own transaction and `EvalConsumer` calls sinks
sequentially (`eval.py:145`), so a second sink would run in a second transaction and break the
atomicity the design requires. **Atomicity test (required):** an injected projection failure mid-write
aborts the raw insert too — neither raw row nor span rows land — and PEL redelivery replays both.
Idempotent under PEL redelivery. **The consumer-code branch form below is NORMATIVE; any
single-statement SQL sketch is illustrative only** (design r5 grok pin). Order:

1. **Ledger read-or-establish (gate for ALL writes).** Upsert-and-lock the `eval_task` row as the epoch
   ledger for `(run_id, task_id)`: `INSERT … ON CONFLICT (run_id, task_id) DO UPDATE SET attempt_epoch =
   GREATEST(eval_task.attempt_epoch, EXCLUDED.attempt_epoch) RETURNING …` (equivalently `SELECT … FOR
   UPDATE` then branch) — the row lock serialises concurrent epoch races. The ledger is
   **self-establishing**: the first projected event of a `(run_id, task_id)` creates it whatever its type
   (Q1 CLOSED — dispatch allocates the epoch before `task_started`, `bridge.py:1291-1294`, but the ledger
   does not rely on ordering).
2. **O3 — ignore stale (ALL writes).** `incoming_epoch < stored_epoch` ⇒ **no turn/tool/task writes at
   all, INSERTs included** — return. This is consumer-code branching on the locked value, NOT an
   `ON CONFLICT … WHERE` clause: the design's r0 ghost-INSERT hole (a stale lower-epoch event whose natural
   key was O2-deleted INSERTing a ghost row with no conflict to gate on) cannot occur because nothing is
   written on the stale branch.
3. **O2 — supersede on bump.** `incoming_epoch > prior_stored_epoch` ⇒ `DELETE` `eval_tool_call` and
   `eval_turn` rows for `(run_id, task_id)` with `attempt_epoch < incoming` (kills surplus higher ordinals
   from a shorter re-run + prior tool rows), reset `eval_task` rollup fields to the new attempt. Replay-safe:
   a redelivered bump finds `stored == incoming` (equal, not greater) ⇒ O2 no-ops.
4. **O1 — replace on bump.** Subsequent upserts for the new epoch write fresh `started_at` etc.; the O2
   delete already removed prior-attempt rows so no cross-attempt splice is possible. The upsert `DO UPDATE`
   arm additionally carries `WHERE <table>.attempt_epoch <= EXCLUDED.attempt_epoch` (belt).
5. **Equal-epoch field-scoped writes.** The **start** edge upserts with `DO UPDATE SET started_at =
   COALESCE(<table>.started_at, EXCLUDED.started_at)` (+ other start-only fields likewise COALESCEd) —
   fills a NULL left by a prior orphaned state, never clobbers a real value. The **finish** edge is
   **UPDATE-ONLY — it has NO INSERT arm** (v2 fold, r0 grok/cold-Opus P1): it SETs ONLY
   completion-side fields — `finished_at`/`completed_at`, `latency_ms`, `latency_basis`, `outcome`,
   `close_basis`, `finality_evidence`, `exit_code`, `ok`, `finished_stream_id`, `updated_at` — never
   start fields (this list is NORMATIVE). If the UPDATE matches **zero rows** (no span row exists for
   the natural key at this epoch), the finish edge is an **ORPHAN → deadletter (D2.9), never a
   partial row**. Within one attempt the stream orders start before finish (single consumer group,
   append order), so a legitimately-open row always exists by the time its finish arrives; a
   redelivered `turn_started` after `turn_completed` cannot regress the row to `open` or null its
   completion.
6. **Absent `attempt_epoch`** (pre-5a-0 event, any type incl. task lifecycle) ⇒ **skip span projection
   uniformly**, bump a `span_skipped_no_epoch` counter (Q2 CLOSED — consistency over any task-as-1 carve-out).
7. **claude-tail epoch is constant `1`** (5a-0 R1) ⇒ O1–O3 are structurally inert for claude-tail rows;
   asserted by a test.
8. **Rollup counters are projection-maintained increments.** `eval_turn.tool_call_count`,
   `eval_task.turn_count`, `eval_task.tool_call_count` are incremented as child spans close in the
   projection — NEVER copied from an edge payload (a redelivered finish edge must not double-count; dedup is
   via the child span's own UNIQUE key transition to a closed outcome).
9. **span_deadletter write path (v2 fold — restores the settled design rules, [[evidence-store-no-silent-drop]]).**
   - **Orphan finish / un-pairable finish** (zero-row UPDATE in D2.5; or a tool/turn edge carrying an
     `attempt_epoch` but MISSING its pairing id — no `tool_call_id` on a tool edge, no `turn_index` on
     a turn edge): **no span row**, `INSERT INTO span_deadletter` (UNIQUE `stream_entry_id` — replay-
     idempotent). **No FIFO pairing under ANY missing-id condition** (settled D4: a wrong latency in a
     timing dataset is worse than an absent one).
   - **Scope pins (v3 fold — what is NOT an orphan):** the orphan rule applies to **pair-keyed finish
     edges** (`turn_completed`, tool finish edges, `turn_timeout`) **plus the row-absent
     `turn_finalized` case in pin (b) below** — nothing else. (a) `task_finished` closing
     ZERO still-open children is the NORMAL success path — a no-op, never a deadletter. (b) A D5
     `turn_finalized` UPDATE matching zero rows **because the sticky `IS DISTINCT FROM 'retracted'`
     predicate excluded a PRESENT retracted row** is the refuse-reopen no-op working as designed —
     distinguish by row presence: zero-row + row EXISTS with `finality_evidence='retracted'` → no-op;
     zero-row + row ABSENT → orphan → deadletter. One unit assertion each (three total).
   - **Dual-except discipline:** infra errors (psycopg/redis) abort the shared transaction → PEL
     redelivery retries; deterministic malformed events → deadletter + ack, NEVER ack-and-drop, NEVER
     retry-forever. (Absent-epoch events are NOT deadletters — they skip with the counter, D2.6:
     pre-5a-0 traffic is expected, not malformed.)
   - The deadletter INSERT happens in the SAME transaction as the raw insert (atomic with the ack path).

### D3 — O4 latency bases per producer

- **Dispatch producers** (`event_ts` absent): `latency_basis=sent_at`; turn latency = `turn_completed.sent_at
  − turn_started.sent_at`; tool spans pair by `tool_call_id` (5a-0 D1 canonicalised), each edge's own
  `sent_at`.
- **claude-tail** (`event_ts` present, from 5a-0 R2): `latency_basis=event_ts`. Turn latency is computed
  **ONLY** when the closing edge's `turn_clock_monotonic` is `true`, = `event_ts − turn_started_ts`. False
  or missing ⇒ `latency_ms NULL`, `outcome=clock_invalid`. **Never a `sent_at` fallback** for claude-tail.
  Tool spans pair by `tool_call_id`, each edge's own `event_ts`, non-decreasing or NULL + `clock_invalid`.
- A claude-tail turn with `turn_started` and no close stays `open`/NULL until `turn_finalized` (→
  `recovered`) or nothing (stays `open` — honest, countable). **A turn with `turn_started` and no `true`
  `turn_completed` MUST be treated as flag=`false` ⇒ NULL** (5a-0 O-gate editorial fold: "flag false" and
  "no true close" are the same fail-closed semantics).

### D4 — O5 terminal-turn latency finalizer (claude-tail service)

The finalizer lives in the claude-tail `Service` (`service.py`). It recovers latency for **single-dispatch
cold-seat terminal turns** (no next-human-`user` close) via a state-based handshake — observed
fd-relinquishment + a durable digest-watch with horizon revalidation. **Mark ratified (2026-07-14) that
this state-based handshake IS the O5 "producer-close handshake."** Mechanism (design § O5):

0. **Finalization hold.** From the moment a turn is NOMINATED, the candidate's state (nominating →
   quiescing → confirming → watched) places a **hold** that blocks the cold-seat completion path's file
   deletion and tailer removal (the owned surface: `_delete_cold_seat_files` `service.py:486-495`, tailer
   removal `service.py:197-201`, both fired on `_cold_seat_completed` `service.py:471-477`). The hold
   releases only when the watch CLOSES or the candidate is ABANDONED
   (`ARB_TAIL_FINALITY_ABANDON_SECS`, default 300 — turn stays NULL, deletion proceeds).
   - **Startup re-nomination (cold specs ONLY).** Candidacy is restart-durable by RE-DISCOVERY, not by
     persisted RAM state. At service start, for each cold spec with a completed sidecar, NO watch record,
     and NO retracted marker, the service performs a **forced byte-0 continuous observation** that
     explicitly IGNORES the persisted offset (a normal `poll()` resumes at EOF, `tailer.py:123-131`, and
     would observe nothing) and is **candidacy-evidence ONLY** — it never re-emits capture edges, never
     commits offsets (`offset.py:58-67`), never forges capture-side `turn_completed`/`turn_clock_monotonic`.
     Clean whole-turn observation to clean EOF ⇒ re-nominate; dirty scan ⇒ no nomination, stays NULL
     (fail-closed). **Ordering (normative):** startup re-nomination runs and places its holds BEFORE any
     teardown-capable tick.
1. **Nomination.** A turn is a candidate iff: `turn_started` emitted, no `turn_completed`; the tailer's OWN
   5a-0 scan-continuity state for the turn is clean (shared predicates — `_turn_clock_ok` etc.,
   `tailer.py:310`, generation continuity, clean parse — up to a clean EOF); AND the ONE irrevocable
   terminal stop signal fired: the **cold-seat sidecar `completed:true`** (written once by the
   `SubagentStop` hook, `subagent_stop.py:24-25`; the sidecar carries no final cursor — its truth is the
   `completed` boolean alone). **Idle-finish NEVER nominates** (`service.py:202-207`, design r0 grok P1-4).
   **Draining/deregistration records NEVER nominate** (registry-flap markers, `service.py:274-277` drops
   them on re-registration without a finish). **Warm-session terminal turns are explicitly descoped** —
   they project NULL/`open`.
2. **Quiescence observation.** Resolve the `.output` symlink (`subagent_start.py:38-41`); identify the
   TARGET `(path, inode)` — the watch holds the resolved target, not the symlink. Enumerate open write
   descriptors on the target inode (macOS `lsof`, Linux `/proc/*/fd`). Earn requires **zero write-open fds**.
   **Named precondition:** the finalizer runs same-host, same-user as the producer (true by construction
   today — both are the operator's processes on this Mac). `lsof` error, timeout, or permission failure ⇒
   **no earn, stay NULL (fail-closed).** Candidate checks are **batched per service tick**, never per-poll.
3. **Confirmation read + digest.** After quiescence: re-stat (same inode, same size), re-verify clean EOF,
   record `sha256` over the turn's byte range `[turn_start_offset, final_size)`. Any mismatch ⇒ no emit;
   growth ⇒ wait.
4. **Durable watch BEFORE emit (watch-before-emit ordering).** Atomically persist a finalization-watch
   record (same store/pattern as the 5a-0 offset composite, `offset.py:58-67`): key
   `watch:{path|target_inode}:{turn_index}` → `{run_id, task_id, turn_index, target_path, target_inode,
   turn_start_offset, observed_size, digest, event_ts, turn_started_ts, status, finalized_at, horizon_end}`,
   `status` written `watched` at creation. Only after the watch is durably written does the service emit
   **`turn_finalized`** `{turn_index, attempt_epoch: 1, event_ts, turn_started_ts, finality_evidence:
   "fd_quiescence", observed_inode, observed_size}`. `turn_finalized.event_ts` = the turn's LAST
   cleanly-parsed record's transcript `timestamp` (recovered latency measures last observable activity,
   deliberately excluding terminal dead-time). Watch write fails ⇒ no emit (fail-closed). **At-most-once +
   sticky retract:** at most ONE successful `turn_finalized` per `(run_id, task_id, turn_index)`; after a
   `turn_finality_retracted` that turn is NEVER re-finalized (the live-or-retracted watch record blocks
   re-emit).
5. **Watch lifecycle.**
   - **Teardown deferral:** covered from NOMINATION by the hold (step 0); after earn the live watch
     continues the hold until close.
   - **Re-arm on start (the NINE-cell matrix, below).** Service startup scans the watch namespace and
     re-arms each record: stat `target_path`, compare inode + size, re-hash `[turn_start_offset,
     observed_size)` against `digest` — fully computable from the record alone.
   - **Retraction triggers.** The watch guards the byte range `[turn_start_offset, observed_size)`. Any
     mutation of that range — shrink below `observed_size`, inode swap, digest mismatch — **retracts**.
     Appends BEYOND `observed_size` are read + parsed: records belonging to the finalized turn retract it;
     records opening a LATER turn do not. Retraction emits `turn_finality_retracted`.
   - **Horizon end.** The watch closes at `min(target-inode deletion, finalized_at +
     ARB_TAIL_FINALITY_HORIZON_SECS)` (default 900), and closing REQUIRES a **final revalidation** (re-stat
     + fd-quiescence re-check + digest re-verify over the recorded range). **Transient tool failures (lsof
     timeout/error/permission) DEFER the close to the next tick — they NEVER retract** (only definitive
     state changes retract). **Pass ⇒ the closing sequence is: SET `status=closing` (durable) → unlink files
     → delete the watch record.** **Retraction ⇒ writes `status=retracted` (7-day TTL marker) AND performs
     the same discovery-artifact teardown as a passing close** (unlink the sidecar/symlink — **cold-only,
     never the parent session's real transcript**), so startup re-nomination is structurally impossible for
     a retracted turn even after the marker expires.

**Normative NINE-cell re-arm matrix** (design v7; keyed `{target present?} × {status}`):

| | `watched` | `closing` | `retracted` |
|--|--|--|--|
| present + validates | continue watch + idempotent re-emit | resume close (unlink → delete record) | sticky: no re-emit, no re-arm |
| present + mismatch | retract | resume close, NO retract | sticky |
| missing | retract (mid-horizon loss) | self-heal (delete record, span stays `recovered`) | sticky |

`status=closing` proves the final revalidation already PASSED ⇒ **resume the close in EVERY cell, never
retract** (any post-pass mutation is honest-bounds residual (b), not a retract trigger). `present +
validates`/`watched` **re-emits an idempotent `turn_finalized`** from the record's `event_ts`/`turn_started_ts`
(fire-always normative; projection dedup makes duplicates benign).

### D5 — Projection of `turn_finalized` / `turn_finality_retracted`

- **`turn_finalized`** closes the open turn: `latency_ms = event_ts − turn_started_ts` (O4 arithmetic),
  `outcome=recovered`, `close_basis=turn_finalized`, `finality_evidence='fd_quiescence'`. **The refuse-reopen
  guard is an EXPLICIT SQL predicate on every `turn_finalized` application: `WHERE eval_turn.finality_evidence
  IS DISTINCT FROM 'retracted'`** (the durable belt must be visible in the statement, not implied; the PG row
  outlives any Redis TTL). `recovered` stays distinct from `finished`.
- **`turn_finality_retracted`** sets `latency_ms NULL, outcome=clock_invalid, close_basis=none,
  finality_evidence='retracted'`.
- Both events carry `attempt_epoch: 1` (claude-tail R1) ⇒ they flow through the D2 ledger gate inertly.

### D6 — Retention

- **Nightly scheduled purge one-shots**, driven by host cron + compose one-shot containers (SP3 default:
  host cron over `pg_cron`), invoking the existing `python -m arb_memory eval-purge` (`run.py:67-73`) and
  `transcript-purge` (`run.py:84-90`) subcommands, **which delete raw rows only**.
- **Pinned env contract:** `ARB_EVAL_RETENTION_DAYS=56` and `ARB_TRANSCRIPT_RETENTION_DAYS=56`. The current
  code defaults are **30** (`run.py:70`, `run.py:87`) — the cron/compose unit MUST set both to 56 explicitly.
- **Span-table retention.** The span tables project from raw rows purged at 56d; span rows are derived and
  MUST NOT be purged before their raw source (they survive the raw purge — asserted by the O-gate purge
  boundary). If a span-table purge is added it uses the same 56d window keyed on the span's own `inserted_at`.
- **F5 fix (bug in existing code).** `transcript.py`'s `purge_expired` deletes on `WHERE ts < now() − …`
  (`transcript.py:234-235`) — it MUST purge on **`inserted_at`** (the column exists, `run.py:166`), matching
  the eval purge which already uses `inserted_at` (`eval.py:220-221`). `ts` is the transcript-line timestamp
  (attacker/producer-controlled), not the ingestion clock; retention must key on ingestion time. Fix =
  change the `transcript.py` purge column `ts` → `inserted_at`, **AND add
  `CREATE INDEX IF NOT EXISTS transcript_io_inserted_at_idx ON transcript_io (inserted_at)`** (v2 fold,
  cold-Opus: only `ts` is indexed today — `run.py:174-175` creates `transcript_io_task_ts_idx` and
  `transcript_io_ts_idx`; a 56-day purge scanning 98k+ unindexed rows nightly is a table scan;
  `eval_event_raw` already has `eval_event_raw_inserted_at_idx`, `run.py:116`).

### D7 — Grants (least-privilege)

- **Eval consumer role:** currently `apply_eval_grants` (`grants.py:233-272`) grants SELECT, INSERT on the
  raw tables and REVOKEs UPDATE/DELETE. Extend it to grant the consumer role **SELECT, INSERT, UPDATE,
  DELETE** on `eval_turn`, `eval_tool_call`, `eval_task` (the ledger lock/read and COALESCE upserts need
  SELECT; the O2 supersede needs DELETE — v2 fold, agy), and **INSERT** on `span_deadletter`. Grant
  `USAGE` on the new sequences.
- **Retention role (exact, testable — v2 fold, terra P1):** the purge one-shots run connected as a
  dedicated role, not the consumer/owner. Its grants are EXACTLY: `DELETE` on `eval_event_raw` and
  `transcript_io`, plus **column-level `SELECT (inserted_at)`** on both (the batched purge's `ctid`
  subquery filters on `inserted_at`, `eval.py:217-223` / `transcript.py:231-237` post-F5; if PostgreSQL
  rejects the `ctid` reference under column-level SELECT at implementation, escalate to table-level
  SELECT on the two raw tables ONLY, recording the escalation in the deploy notes). No access to the
  span tables. **The D6 purge tests MUST run connected as this role** — a purge that only works as
  owner fails the deliverable.
- **Nobody new reads `eval_event_raw`.** `apply_local_reader_grants` (`grants.py:34-50`) and
  `apply_mcp_grants` (`grants.py:95-108`) already REVOKE ALL on the raw eval tables from the MCP/local-reader
  roles; the span tables inherit the same posture — the local-reader and MCP roles get **no grant** on the
  new span tables (5b's read role, out of scope here, would be the only future reader).

### D8 — Allowlist additions

Add **three** members to `EVAL_ALLOWLIST` (`eval_tee.py:10-20`): **`finality_evidence`**, **`observed_inode`**,
**`observed_size`** — all bounded scalars (a token / an int / an int), consistent with the extract-only
contract. **None of the three is present today** (v2 fold — v1 falsely listed `finality_evidence` as
already present). Already present as of 5a-0 (`eval_tee.py:19`): `event_ts`, `turn_started_ts`,
`turn_clock_monotonic`, `attempt_epoch`, `tool_call_id`. Additive ⇒ **no `EVAL_SCHEMA_VERSION` bump**
(default; confirm at panel). **Emit routing (v3 fold — REQUIRED routing change, r1 terra P1):**
claude-tail routes events to eval via `Tailer._emit_eval` → direct `xadd` (`tailer.py:446-458`),
gated by event-type membership in `LIVE_AND_TRACE_EVENTS`/`LIVE_ONLY_EVENTS` (`tailer.py:22-29`),
with `turn_index` stamped only for `TURN_INDEXED_EVENTS` (`tailer.py:449-451`). `turn_finalized` and
`turn_finality_retracted` are in NEITHER set today — without a routing change the finalizer's emits
never reach `eval_event_raw` and D5 cannot project. The deliverable therefore INCLUDES (v3 final, r2 cold-Opus:
the routing-set option is NOT viable — tailer `_emit_eval` fires only on transcript lines and stamps
its own current `turn_index`, which is wrong for a service-level synthetic emit): **the finalizer
constructs its records via `build_eval_record` (`bridge.py:152-167`) + the same allowlist extract,
with the finalized turn's explicit `turn_index`, and XADDs via the service's eval Redis
(`_eval_redis_from_env`, `service.py:587`).** **E2E assertion (required):**
both event types XADD to the eval stream carrying `attempt_epoch=1` and the finalized turn's original
`turn_index`, and land in `eval_event_raw`; without the routing change this test is RED.

---

## Semantics pins (settled by the panel; do not reopen)

- **Equal-epoch no-clobber:** start edges COALESCE start fields; finish edges SET only completion fields
  (D2.5). A redelivered `turn_started` after `turn_completed` cannot regress the row.
- **Absent-epoch uniform skip + counter:** any type incl. task lifecycle events skip span projection and
  bump `span_skipped_no_epoch` (D2.6, Q2).
- **claude-tail epoch-1 inertness:** O1–O3 never fire on claude-tail rows (epoch constant 1, R1) — test-asserted.
- **task-finish-derived closes:** `task_finished` closes only still-open children, outcome from `ok`;
  `close_basis=task_finish_derived` for `pi_rpc`/`agy-print`. `turn_completed`→`finished`;
  `turn_timeout`→`timeout`.
- **Stall events never close:** `stall_detected`/`stall_unknown` are never terminal (never write a close).
- **Rollup counters projection-maintained:** never edge-copied (D2.8).
- **Dispatch heuristics never touch claude-tail rows:** claude-tail spans are EXEMPT from all dispatch
  terminal heuristics (their only close is `turn_completed` at a next-human-`user` line or `turn_finalized`).
- **The 5a-0 capture flag is NEVER mutated by O5** (panel-verified clean) — O5 recovers latency at the
  projection layer only.

---

## Acceptance criteria (each contract obligation → deliverable + test)

| Obligation | Deliverable | Acceptance test |
|--|--|--|
| **O1** (replace on bump — no crash-gap splice) | D2.4 | M2-path re-run: turn latency is the re-run's, prior `started_at` not retained. |
| **O2** (supersede prior turn + tool rows) | D2.3 | Higher epoch deletes prior-attempt turn/tool rows; rollups count one attempt; redelivered bump no-ops. |
| **O3** (ignore stale predecessor, incl. INSERTs) | D2.2 | Stale lower-epoch event lands NOTHING — including the ghost-INSERT probe (a late lower-epoch event for an O2-deleted ordinal writes no row). |
| **O4** (claude-tail latency basis + temporal validity) | D3 | `turn_clock_monotonic=false` ⇒ `latency_ms NULL`/`clock_invalid`; true ⇒ `event_ts − turn_started_ts`; never a `sent_at` fallback; no wrong-but-green latency. |
| **O5** (recover terminal-turn latency, cold single-dispatch) | D4, D5 | Cold-seat terminal turn with `completed:true` sidecar earns `turn_finalized` ⇒ `recovered`; warm terminal turn stays NULL/`open` (descope). |
| **O-gate** (live latency + causal validity) | O-gate below | The six branches + four-arm canary + epoch-fence live assertions pass; deny-proofs go red when removed. |

---

## Unit test list (per deliverable)

- **D1:** each enum `CHECK` rejects an out-of-vocab outcome; UNIQUE keys carry no epoch dimension.
- **D2:** stale-INSERT probe (O3 branch skips INSERTs, no ghost row); ledger self-establishment from a
  non-`task_started` first event; O2 redelivery no-op; equal-epoch redelivered `turn_started` after
  `turn_completed` leaves completion intact; out-of-order start edge COALESCEs `started_at` (no permanent
  NULL); absent-`attempt_epoch` uniform skip (incl. task events) + counter; claude-tail epoch-1 O1–O3
  inertness; rollup counters are increments (redelivered finish edge doesn't double-count).
- **D3 + terminal model (v2 fold — the design's full list):** claude-tail `clock_invalid` NULLs latency
  (never `sent_at`); dispatch producer `sent_at` basis; tool-span pairing by `tool_call_id`;
  `task_finished` closes ONLY still-open children (a success does NOT mark children `incomplete`);
  `close_basis=task_finish_derived` for STILL-OPEN children on pi_rpc/agy-print recovery paths (those engines DO emit `turn_completed` on success — the derived close is the fallback, not the norm);
  `turn_timeout` closes `timeout`; `stall_detected`/`stall_unknown` never close anything; dispatch
  terminal heuristics never touch claude-tail rows.
- **D2.9 (deadletter):** orphan finish (zero-row UPDATE) deadletters, creates no partial row;
  missing-pairing-id edge (with epoch) deadletters; NO FIFO pairing under any missing-id condition;
  deadletter INSERT is replay-idempotent (UNIQUE `stream_entry_id`); infra error aborts + redelivers
  (row count unchanged); absent-epoch skips (counter), never deadletters.
- **D4:** finalization hold blocks deletion from nomination; abandon timeout releases the hold + turn stays
  NULL; idle-finish never nominates; draining/deregistration never nominates; watch-before-emit ordering
  (watch write failure ⇒ no `turn_finalized`); quiescence fail-closed on `lsof` error; digest mismatch
  retraction; same-size in-range rewrite after restart retracts (digest path); transient lsof failure at
  horizon end defers (never retracts); teardown order (files-then-record) under a crash between the two
  never re-finalizes; at-most-once earn + sticky retract; crash-window re-emit iff record still validates;
  startup re-nomination (completed sidecar + no watch + no retracted marker → byte-0 re-scan → re-nominate;
  dirty re-scan ⇒ no nomination); retracted-marker TTL + artifact teardown blocks re-finalization; **the
  nine-cell re-arm matrix — every cell asserted.**
- **D5:** `turn_finalized` ⇒ `recovered`; `turn_finality_retracted` ⇒ `clock_invalid`/`retracted`; the
  explicit `finality_evidence IS DISTINCT FROM 'retracted'` predicate present on every `turn_finalized`
  application.
- **D6:** F5 boundary — purge deletes rows past retention on `inserted_at`, NOT on `ts`; span rows survive a
  raw purge; retention env defaults to 56 in the cron unit.

### Deny-proofs (must go RED when the guard is removed)

- Remove the O3 ledger stale-branch → the O3/stale-INSERT test goes red.
- **Silent-drop deny-proof (v2):** remove the D2.9 deadletter INSERT (restore an ack-and-drop or a
  finish-INSERT arm) → the orphan-finish test goes red.
- Remove the injected-failure transaction sharing (project outside `PostgresEvalSink.write`'s
  transaction) → the atomicity rollback test goes red.
- Disable the quiescence check → the O5 canary goes red.
- Disable watch re-arm → canary arm (b) (restart + same-size in-range rewrite) goes red.
- Delete the `finality_evidence IS DISTINCT FROM 'retracted'` predicate → the sticky-retract test goes red.
- **Flap deny-proof:** a `deregister → draining record → re-register` warm session (the `test_service.py`
  flap lifecycle, `service.py:118-124` + `:274-277`) must NEVER produce a `turn_finalized` at any point —
  including across a service restart inside the flap window.

---

## Live gate (O-gate)

**Six branches** (each projects NULL/`clock_invalid`, never a number): (1) pure-text `user` + earlier pure
`thinking`/`text` child + no tool; (2) intermediate trace-only inversion between non-decreasing bookends
with tools; (3) inversion / plain interruption straddling a mid-turn tailer restart or resumable idle-finish
(a fresh generation must never close a turn `true`); (4) corrupt (`json.loads`-failing) in-turn line; (5)
same-object byte-0 re-read (offset-key loss / truncate / inode-swap mid-turn — the replayed opening must not
forge `true`); (6) any terminal stop with no next-human-`user` line ⇒ NULL absent O5 evidence, and with the
finalizer `recovered` is supported **ONLY** by `turn_finalized`.

**FOUR-arm canary** (O5 durability): (a) in-session backward append after finalization ⇒ retraction; (b)
finalize → RESTART the service → same-size in-range rewrite ⇒ the re-armed watch's digest re-verify retracts;
(c) finalize → horizon expiry ⇒ final revalidation ran, watch closed clean (files gone, record gone, span
still `recovered`); (d) crash-between-unlink-and-record: finalize → pass revalidation → kill the service
between unlink and record delete → restart ⇒ self-heal (record gone, NO retraction, span still `recovered`).

**Epoch-fence live assertions on a real M2-path crashed dispatch:** O1 re-run latency correct; O2 single
attempt (no ghosts); O3 stale predecessor inert — including the stale-INSERT probe (a late lower-epoch event
for a deleted ordinal lands NOTHING).

**Plus:** one real dispatch per engine family (agent_sdk, pi_rpc, codex, agy, grok, cursor); the purge
boundary (rows past 56d gone, span tables survive purge); and **pin the Claude Code version observed at gate
time** (the producer flush-ordering property the O5 canary exercises is version-dependent, not statically
citable).

---

## Config surface (new env vars, with defaults)

- `ARB_TAIL_FINALITY_HORIZON_SECS=900` — watch horizon (design § O5.5).
- `ARB_TAIL_FINALITY_ABANDON_SECS=300` — never-earning candidate releases the hold, turn stays NULL.
- `ARB_EVAL_RETENTION_DAYS=56` — pinned (code default 30, `run.py:70`; the cron unit sets 56).
- `ARB_TRANSCRIPT_RETENTION_DAYS=56` — pinned (code default 30, `run.py:87`).
- `ARB_EVAL_CONSUMER_ROLE` (existing, `run.py:233`) — the role D7 extends with span-table grants.
- A new retention-role env (e.g. `ARB_RETENTION_ROLE`) for the DELETE-only purge role (D7).

---

## Deploy pin (v3, grok)

Exactly **ONE active `EvalConsumer` instance per consumer group**. The projection's ordering
assumptions (start-before-finish within an attempt; O2/O3 ledger serialization) rest on single-group
append-order delivery; a second instance in the same group splits the PEL and reintroduces the
reorder surface the design's D5 fold eliminated. The prod compose already runs one `eval` service
replica — this pin makes it a stated invariant, not an accident.

## Open items for the plan stage (implementation-order only — NOT design questions)

- ~~Projection wiring~~ — RESOLVED in v2 (r0 terra P1): D2 is normatively an extension of
  `PostgresEvalSink.write` inside its existing transaction; a separate sink cannot share it.
- **Watch store substrate:** the finalization-watch record reuses the offset-store Redis pattern
  (`offset.py:58-67`); confirm namespace + TTL wiring for the retracted marker at plan.
- **DDL migration ordering vs grants:** span tables must exist before `apply_eval_grants` runs the new
  grants (`run.py:239`); sequence the `setup-schema` extension before `grants` in the deploy one-shot.
- **Cron vs compose one-shot shape** (SP3): host cron is the default; the compose one-shot container shape
  (image, env injection of the pinned 56d values, schedule) is a plan-stage packaging decision.
- **Retention-role provisioning:** creating the DELETE-only role + wiring the purge one-shots to authenticate
  as it is deploy plumbing for the plan.
