# ARB Observability — Slice 5a: span data layer + retention (design **v8 — folded**)

Status: **DESIGN v8 — CONVERGED (Mark-ratified 2026-07-14).** Panel r5 (run
`panel-slice5a-design-r5-20260714T211106Z-f507ac`): agy approve/none, grok + cold-Opus approve/P2
(stale-text nits, folded editorially), terra needs-changes/P1 — an INTERPRETATION dissent on whether
observed fd-relinquishment + self-correcting retraction satisfies O5's "producer-close handshake"
language. **Mark's ratification (constitution-layer spec-meaning call, 2026-07-14): the state-based
handshake (quiescence observation + digest-watch retraction + horizon revalidation + version-pinned
canary) IS the O5 handshake.** Terra's dissent is recorded, not folded; zero unresolved mechanism
P0/P1 remain. Ready for the SPEC stage. Prior fold headers retained below. Date: 2026-07-14.

> **v7 → v8 fold (design panel r4, run `panel-slice5a-design-r4-20260714T210159Z-ca2a91`).** All
> three r3 folds verified closed by three seats. One surviving P1, hinge code-confirmed, folded by
> NARROWING (the Option-D lineage: delete the unprovable surface, don't fence it):
> - **P1 (terra; grok flagged the same class as residual) — a draining record is a registry-FLAP
>   marker, not an irrevocable terminal fence.** `service.py` persists a draining record on any
>   mid-life deregistration (`:119-124`) and the flap-supersede arm (`:274-277`) drops it WITHOUT a
>   finish when the session re-registers — so "draining record exists" proves a registry gap, not
>   producer closure. A live session with per-line open-append-close can show zero write-fds during a
>   thinking pause → false `turn_finalized` on a LIVE turn (retraction churn + up-to-horizon wrong
>   values), violating O5's causally-after-close requirement. **Fix — O5 nomination narrows to the
>   cold-seat sidecar `completed:true` ONLY** (the one explicit, irrevocable stop signal: written once
>   by the SubagentStop hook, never flaps, and its processing path deletes the discovery artifacts).
>   Draining/deregistration records return to **drain-only** — never O5 evidence; the r2 holds on the
>   draining/deregister teardown arms are removed as moot; startup re-nomination returns to cold
>   specs only. **Warm-session terminal turns are an EXPLICIT DESCOPE with rationale** (the contract's
>   "implement or explicitly descope" arm): no citable irrevocable session-end fence exists for a
>   warm interactive session (sessions flap and resume by design), and building one is the machinery
>   class the 5a-0 arc deleted three times (r5/r6/r7). Their terminal turns project NULL — honest,
>   countable, visible in 5b as `open`. O5 remains IMPLEMENTED for its motivating bulk class:
>   single-dispatch cold-seat terminal turns. **Deny-proof added:** a flapping warm session
>   (deregister → draining record → re-register, the `test_service.py` flap lifecycle) must never
>   produce a `turn_finalized`.
> - **P2 folds:** the projection's refuse-reopen guard is an explicit SQL predicate — every
>   `turn_finalized` application carries `WHERE eval_turn.finality_evidence IS DISTINCT FROM
>   'retracted'` (cold-Opus: the belt must not silently vanish in an unguarded upsert; step 6
>   updated); the abandoned-candidate restart residual is noted as bounded (re-nomination within the
>   300s abandon window on a restart loop defers cold-file deletion; after abandonment in any single
>   process lifetime, deletion proceeds and the sidecar's removal ends re-discovery — cold-Opus);
>   grok's stale-text P2s dissolve under the narrowing (cold-only re-nomination is again the correct
>   text; the "third rediscovery class" no longer exists).

> **v6 → v7 fold (design panel r3, run `panel-slice5a-design-r3-20260714T205157Z-2c064e`).** Both r2
> P1 folds verified structurally closed by all seats; no regression. Three folds:
> - **P1 (terra + grok converged) — startup re-nomination was COLD-ONLY.** A warm turn nominated via
>   draining/session-end that restarts pre-watch was silently NULL (the draining re-discovery path
>   resumes at persisted EOF and finishes non-earning, `service.py:186-192, 269-307`). Fix: the
>   re-discovery predicate is GENERALIZED to every nominating trigger — terminal-stop still evident
>   (completed sidecar OR persisted draining record OR session-end evidence from the registry/draining
>   records) + no watch record + no retracted marker → **forced byte-0 continuous observation** →
>   re-nominate. Two pins (grok): the re-nomination scan **explicitly ignores the persisted offset**
>   (a normal `poll()` resumes at EOF and would observe nothing) and is **candidacy-evidence ONLY** —
>   it never re-emits capture edges, never touches the offset store, never forges capture-side
>   `turn_completed`/`turn_clock_monotonic`; and startup re-nomination runs + places its holds
>   **BEFORE any teardown-capable tick** (normative ordering, not implied).
> - **P1 (cold-Opus; grok P2 same substance) — re-arm matrix asymmetry: `{present, closing}` had no
>   cell**, so a crash in the SET-closing→unlink window could retract a close that already PASSED its
>   final revalidation. Fix: `status=closing` means the close passed — **resume the close** (unlink →
>   delete record), never retract, in EVERY cell; post-pass mutation is honest-bounds residual (b),
>   not a retract trigger. The six-cell matrix is now normative (below).
> - **P2 (cold-Opus) — sticky-retract permanence rested on a 7-day-TTL marker while re-nomination
>   fires on marker ABSENCE** (a completed sidecar outliving the marker would re-earn a retracted turn
>   after day 7). Fix: the retract path performs the same discovery-artifact teardown as a passing
>   close (unlink sidecar/symlink — never the parent session's real transcript (cold-only in v8)), so
>   re-nomination is structurally impossible after retraction; AND the projection durably refuses to
>   re-open a turn whose row carries `finality_evidence=retracted` (the PG row outlives any Redis
>   TTL — the durable half of the guard).
>
> **Normative re-arm matrix (v7):**
>
> | | `watched` | `closing` | `retracted` |
> |--|--|--|--|
> | present + validates | continue watch + idempotent re-emit | resume close (unlink → delete record) | sticky: no re-emit, no re-arm |
> | present + mismatch | retract | resume close, NO retract | sticky |
> | missing | retract (mid-horizon loss) | self-heal (delete record, span stays `recovered`) | sticky |

> **v5 → v6 fold (design panel r2, run `panel-slice5a-design-r2-20260714T204403Z-79bb5b`).** Both r1
> P1 folds verified CLOSED by all four seats; no earlier-settled item regressed. Two new P1s + P2s:
> - **P1 (grok) — fold-interaction bug: files-then-record teardown vs missing-target-retract contradict
>   at one crash point.** After a PASSING horizon close, a crash between unlink and watch-record delete
>   leaves `{watch present, target absent}`, which re-arm read as definitive mid-horizon loss →
>   `turn_finality_retracted` destroying a CORRECT recovery — and sticky-retract made it permanent.
>   Fix: the watch record carries a durable **`status ∈ {watched, closing, retracted}`**. Horizon-pass
>   sequence: SET `status=closing` (durable) → unlink files → delete record. Re-arm policy keyed on
>   `{target present?, status}`: missing+`closing` → self-heal (delete record, NO retract, span stays
>   `recovered`); missing+`watched` → retract (definitive mid-horizon loss); `retracted` → sticky,
>   never re-emit. Retracted markers are kept with a 7-day TTL rather than deleted (agy r2 nit).
>   Canary adds a crash-between-unlink-and-record probe: span must stay `recovered`.
> - **P1 (terra) — restart during the nominate→earn window silently NULLed a valid terminal turn**
>   (candidate/hold state was process-local; the contract forbids silently-unmeasured). Fix:
>   **startup re-nomination** — for each cold spec with a completed sidecar, NO watch record, and NO
>   retracted marker, the fresh tailer performs a **full byte-0 re-scan**; if that single fresh
>   generation observes the whole turn cleanly to clean EOF, the turn re-nominates (hold
>   re-establishes, quiesce→confirm→earn proceeds). The evidence is the new generation's own
>   continuous observation — 5a-0-consistent (byte-0 recount, epoch stays 1), no inherited RAM state.
>   (grok rated this surface bounded-residual; the fold takes the strict fix, dissolving the
>   severity disagreement upward.)
> - **P2 folds:** the finalization hold covers **every nominating trigger's** teardown surface — the
>   draining finish (`service.py:186-192`) and deregister finish (`service.py:126-127`) arms are held
>   for candidates exactly like the cold path (cold-Opus P2-1: warm-terminal/session-end turns were
>   losing candidacy pre-earn); the watch record additionally carries **`event_ts`,
>   `turn_started_ts`** so the crash-window re-emit is computable from the record alone, and the
>   re-emit is stated normatively as fire-on-every-valid-re-arm, idempotent via projection dedup
>   (cold-Opus P2-2 + grok P2); rollup counters pinned: `tool_call_count`/`turn_count` are
>   **projection-maintained increments** (updated as child spans close), never copied from edge
>   payloads — the finish-edge SET lists stay complete as written (agy r2 nit, resolved by semantics
>   not by list extension).

> **v4 → v5 fold (design panel r1, run `panel-slice5a-design-r1-20260714T203320Z-efd896`).** All four
> r0 folds verified CLOSED by the panel (O3 ledger + idle-removal fully; O5 durability in mechanism).
> Two new P1s + P2 cluster, folded:
> - **P1 (terra + cold-Opus + grok converged) — the watch record omitted `turn_start_offset` and the
>   resolved target path, making the mandated digest re-verification uncomputable after a restart** (a
>   same-size rewrite inside the finalized range would evade retraction — exactly the forgery the digest
>   exists to catch, on exactly the restart path the durable watch exists to survive). Fix: watch record
>   now carries `{target_path, turn_start_offset}`; re-arm and horizon-end checks hash exactly
>   `[turn_start_offset, observed_size)`; canary arm (b) uses a **same-size in-range rewrite**, not just
>   a size-changing append.
> - **P1 (grok) — teardown deferral was gated on a LIVE WATCH, leaving the multi-tick nominate→earn
>   window unprotected**: sidecar `completed:true` nominates, quiescence takes ticks, no watch exists
>   yet, so today's `_delete_cold_seat_files` path could still fire and destroy the target before earn —
>   the r0 cold-seat class shifted one phase earlier. Fix: a **finalization hold** from NOMINATION —
>   any candidate in state nominating/quiescing/confirming/watched blocks file deletion + tailer
>   removal, with an abandon timeout (`ARB_TAIL_FINALITY_ABANDON_SECS`, default 300) after which a
>   never-earning candidate releases the hold (its turn stays NULL; deletion proceeds).
> - **P2s folded:** start edges upsert with `DO UPDATE SET started_at = COALESCE(existing.started_at,
>   EXCLUDED.started_at)` (+ start-only fields) so out-of-order arrival never leaves a permanent NULL
>   `started_at` and never clobbers (agy); horizon-end revalidation distinguishes **transient tool
>   failures** (lsof timeout/error → defer + retry next tick, never retract) from **definitive state
>   changes** (size/inode/digest/write-fd → retract) (agy); teardown order pinned files-first-then-
>   watch-record so a crash between the two cannot cause re-discovery + duplicate `turn_finalized`
>   (agy); **at-most-once earn + sticky retract** — one successful `turn_finalized` per
>   `(run_id,task_id,turn_index)`, a retracted turn is never re-finalized, the watch/retracted marker
>   blocks re-emit (grok); crash between watch-write and emit → re-arm **re-emits** an idempotent
>   `turn_finalized` iff inode+size+digest still match, else drops the watch (grok); the finish-edge
>   SET list explicitly includes `close_basis`, `latency_basis`, `finality_evidence` (grok); the
>   allowlist additions are restated as normative: `finality_evidence`, `observed_inode`,
>   `observed_size` join `EVAL_ALLOWLIST` (grok — v4 dropped v3's line); the consumer-code branch form
>   is NORMATIVE for the epoch ledger, the GREATEST SQL sketch is illustrative only (grok);
>   `turn_finalized.event_ts` pinned = the turn's LAST cleanly-parsed record's transcript `timestamp`
>   — recovered latency measures last observable activity and deliberately excludes terminal dead-time
>   (cold-Opus); retraction triggers reconciled: the watch guards the byte range
>   `[turn_start_offset, observed_size)` — any mutation of that range (shrink, inode swap, digest
>   mismatch) retracts; appends BEYOND `observed_size` are parsed and retract only if they belong to
>   the finalized turn (cold-Opus); the `service.py:200` `del self._tailers[key]` citation corrected to
>   "the tailer-removal arm of the cold-seat completion path `service.py:197-201`" (cold-Opus nit).
Author: Fable (warm orchestrator session); v3→v4 fold by the same author from the r0 reports.
Supersedes: v3 (this file's prior content), which superseded v2
(`2026-07-13-arb-observability-slice5a-design.md`). v2's five folds stand un-relitigated.

Contract source: `2026-07-13-arb-observability-slice5a-0-capture-normalization-SPEC.md` § "Contract"
(O1–O5, O-gate). Preconditions MET: 5a-0 merged `536f741` + fleet-deployed; M2 owner-fenced recovery
`e2d16b0`. Mark's scope calls (2026-07-14): **O1–O3 IMPLEMENT**, **O5 IMPLEMENT**.

> **v3 → v4 fold (design panel r0, run `panel-slice5a-design-r0-20260714T202118Z-eeed57`; terra + agy +
> grok + cold-Opus all needs-changes/P1; GLM removed mid-round by Mark — abstain, no content).**
> Converged P1s, all hinge-verified before folding:
> - **P1 (terra/grok, high-confidence SQL semantics) — O3 gated only the `DO UPDATE` arm; a stale
>   lower-epoch event whose natural key was O2-deleted INSERTs a ghost row with no conflict to gate on.**
>   Fix: epoch-ledger-gated projection — ALL writes (inserts included) branch on the stored task epoch,
>   read under lock in the same transaction (§ O1–O3 below).
> - **P1 (all four seats) — the O5 retraction watch was in-memory and torn down by
>   `_delete_cold_seat_files` (`service.py:486-495`) + `del self._tailers[key]` (`service.py:200`) at the
>   SAME `_cold_seat_completed` signal (`service.py:197`) that nominates finalization, and lost on service
>   restart (`service.py:129-132`) — the never-durably-wrong floor was violated on exactly the dominant
>   cold-seat path (the r7 forge resurrected at the projection layer).** Fix: durable finalization-watch
>   ledger (watch-before-emit), re-armed on service start, teardown deferred to horizon end with final
>   revalidation (§ O5 v4 below).
> - **P1 (cold-Opus) — the O-gate canary appended while the watch was still live → vacuously green for
>   the durability claim.** Fix: the canary now exercises the restart/teardown gap explicitly.
> - **P1-4 (grok, medium) — idle-finish nomination can earn a false `recovered` mid-turn if the producer
>   open-appends-closes per line.** Fix: idle-finish REMOVED from nomination (r6/r7 history alignment);
>   only sidecar `completed:true`, draining teardown, or true session-end nominate.
> - **P2 folds:** equal-epoch field-scoped SET lists pinned to v2 semantics (agy/grok — a redelivered
>   `turn_started` can never null completion fields); confirmation read records a **byte-range digest**,
>   re-verified at horizon end (terra — same-size rewrite forgery); finalizer shares the tailer's own
>   continuity predicates, not a parallel checklist (grok); POSIX/lsof visibility stated as a named
>   same-user precondition with fail-closed error handling + per-tick batching (grok/agy); Q1 CLOSED
>   (epoch INCR precedes `task_started`, `bridge.py:1291-1303`; ledger self-establishing regardless —
>   cold-Opus verified benign); Q2 CLOSED (absent-epoch events skip span projection uniformly, counted,
>   task rows included — consistency over the v3 task-as-1 carve-out); retention env contract pinned
>   (56d explicit, current one-shot defaults are 30d — `run.py:70,87`); citation re-pins at impl
>   (cold-Opus bookkeeping). Cleared by the panel, recorded: O2 DELETE is replay-safe under PEL
>   redelivery; the capture flag is never mutated by O5; O4/R1/R2 consistency holds.

---

## What changed from v2 (kept from v3, for lineage)

| # | v2 said | v3/v4 does | Why |
|---|---------|-----------|-----|
| 1 | Span keys without epoch | `attempt_epoch` column + O1–O3 epoch fences (v4: ledger-gated, all writes) | 5a-0 contract; Mark: implement |
| 2 | Latency always from `sent_at` | Per-producer bases (O4): claude-tail `event_ts` gated on `turn_clock_monotonic`; `clock_invalid` fail-closed | 5a-0 O4 |
| 3 | (absent) | O5 finalizer: fd-quiescence handshake (v4: durable watch ledger + final revalidation) | 5a-0 O5; Mark: implement |
| 4 | task-finish heuristics for all | claude-tail rows exempt from dispatch terminal heuristics | O-gate semantics |
| 5 | — | O-gate spelled as acceptance (v4: canary covers restart/teardown gap) | 5a-0 O-gate |

Unchanged v2 folds (settled): no partitioning; derive spans inside `EvalConsumer` (D5); exact-id only,
no FIFO (D4); `stall_detected`/`stall_unknown` never terminal; `outcome` columns everywhere; orphan
finish → deadletter; nightly scheduled `purge_expired` + F5 `inserted_at` fix (confirmed still unfixed:
`transcript.py:235` purges on `ts`); least-privilege retention role.

## Inputs (verified)

- `EVAL_ALLOWLIST` (`eval_tee.py:10-20`): `tool_call_id`, `attempt_epoch`, `event_ts`,
  `turn_started_ts`, `turn_clock_monotonic`, `turn_index`, `ok`, `exit_code`, …
- Dispatch producers: epoch = task-scoped Redis INCR allocated BEFORE `task_started`
  (`bridge.py:1291-1303`); `turn_index` bridge-stamped (`bridge.py:2514`); canonical `tool_call_id` both
  edges.
- claude-tail: epoch pinned `1` (R1); persisted `{v, offset, turn_index}`; `event_ts` transcript-native;
  `turn_completed` emitted ONLY at next-human-`user` close on a clean continuous scan (Option-D); a turn
  with `turn_started` and no `turn_completed` IS flag=false.
- `eval_event_raw` rows: `run_id, task_id, seat_id, orchestrator, event_type, sent_at, payload,
  stream_entry_id UNIQUE` — projected in the same consumer transaction.

## Components

1. **Span projection inside `EvalConsumer`** (D5): same transaction as the raw insert; epoch-ledger
   branch (below), then field-scoped upserts; idempotent under PEL redelivery.
2. **Span tables** `eval_turn`, `eval_tool_call`, `eval_task` + `span_deadletter`.
3. **O5 finalizer** in the claude-tail service: durable watch ledger + `turn_finalized` /
   `turn_finality_retracted` events.
4. **Retention**: nightly `purge_expired` via existing one-shots (`run.py:67-73,84-90`) driven by host
   cron + compose one-shot containers; **pinned env contract: `ARB_EVAL_RETENTION_DAYS=56`,
   `ARB_TRANSCRIPT_RETENTION_DAYS=56`** (defaults are 30 — the cron unit must set both); F5:
   `transcript.py:235` purge column → `inserted_at`.
5. **Grants**: eval consumer role +INSERT/UPDATE/DELETE on span tables, +INSERT `span_deadletter`;
   retention role DELETE-only on raw tables; nobody new reads `eval_event_raw`.

## Span table schemas (v4 — unchanged from v3)

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

`latency_basis ∈ {sent_at, event_ts}`. Epoch is a column, not key material (O1/O2 replace, never
accrete).

## O1–O3 — epoch-ledger-gated projection (v4, closes the stale-INSERT hole)

Every turn/tool event's projection, inside the raw-insert transaction, in this order:

1. **Ledger read-or-establish (the gate for ALL writes).** Upsert the `eval_task` row as the epoch
   ledger and lock it:
   `INSERT INTO eval_task (run_id, task_id, seat_id, orchestrator, attempt_epoch) VALUES (...)
   ON CONFLICT (run_id, task_id) DO UPDATE SET attempt_epoch = GREATEST(eval_task.attempt_epoch,
   EXCLUDED.attempt_epoch) RETURNING attempt_epoch, (attempt_epoch > EXCLUDED.attempt_epoch) AS stale,
   ...prior epoch via a CTE...` — implementable equivalently as `SELECT ... FOR UPDATE` then branch in
   consumer code; the row lock serializes concurrent epoch races. The ledger is **self-establishing**:
   the first projected event of a `(run_id, task_id)` creates it whatever its type (Q1 CLOSED — dispatch
   order guarantees `task_started` first anyway, `bridge.py:1291-1303`, but the ledger does not rely on
   it).
2. **O3 (ignore stale, ALL writes):** if `incoming_epoch < stored_epoch` → **no turn/tool/task writes at
   all, including INSERTs** — return. This is consumer-code branching on the locked ledger value, not an
   `ON CONFLICT ... WHERE` clause; the r0 ghost-INSERT path cannot occur (nothing is written on the
   stale branch).
3. **O2 (supersede on bump):** if `incoming_epoch > prior_stored_epoch`: DELETE `eval_tool_call` and
   `eval_turn` rows for `(run_id, task_id)` with `attempt_epoch < incoming` (kills surplus ordinals +
   prior tool rows), reset `eval_task` rollup fields to the new attempt. Replay-safe: a redelivered
   bump event finds stored == incoming (equal, not greater) → O2 no-ops (panel-verified).
4. **O1 (replace on bump):** subsequent upserts for the new epoch write fresh `started_at` etc. — the O2
   delete already removed prior-attempt rows, so no splice is possible; the upsert's `DO UPDATE` arm
   additionally carries `WHERE <table>.attempt_epoch <= EXCLUDED.attempt_epoch` as belt-and-braces.
5. **Equal-epoch field-scoped writes (pinned, v5):** the start edge upserts with
   `ON CONFLICT ... DO UPDATE SET started_at = COALESCE(<table>.started_at, EXCLUDED.started_at)` (+
   start-only fields likewise COALESCEd) — fills a NULL left by out-of-order arrival, never clobbers a
   real value (r1 agy). The finish edge's `DO UPDATE` SETs ONLY the completion-side fields:
   `finished_at/completed_at, latency_ms, latency_basis, outcome, close_basis, finality_evidence,
   exit_code, ok, finished_stream_id, updated_at` — never start fields (r1 grok: list is normative and
   includes the basis/evidence columns). A redelivered `turn_started` after `turn_completed` therefore
   cannot regress the row to open or null its completion. The consumer-code branch form is NORMATIVE
   for the ledger gate; any single-statement SQL sketch (GREATEST etc.) is illustrative only.
- **Absent `attempt_epoch`** (pre-5a-0 event, any type incl. task lifecycle): skip span projection
  uniformly, bump `span_skipped_no_epoch` counter (Q2 CLOSED — consistency over the v3 task-as-1
  carve-out; historical rows are out of scope).
- claude-tail epoch is constant 1 (R1) → O1–O3 structurally inert for claude-tail rows; asserted by a
  test.

## O4 — latency bases (unchanged from v3)

- Dispatch producers (`event_ts` absent): `latency_basis=sent_at`; turn = `turn_completed.sent_at −
  turn_started.sent_at`; tools likewise.
- claude-tail (`event_ts` present): `latency_basis=event_ts`; turn latency computed ONLY when the
  closing edge's `turn_clock_monotonic` is true, = `event_ts − turn_started_ts`; false/missing →
  `latency_ms NULL`, `outcome=clock_invalid`. **Never** a `sent_at` fallback. Tool spans pair by
  `tool_call_id`, each edge's own `event_ts`, non-decreasing or NULL + `clock_invalid`.
- A claude-tail turn with `turn_started` and no close stays `open`/NULL until `turn_finalized` (→
  `recovered`) or nothing (stays open — honest, countable).

## O5 — terminal-turn latency recovery (v5: durable watch, finalization hold, final revalidation)

The r0 panel killed v3's in-memory retraction watch; r1 closed the watch-schema and pre-earn-race
gaps. v5's mechanism:

0. **Finalization hold (r1 grok P1-1; narrowed v8).** From the moment a turn is NOMINATED, the
   candidate's state (nominating → quiescing → confirming → watched) places a **hold** that blocks the
   cold-seat completion path's file deletion and tailer removal (`service.py:197-201, 486-495` — the
   owned change surface, replaced wholesale for candidates). The hold releases only when the watch
   CLOSES or the candidate is ABANDONED (`ARB_TAIL_FINALITY_ABANDON_SECS`, default 300 — turn stays
   NULL, deletion proceeds). Bounded residual (r4 cold-Opus): a restart loop tighter than the abandon
   window re-nominates and defers deletion; after abandonment within any single process lifetime the
   sidecar is deleted and re-discovery ends.
   **Startup re-nomination (r2 terra P1; v8: cold specs ONLY).** Candidacy is restart-durable by
   RE-DISCOVERY, not by persisted RAM state: at service start, for each cold spec with a completed
   sidecar, NO watch record, and NO retracted marker, the service performs a **forced byte-0
   continuous observation**: the scan explicitly IGNORES the persisted offset (a normal `poll()`
   resumes at EOF and would observe nothing, `tailer.py:127-151`) and is **candidacy-evidence ONLY** —
   it never re-emits capture edges, never commits offsets, never forges capture-side
   `turn_completed`/`turn_clock_monotonic`. Clean whole-turn observation to clean EOF → re-nominate
   (quiesce → confirm → earn); dirty scan → no nomination, stays NULL (fail-closed). **Ordering
   (normative):** startup re-nomination runs and places its holds BEFORE any teardown-capable tick.
   A restart during the nominate→earn window therefore delays cold-seat recovery, never silently
   loses it.

1. **Nomination (narrowed twice — v4 killed idle, v8 killed draining/session-end).** A turn is a
   candidate iff: `turn_started` emitted, no `turn_completed`; the tailer's OWN 5a-0 scan-continuity
   state for the turn is clean (same predicates/fields — `_turn_clock_ok`, generation continuity,
   clean parse — shared code, not a parallel checklist) up to a clean EOF; AND the ONE irrevocable
   terminal stop signal fired: **the cold-seat sidecar `completed:true`**. **Idle-finish never
   nominates** (r0 grok P1-4). **Draining/deregistration records never nominate** (r4 terra P1: a
   draining record is a registry-flap marker — `service.py:274-277` drops it on re-registration
   without a finish — not producer-closure evidence). **Warm-session terminal turns are explicitly
   descoped**: no citable irrevocable session-end fence exists; they project NULL/`open` (the
   contract's descope-with-rationale arm; O5 stays implemented for its motivating bulk class,
   single-dispatch cold seats).
2. **Quiescence observation.** Resolve the symlink; identify the TARGET `(path, inode)`. Enumerate open
   write descriptors on the target inode (macOS `lsof`, Linux `/proc/*/fd`). Earn requires zero
   write-open fds. **Named precondition:** the finalizer runs same-host, same-user as the producer
   (true by construction today — both are the operator's processes on this Mac); `lsof` error, timeout,
   or permission failure → no earn, stay NULL (fail-closed). Candidate checks are batched per service
   tick, never per-poll.
3. **Confirmation read + digest.** After quiescence: re-stat (same inode, same size), re-verify clean
   EOF, and record `sha256` over the turn's byte range `[turn_start_offset, final_size)`. Any mismatch →
   no emit; growth → wait.
4. **Durable watch BEFORE emit (watch-before-emit ordering).** Atomically persist a finalization-watch
   record — same store/pattern as the 5a-0 offset composite: key `watch:{path|target_inode}:{turn_index}`
   → `{run_id, task_id, turn_index, target_path, target_inode, turn_start_offset, observed_size,
   digest, event_ts, turn_started_ts, status, finalized_at, horizon_end}` (r1 P1: `target_path` +
   `turn_start_offset` REQUIRED — the digest is meaningless without its range lower bound; r2:
   `event_ts` + `turn_started_ts` REQUIRED — the crash-window re-emit must be computable from the
   record alone; `status ∈ {watched, closing, retracted}`, written `watched` at creation). Only after
   the watch record is durably written does the service emit **`turn_finalized`** (`{turn_index,
   attempt_epoch: 1, event_ts, turn_started_ts, finality_evidence: "fd_quiescence", observed_inode,
   observed_size}`).
   `turn_finalized.event_ts` = the turn's LAST cleanly-parsed record's transcript `timestamp` —
   recovered latency measures last observable activity, deliberately excluding terminal dead-time (r1
   cold-Opus). If the watch write fails → no emit (fail-closed). **Allowlist (normative):**
   `finality_evidence`, `observed_inode`, `observed_size` join `EVAL_ALLOWLIST` (bounded scalars).
   **At-most-once + sticky retract (r1 grok):** at most ONE successful `turn_finalized` per
   `(run_id, task_id, turn_index)`; after a `turn_finality_retracted` that turn is NEVER re-finalized —
   the watch record (live or retracted-marker) blocks re-emit.
5. **Watch lifecycle (r0 P1 fixes + r1 hardening, explicitly superseding today's teardown).**
   - **Teardown deferral:** covered from NOMINATION by the finalization hold (step 0); after earn, the
     live watch continues the hold until close. The watch holds the resolved TARGET, not the symlink
     (the r6 symlink-vs-target fact).
   - **Re-arm on start (policy = the normative six-cell matrix in the v7 fold header — r2 grok P1 +
     r3 cold-Opus P1):** service startup scans the watch namespace and re-arms each record: stat
     `target_path`, compare inode + size, re-hash `[turn_start_offset, observed_size)` against
     `digest` — fully computable from the record alone (r1 P1 fix). `status=watched`: present +
     validates → continue + **re-emit an idempotent `turn_finalized`** from the record's
     `event_ts`/`turn_started_ts` (fire-always normative; projection dedup makes duplicates benign);
     present + mismatch → retract; missing → retract (mid-horizon loss). **`status=closing` in EVERY
     cell → resume the close** (unlink if present → delete record), NEVER retract — `closing` proves
     the final revalidation already PASSED; any post-pass mutation is honest-bounds residual (b), not
     a retract trigger (r3: the asymmetric matrix could destroy a passed close). `status=retracted` →
     sticky: no re-emit, no re-arm, no re-finalize.
   - **Retraction triggers (reconciled, r1 cold-Opus):** the watch guards the byte range
     `[turn_start_offset, observed_size)`. Any mutation of that range — shrink below `observed_size`,
     inode swap, digest mismatch — retracts. Appends BEYOND `observed_size` are read and parsed:
     records belonging to the finalized turn retract it; records opening a LATER turn do not.
     Retraction emits **`turn_finality_retracted`**; projection sets `latency_ms NULL,
     outcome=clock_invalid, close_basis=none, finality_evidence=retracted`.
   - **Horizon end (Q3 CLOSED):** the watch closes at `min(target-inode deletion, finalized_at +
     T_horizon)` (env `ARB_TAIL_FINALITY_HORIZON_SECS`, default 900), and closing REQUIRES a **final
     revalidation**: re-stat + fd-quiescence re-check + digest re-verify over the recorded range.
     **Transient tool failures (lsof timeout/error/permission) DEFER the close to the next tick — they
     never retract** (r1 agy: a CPU spike must not convert into permanent metrics loss); only
     definitive state changes retract. **Pass → the closing sequence is (r2 grok P1): SET
     `status=closing` (durable) → unlink files → delete the watch record.** A crash after `closing` is
     self-healed on re-arm with the span kept `recovered`; a crash before `closing` retracts on re-arm
     as a genuine mid-horizon loss — the two crash windows are now distinguishable by the durable
     phase bit. **Retraction (r3 cold-Opus P2): writes `status=retracted` (7-day TTL marker) AND
     performs the same discovery-artifact teardown as a passing close** — unlink the sidecar/symlink
     (cold-only in v8; never the parent session's real transcript) — so startup re-nomination is
     structurally impossible for a retracted turn even after the marker expires. Belt: the projection
     durably refuses to re-open a turn whose row carries `finality_evidence=retracted` (the PG row
     outlives any Redis TTL). Fail (definitive) → retract, then close.
6. **Projection.** `turn_finalized` closes the open turn: latency = `event_ts − turn_started_ts` (O4
   arithmetic), `outcome=recovered`, `close_basis=turn_finalized`. **The refuse-reopen guard is an
   explicit SQL predicate on every `turn_finalized` application: `WHERE eval_turn.finality_evidence
   IS DISTINCT FROM 'retracted'`** (r4 cold-Opus: the durable belt must be visible in the statement,
   not implied). `recovered` stays distinct from `finished`; 5b renders it distinctly. The 5a-0
   capture flag is NEVER mutated (panel-verified clean).
7. **Canary (v6).** FOUR arms: (a) in-session backward append after finalization → retraction; (b)
   **finalize → RESTART the service → a SAME-SIZE in-range rewrite → assert the re-armed watch's digest
   re-verify retracts** (r1: the append variant missed the digest path); (c) finalize → horizon expiry
   → assert final revalidation ran and the watch closed clean (files gone, record gone, span still
   `recovered`); (d) **crash probe (r2): finalize → pass revalidation → kill the service between
   unlink and record delete → restart → assert self-heal (record gone, NO retraction, span still
   `recovered`)**. Pin the Claude Code version observed at gate time.

**Honest bounds (v5):** (a) between `turn_finalized` and retraction the row holds a number later
evidence can remove — unchanged, by design; 5b surfaces `recovered` distinctly. (b) **Post-horizon
floor:** after a PASSING final revalidation, the watch ends; a producer write after that point is
unobserved. The floor is now "never durably wrong within the horizon, and the horizon cannot close
without a passing revalidation" — a bounded, stated residual, not silence. (c) The same-user/same-host
precondition and the fd-visibility claim are preconditions + canary-tested, not asserted producer
behavior.

## Terminal-event model (dispatch producers — unchanged from v3)

`turn_completed` → `finished`. `turn_timeout` → `timeout`. `task_finished` closes only still-open
children, outcome from `ok` (`task_finish_derived` close_basis for pi_rpc/agy-print). Stall events never
close. claude-tail rows exempt from all dispatch heuristics.

## O-gate — the 5a live gate

Branches 1–5 unchanged from v3 (pure-text inversion; trace-only inversion; restart/idle straddle;
corrupt line; byte-0 re-read — each projects NULL/`clock_invalid`). Branch 6: terminal stop → NULL
absent O5 evidence; with the finalizer, `recovered` supported ONLY by `turn_finalized`; the THREE-arm
canary above. Epoch-fence live assertions (M2 crash path: O1 re-run latency, O2 single attempt, O3
stale predecessor inert — including the stale-INSERT probe: a late lower-epoch event for a deleted
ordinal must land NOTHING). Deny-proofs red-when-removed: remove the ledger stale-branch → O3 test
red; disable the quiescence check → canary red; disable watch re-arm → canary arm (b) red. One real
dispatch per engine family; purge boundary; span tables survive purge.

## Error handling / privileges (unchanged)

Dual-except; malformed → `span_deadletter`; same-transaction abort+redeliver; least-privilege grants.

## Testing (unit tier)

v3 list, plus: stale-INSERT probe (O3 branch skips inserts); equal-epoch redelivered `turn_started`
after `turn_completed` leaves completion intact; ledger self-establishment from a non-`task_started`
first event; O2 redelivery no-op; watch-before-emit ordering (watch write failure → no `turn_finalized`);
re-arm-on-start retraction; digest mismatch retraction; horizon-end revalidation (pass and fail arms);
idle-finish never nominates; absent-epoch uniform skip (incl. task events); F5 boundary on
`inserted_at`. v5 additions: finalization hold blocks deletion from nomination; abandon timeout
releases the hold and the turn stays NULL; same-size in-range rewrite after restart retracts (digest
path); transient lsof failure at horizon end defers, never retracts; teardown order
(files-then-record) under a crash between the two never re-finalizes; at-most-once earn + sticky
retract; crash-window re-emit iff record still validates; out-of-order start edge COALESCEs
`started_at` (no permanent NULL). v6 additions: re-arm policy matrix (the v7 normative table:
{present+validates, present+mismatch, missing} × {watched, closing, retracted} — nine cells, each asserted); startup re-nomination (completed sidecar +
no watch + no retracted marker → byte-0 re-scan → re-nominate; dirty re-scan → no nomination, stays
NULL); retracted-marker TTL blocks re-finalization;
rollup counters are projection-maintained increments (never edge-copied). v8 additions: the **flap
deny-proof** — a warm session that deregisters (draining record persisted) and re-registers (flap
supersede, `service.py:274-277`) must NEVER produce a `turn_finalized` at any point in that
lifecycle, including across a service restart inside the flap window; draining/deregistration paths
never enter O5 candidacy; every `turn_finalized` application carries the explicit
`finality_evidence IS DISTINCT FROM 'retracted'` predicate (delete the predicate → the
sticky-retract test goes red).

## Resolved questions (were v3's open Q1–Q4 + SP3)

- Q1 CLOSED: ledger self-establishing; dispatch order also guarantees epoch-before-`task_started`
  (`bridge.py:1291-1303`).
- Q2 CLOSED: absent-epoch → uniform skip + counter, task events included.
- Q3 CLOSED: horizon = min(target deletion, 900s default), close requires final revalidation.
- Q4 CLOSED: per-tick batched checks; fail-closed on lsof error/permission; same-user precondition named.
- SP3 (carried to impl): pg_cron availability vs host cron — design defaults to host cron + compose
  one-shots with the pinned 56d env contract.
