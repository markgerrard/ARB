# ARB Observability Slice 5a: span data layer + retention — Implementation Plan

**Status: PLAN v4 — CONVERGED** (plan panel r3 `panel-slice5a-plan-r3-20260715T004622Z-109538` unanimous approve/none; rounds r0–r3 closed `emitted`). Ready for implementation dispatch (codex-bridge-dev-luna @ high).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Project the durable `eval_event_raw` stream into three timing-span tables + a deadletter, honouring the five capture obligations 5a-0 handed forward (O1–O5) and the live gate (O-gate); add the O5 terminal-turn latency finalizer to the claude-tail service; wire nightly 56-day retention purges; and provision least-privilege grants for the new tables. Implements SPEC v3 (`docs/superpowers/specs/2026-07-15-arb-observability-slice5a-SPEC.md`) exactly — deliverables D1–D8, its semantics pins, unit test list, deny-proofs, and O-gate hooks. **5a owns the projection SQL and the producer-close handshake; it does NOT re-open 5a-0's capture code.**

**Architecture:** Additive. Span DDL lands in BOTH `src/arb_memory/schema.sql` and `src/arb_memory/run.py:setup_schema` (they are kept in lockstep — `schema.sql` feeds the `scratch` test fixture, `run.py` feeds the `setup-schema` deploy command; see Fixture strategy). The epoch-ledger-gated projection is an EXTENSION of `PostgresEvalSink.write` (`eval.py:23-42`) inside its existing `conn.transaction()` block — NOT a new sink (a second sink runs a second transaction, `eval.py:145`, and breaks atomicity). The O5 finalizer lives in the claude-tail `Service` (`service.py`) as a per-tick candidate machine over a durable Redis watch namespace (the `OffsetStore` pattern, `offset.py:58-67`). Three allowlist members are added (`eval_tee.py:10-20`) and the finalizer's synthetic emits are routed via `build_eval_record` (`bridge.py:152-167`) + the service's eval Redis (`_eval_redis_from_env`, `service.py:587-592`).

**Tech Stack:** Python 3.14, `psycopg` (sync), `redis` (sync client), pytest (`uv run --extra arb-memory pytest tests/...`). Live DB/Redis tests skip without `ARB_MEMORY_DSN` / `ARB_MEMORY_REDIS_URL` (conftest `scratch`/`redis_bus`). The `agent_redis_bridge.claude_tail` package (`service.py`, `tailer.py`, `offset.py`) and `scripts/claude_tail_hooks/` sidecar hooks.

## Global Constraints (every task's requirements implicitly include this section)

- **Test invocation:** `uv run --extra arb-memory pytest tests/...` (bare `pytest` → `ModuleNotFoundError: psycopg`). NEVER run the full suite; each task names its exact targeted invocation.
- **GREEN evidence from a CLEAN CHECKOUT of the committed SHA, not the authoring worktree** (5a-0 r2 rule): each task records its final GREEN + delete-to-red evidence from `git worktree add <tmp> <sha>`, so an un-staged file/fixture surfaces before merge.
- **Deny-proof discipline (REQUIRED):** a deny-proof is (1) a POSITIVE test that passes *because* a specific production guard exists, PLUS (2) an explicit **delete-to-red** step — physically mutate/delete that guard, re-run the SAME positive test, confirm RED, restore, record the red output ([[deny-proofs-need-adversarial-verification]]). No assert-the-defect tests. No vacuously-green guards ([[vacuously-green-guard-fail-loud]]).
- **Extract-only eval boundary is 5a-0's, not 5a's:** 5a adds exactly THREE allowlist scalars (D8); it does NOT touch capture emit semantics, `turn_clock_monotonic`, the 5a-0 scan lifecycle, or `EVAL_SCHEMA_VERSION` (additive ⇒ no bump).
- **`attempt_epoch` is a COLUMN, never key material** — UNIQUE keys carry no epoch dimension (O1/O2 replace, never accrete).
- **Cross-slice claims cite `file:line`** ([[cross-slice-claims-need-citation]]).
- **CHANGELOG discipline:** every deliverable folds a `CHANGELOG.md` entry (what AND why) into the task that ships it ([[changelog-discipline]]).
- **Deploy is gated:** the plan builds cron/compose/role artifacts and their tests; the actual prod application (schema one-shot, role creation, cron install, fleet redeploy, live gate) is the ORCHESTRATOR's job post-merge. Do NOT apply to prod.

---

## Plan-stage resolutions of the spec's "Open items" (implementation-order decisions)

- **Watch-store namespace + TTL.** Reuse the `OffsetStore` Redis client/JSON pattern (`offset.py:21-67`) via a new `FinalityWatchStore` in `claude_tail/finality.py`. Keys (v4 fold, terra r2 P1 — the SPEC pins `{path|target_inode}`, an inode alone is reusable after teardown): watch record `{prefix}claude:finality:watch:{target_path}|{target_inode}:{turn_index}` — **no TTL** (durable until the close/self-heal deletes it); retracted marker `{prefix}claude:finality:retracted:{target_path}|{target_inode}:{turn_index}` — **7-day TTL**. RED test added: an inode-reuse/cross-path collision (same inode number, different resolved path) must NOT match an existing watch or retracted marker (`ARB_TAIL_FINALITY_RETRACTED_TTL_SECS=604800`). Rationale: the live watch must outlive any Redis eviction window (it guards a byte range for up to the horizon); the retracted marker only needs to survive long enough that startup re-nomination can't resurrect a retracted turn before its discovery artifacts are unlinked (they are unlinked on retraction, D4.5, so the marker is belt).
- **DDL migration ordering vs grants.** `setup-schema` MUST run before `grants` in the deploy one-shot (`apply_eval_grants` references the new tables/sequences). Encoded as an ordered deploy-notes step in T-11; the grant function tolerates absent tables only via explicit `to_regclass` guards where a table may not yet exist — but the required order is schema→grants.
- **Cron vs compose one-shot shape (SP3).** **Host cron (default), NOT a compose service** — `test_compose_shape.py:14-24` asserts the EXACT service set `{memory,audit,audit-close-consumer,eval,transcript,mcp,cloudflared,writer,visibility}`; a new compose service breaks it and a purge one-shot needs no long-lived container. Follows the existing vault-export nightly-cron-over-`docker compose exec` pattern (compose comment, `docker-compose.yml:15-19`). T-11 ships `deploy/retention-purge.sh` + a systemd timer unit (prod droplet) / launchd plist fragment (dev host), both pinning `ARB_EVAL_RETENTION_DAYS=56` / `ARB_TRANSCRIPT_RETENTION_DAYS=56`.
- **Retention-role provisioning.** A dedicated `apply_retention_grants(conn, role)` in `grants.py`, wired into `run_grants` keyed on a new `ARB_RETENTION_ROLE` env. Role *creation* (`CREATE ROLE … LOGIN`) is deploy plumbing (orchestrator, T-11 notes); the grant function + its role-connected purge test are implementor-buildable.

---

## Fixture strategy (grounded in existing patterns)

- **DB projection/purge/grants tests** use the `scratch` fixture (`tests/arb_memory/conftest.py:78-99`): a throwaway schema loaded from `schema.sql`, so **the span DDL MUST be in `schema.sql`** or every projection test errors on a missing table. `conn_factory` (`conftest.py:102-124`) yields real autocommit conns on the same schema for the consumer. Eval-consumer driver idioms: `_xadd`/`_drain`/`_make` (`tests/arb_memory/test_eval_consumer.py:12-30`) — XADD a raw event, `_drain` the consumer, assert on `eval_event_raw` + the new span tables.
- **Role-connected purge/grants tests** follow `test_eval_grants.py`'s `_mcp_dsn`/`_has_priv` pattern (`tests/arb_memory/test_eval_grants.py:63-` and `:14-24`): CREATE the retention role, `apply_retention_grants`, then open a SECOND conn *as that role* and prove purge works AND that SELECT on span tables is denied. A purge that only works as owner FAILS D7.
- **claude-tail finalizer tests** use `tests/claude_tail/test_service.py` idioms: `FakeRedis` (`test_service.py:11-37`, get/set/delete/scan_iter — extend the local copy with the ex/ttl already present), `FakeTailer` (`:40-`), and `_write_json` sidecar writers. The watch store is driven against `FakeRedis`.
- **`lsof`/fd-quiescence MUST be injectable.** The finalizer takes a `fd_probe: Callable[[int], list[FdInfo]]` (default a real `lsof`/`/proc` implementation in `finality.py`); tests inject a fake returning a **configurable** fd list. **[[fixture-supplies-what-code-lacks]]: the fake returns raw fd observations (input the OS would supply); it MUST NOT return an "earned/not-earned" verdict** — the zero-write-open⇒earn decision is the code under test and must live in production, or the quiescence deny-proof is vacuous.
- **Digest/stat injection.** File stat + byte-range hashing run against real tmp files (`tmp_path`); the watch's re-stat/re-hash is exercised by mutating the real file between ticks (append/truncate/inode-swap via unlink+recreate), never by faking the hash.

---

## Tasks

### T-0 — D1: span table DDL (schema.sql + run.py parity)

- **Files:** `src/arb_memory/schema.sql` (after `transcript_deadletter`, ~line 175), `src/arb_memory/run.py` (`setup_schema`, extend the body ~line 193 before its close).
- **RED first:** in `tests/arb_memory/test_setup_schema.py` (or a new `test_span_schema.py`): assert each of `eval_turn`, `eval_tool_call`, `eval_task`, `span_deadletter` exists after `setup_schema`; assert each enum `CHECK` REJECTS an out-of-vocab `outcome` (INSERT raises); assert the UNIQUE keys are exactly `(run_id,task_id,tool_call_id)` / `(run_id,task_id,turn_index)` / `(run_id,task_id)` / `(stream_entry_id)` and carry **no `attempt_epoch`**; assert `span_deadletter` has a UNIQUE `stream_entry_id`. Add a parity test (mirror `test_schema.py:27-30`) that loading `schema.sql` alone creates all four tables.
- **GREEN:** add the four `CREATE TABLE IF NOT EXISTS` blocks (exact columns per SPEC D1, incl. `latency_basis ∈ {sent_at,event_ts}`, `finality_evidence text`, `inserted_at/updated_at timestamptz DEFAULT now()`), enum columns as `text` + `CHECK (outcome IN (…))`, and the UNIQUE constraints — in BOTH files identically. No indexes beyond the UNIQUE keys yet.
- **Done:** `uv run --extra arb-memory pytest tests/arb_memory/test_setup_schema.py tests/arb_memory/test_schema.py -q` green; enum-reject tests red if a `CHECK` is dropped.
- **NON-goals:** no projection logic; no grants; do not touch `eval_event_raw`/`transcript_io` DDL.

### T-1 — D2 core: epoch ledger + O1/O2/O3 gate + eval_turn start/finish edges (atomic)

- **Files:** `src/arb_memory/eval.py` — extend `PostgresEvalSink.write` INSIDE its `conn.transaction()` (`eval.py:25`); add a `project_spans(conn, event)` helper called after the raw INSERT within the same `with conn.transaction()`.
- **RED first** (`tests/arb_memory/test_span_projection.py`, driven via `_xadd`/`_drain`):
  - (a) ledger self-establishment from a non-`task_started` first event (a `turn_started` creates the `eval_task` ledger row).
  - (b) O3 stale-INSERT probe — a lower-epoch event after a higher epoch is established writes NOTHING (no ghost `eval_turn`/`eval_tool_call` row, INSERTs included).
  - (c) O2 supersede — a higher epoch DELETEs prior-attempt turn/tool rows and resets `eval_task` rollups.
  - (d) O2 redelivered-bump no-op (`stored == incoming` ⇒ no delete).
  - (e) equal-epoch out-of-order start COALESCEs `started_at` (no permanent NULL).
  - (f) redelivered `turn_started` after `turn_completed` leaves completion fields intact (finish edge is UPDATE-only, SETs only the normative completion columns).
  - (g) **atomicity** — an injected failure mid-projection (monkeypatch the span UPDATE to raise) aborts the raw INSERT too: neither `eval_event_raw` nor span rows land, and PEL redelivery replays both.
- **GREEN:** implement the D2 branch form (NORMATIVE, consumer-code branching, not `ON CONFLICT … WHERE`): ledger upsert-and-lock (`INSERT … ON CONFLICT (run_id,task_id) DO UPDATE SET attempt_epoch = GREATEST(…) RETURNING`); `incoming < stored` ⇒ return (no writes); `incoming > prior` ⇒ DELETE prior-attempt `eval_tool_call`/`eval_turn` + reset `eval_task`; start edge upsert `DO UPDATE SET started_at = COALESCE(<t>.started_at, EXCLUDED.started_at)` with belt `WHERE <t>.attempt_epoch <= EXCLUDED.attempt_epoch`; finish edge UPDATE-only SETting exactly the normative completion columns (D2.5).
- **Done:** `pytest tests/arb_memory/test_span_projection.py -q` green. Atomicity is a named deny-proof (T-12).
- **NON-goals:** no tool/task span bodies yet (stub them as no-ops or handle only `turn_started`/`turn_completed`); no deadletter; no D5 events.

### T-2 (tool edges are the `command_started`/`command_finished` event types — pinned, grok P2) — D2: tool-call + task spans, rollup counters, absent-epoch skip, claude-tail inertness

- **Files:** `src/arb_memory/eval.py` (`project_spans`).
- **RED first:**
  - tool-span start/finish pairing by `tool_call_id` writing `eval_tool_call`; `task_started`/`task_finished` writing `eval_task` start/finish.
  - **rollup counters are projection-maintained increments** — `eval_turn.tool_call_count`, `eval_task.turn_count/tool_call_count` increment as child spans close, and a redelivered finish edge does NOT double-count (dedup via the child span's UNIQUE-key transition to a closed outcome).
  - absent-`attempt_epoch` event of ANY type (incl. task lifecycle) skips span projection uniformly and bumps a `span_skipped_no_epoch` counter (assert counter + no rows + still acks).
  - claude-tail epoch-constant-`1` rows: O1–O3 are structurally inert (two events at epoch 1 never delete/skip each other).
- **GREEN:** implement tool/task projection + counter increments; the absent-epoch early-return-with-counter before any span write; assert-by-construction that epoch-1 never trips O2/O3.
- **Done:** `pytest tests/arb_memory/test_span_projection.py -q` green.
- **NON-goals:** no deadletter routing (T-3); rollups never copied from an edge payload (D2.8).

### T-3 — D2.9: deadletter paths + orphan scope pins + dual-except

- **Files:** `src/arb_memory/eval.py` (`project_spans` + a `deadletter_span(conn, entry, error)` INSERT into `span_deadletter`, same transaction as the raw insert).
- **RED first:**
  - orphan finish (zero-row UPDATE on a pair-keyed finish edge — `turn_completed`/tool finish/`turn_timeout`) INSERTs `span_deadletter`, creates NO partial row.
  - a tool/turn edge carrying `attempt_epoch` but MISSING its pairing id (`tool_call_id`/`turn_index`) deadletters; **NO FIFO pairing** under any missing-id condition.
  - deadletter INSERT is replay-idempotent (UNIQUE `stream_entry_id`, `ON CONFLICT DO NOTHING`).
  - **scope pins (three assertions):** (a) `task_finished` closing ZERO still-open children is a no-op, NOT a deadletter; (b1) a D5 `turn_finalized` zero-row UPDATE where the row EXISTS with `finality_evidence='retracted'` is a refuse-reopen no-op, NOT a deadletter; (b2) a `turn_finalized` zero-row UPDATE where the row is ABSENT IS an orphan ⇒ deadletter.
  - infra error (psycopg/redis) aborts the shared transaction ⇒ PEL redelivery (row count unchanged), NEVER deadletter; absent-epoch skips with the counter, NEVER deadletters.
- **GREEN:** implement the orphan detection (distinguish by row presence for the `turn_finalized` case), the dual-except discipline (infra → re-raise/retry; deterministic malformed → deadletter+ack), and the `span_deadletter` INSERT inside the shared transaction.
- **Done:** `pytest tests/arb_memory/test_span_projection.py tests/arb_memory/test_span_deadletter.py -q` green. Silent-drop is a named deny-proof (T-12).
- **NON-goals:** do not alter the existing `eval_deadletter` path (`eval.py:45-59`); span deadletter is a distinct table.

### T-4 — D3: O4 latency bases + terminal model

- **Files:** `src/arb_memory/eval.py` (`project_spans` close-edge arithmetic + `close_basis`/`outcome` derivation).
- **RED first:**
  - dispatch producer (no `event_ts`) ⇒ `latency_basis=sent_at`, turn latency = `turn_completed.sent_at − turn_started.sent_at`.
  - claude-tail (`event_ts` present) ⇒ `latency_basis=event_ts`, latency computed ONLY when the closing edge's `turn_clock_monotonic` is `true` (`event_ts − turn_started_ts`), else `latency_ms NULL` + `outcome=clock_invalid` — **never a `sent_at` fallback for claude-tail**; a `turn_started` with no `true` `turn_completed` stays `open`/NULL (treated as flag-false).
  - tool-span pairing by `tool_call_id`, each edge's own basis; `turn_completed`→`finished`, `turn_timeout`→`timeout`.
  - `task_finished` closes ONLY still-open children (a success does NOT mark children `incomplete`), `close_basis=task_finish_derived` for STILL-OPEN children on pi_rpc/agy-print recovery paths.
  - `stall_detected`/`stall_unknown` NEVER close anything; dispatch terminal heuristics NEVER touch claude-tail rows.
- **GREEN:** implement per-producer basis selection, the clock-validity gate, close-basis/outcome mapping, and the stall/no-op guards.
- **Done:** `pytest tests/arb_memory/test_span_projection.py -k "latency or terminal or clock or basis" -q` green. `clock_invalid`-NULL is a named deny-proof arm (O-gate).
- **NON-goals:** no `recovered` outcome yet (that is D5/turn_finalized, T-5); no token/cost rollups (out of scope).

### T-5 — D8 allowlist + D5 projection of `turn_finalized` / `turn_finality_retracted`

- **Files:** `src/agent_redis_bridge/eval_tee.py` (`EVAL_ALLOWLIST`, `:10-20`); `src/arb_memory/eval.py` (`project_spans`).
- **RED first:**
  - (allowlist) `extract_eval_payload` now carries `finality_evidence`/`observed_inode`/`observed_size` and still drops non-allowlisted keys; `EVAL_SCHEMA_VERSION` unchanged (`test_eval_tee.py`).
  - (projection) a `turn_finalized` event closes the open turn: `latency_ms = event_ts − turn_started_ts`, `outcome=recovered`, `close_basis=turn_finalized`, `finality_evidence='fd_quiescence'`.
  - **the refuse-reopen guard is an EXPLICIT SQL predicate `WHERE eval_turn.finality_evidence IS DISTINCT FROM 'retracted'` on every `turn_finalized` application** (assert the statement text carries it / assert a pre-retracted row is NOT reopened).
  - `turn_finality_retracted` sets `latency_ms NULL, outcome=clock_invalid, close_basis=none, finality_evidence='retracted'`.
  - both carry `attempt_epoch:1` and flow through the ledger inertly; `recovered` stays distinct from `finished`.
- **GREEN:** add the three allowlist members; add the two event types to `project_spans` as finish-class edges with the sticky-retract predicate.
- **Done:** `pytest tests/test_eval_tee.py tests/arb_memory/test_span_projection.py -k "finalized or retracted or recovered or allowlist" -q` green. Sticky-retract predicate is a named deny-proof (T-12).
- **NON-goals:** the finalizer that EMITS these events is T-6/7/8; here we only project them.

### T-6 — D4 finalizer part 1: watch store + hold + nomination

**(v3 fold, terra r1 P1 — `turn_start_offset` source.)** The digest range `[turn_start_offset,
final_size)` (T-7) and the watch record need the OPEN TURN's starting byte offset, which the tailer
does not track today: the turn-state snapshot excludes any byte offset (`tailer.py:102-105`) and
`_close_and_open_turn` receives none (`tailer.py:304`). T-6 therefore ALSO adds, in `tailer.py`:
record the opening `user` line's byte offset as **turn continuity state** (`_turn_start_offset`),
set where the turn opens (the poll loop knows each record's byte range), included in
`_snapshot_turn_state`/`_restore_turn_state`, exposed READ-ONLY to the finalizer. RED tests: offset
recorded on turn open; survives snapshot/restore; byte-0 recount re-establishes it. **Capture
emission semantics UNCHANGED** — this is continuity state exactly like `turn_index`, not a new
emitted field.

- **Files:** new `src/agent_redis_bridge/claude_tail/finality.py` (`FinalityWatchStore`, candidate state machine, `fd_probe` injection point); `src/agent_redis_bridge/claude_tail/service.py` (hold integration on `_delete_cold_seat_files` `:486-495`, fired from the `:197-201`/`:471-477` cold-completed path, **AND on the missing-spec tailer-removal path `:114-127`** — v2 fold, terra P1: `:197-201` only finishes and deletes files; actual `del self._tailers[key]` removal happens on the later missing-spec arm, and the hold test MUST cover that route too, else a hold protects the files while the tailer state is removed on the next tick).
- **RED first** (`tests/claude_tail/test_finality.py`, `FakeRedis`+`FakeTailer`+`tmp_path`):
  - a NOMINATED candidate's hold BLOCKS `_delete_cold_seat_files` + tailer removal from nomination onward.
  - `ARB_TAIL_FINALITY_ABANDON_SECS` (default 300) release drops the hold and the turn stays NULL (deletion proceeds).
  - **idle-finish NEVER nominates** (`service.py:202-207`); **draining/deregistration NEVER nominates** (`service.py:274-277` flap markers); warm-session terminal turns never nominate (descope).
  - nomination requires ALL of: `turn_started` emitted, no `turn_completed`, the tailer's OWN 5a-0 scan-continuity clean to a clean EOF (`_turn_clock_ok` etc., `tailer.py:310`), AND the cold-seat sidecar `completed:true` (`subagent_stop.py:24-25`).
- **GREEN:** implement `FinalityWatchStore` (namespace/TTL per plan-stage resolution), the candidate state enum (nominating→quiescing→confirming→watched), and the hold that gates the owned teardown surface; nomination predicate reads existing tailer state + sidecar, never re-scans capture.
- **Done:** `pytest tests/claude_tail/test_finality.py -k "nominat or hold or abandon or idle or draining" -q` green.
- **NON-goals:** no quiescence/digest/emit yet; do NOT mutate the 5a-0 capture flag or re-emit capture edges; do NOT touch `_cold_seat_completed` truth (`:471-477`).

### T-7 — D4 finalizer part 2: quiescence, digest, watch-before-emit, emit routing

- **Files:** `claude_tail/finality.py`; `claude_tail/service.py` (emit via `build_eval_record` + service eval Redis, `_eval_redis_from_env` `:587-592`).
- **RED first:**
  - quiescence resolves the `.output` symlink target `(path,inode)` (`subagent_start.py:38-41`), enumerates write-open fds via the injected `fd_probe`, earns ONLY on zero write-open fds; `lsof` error/timeout/permission ⇒ NO earn, stay NULL (fail-closed).
  - confirmation re-stats (same inode+size), re-verifies clean EOF, records `sha256` over `[turn_start_offset, final_size)`; growth ⇒ wait.
  - **watch-before-emit ordering** — the watch record is durably written (`status='watched'`) BEFORE the `turn_finalized` XADD; a watch-write failure ⇒ NO emit.
  - the emit is built via `build_eval_record` (`bridge.py:152-167`) with the finalized turn's EXPLICIT `turn_index` + `attempt_epoch:1` + `finality_evidence:'fd_quiescence'`/`observed_inode`/`observed_size`, XADD'd to the service's eval stream.
  - **at-most-once** — at most ONE successful `turn_finalized` per `(run_id,task_id,turn_index)`; `turn_finalized.event_ts` = the turn's LAST cleanly-parsed record timestamp.
- **GREEN:** implement quiescence (batched per tick, never per-poll), confirmation+digest, durable watch write, then emit via `build_eval_record`. **E2E RED (required)** in `tests/claude_tail/test_finality_e2e.py` (or extend the eval e2e): `turn_finalized` XADDs to the eval stream carrying `attempt_epoch=1` + the finalized turn's original `turn_index` and lands in `eval_event_raw` — RED without the routing change. (v2 fold, terra P1: the `turn_finality_retracted` e2e arm lives in T-8, which owns retraction emission — T-7 must not need T-8 machinery.)
- **Done:** `pytest tests/claude_tail/test_finality.py tests/claude_tail/test_finality_e2e.py -k "quiesc or digest or watch or emit or route" -q` green. Quiescence-disable and watch-write-failure are named deny-proofs (T-12).
- **NON-goals:** do NOT route via `Tailer._emit_eval`/`TURN_INDEXED_EVENTS` (the tailer stamps its own current `turn_index`, wrong for a synthetic emit — SPEC D8 forbids the routing-set option); no re-arm yet.

### T-8 — D4 finalizer part 3: re-arm matrix, retraction, horizon, startup re-nomination

(v2 fold: deliver as TWO commits — T-8a retraction/horizon/close lifecycle; T-8b re-arm matrix +
startup re-nomination — review load, grok/cold-Opus P2. **v3: T-8a's RED list explicitly includes
the retraction e2e arm**: `turn_finality_retracted` XADDs to the eval stream with `attempt_epoch=1`
+ the turn's original `turn_index` and lands in `eval_event_raw` — the arm moved from T-7 lives HERE
as a named test, not prose. Each commit's RED tests: T-8a = retraction triggers + horizon lifecycle
+ retraction e2e; T-8b = nine-cell matrix + startup re-nomination + flap deny-proof.)

- **Files:** `claude_tail/finality.py`; `claude_tail/service.py` (startup scan hooked BEFORE any teardown-capable tick).
- **RED first:**
  - retraction triggers — in-range shrink below `observed_size`, inode swap, digest mismatch ⇒ `turn_finality_retracted`; appends BEYOND `observed_size` are read+parsed (records of the finalized turn retract; records opening a LATER turn do not).
  - horizon close at `min(inode deletion, finalized_at + ARB_TAIL_FINALITY_HORIZON_SECS=900)` requires a final revalidation (re-stat + fd re-check + digest re-verify); **transient lsof failure at horizon DEFERS, never retracts**.
  - pass ⇒ sequence `status=closing` (durable) → unlink → delete record; retraction ⇒ `status=retracted` (7-day marker) AND the same discovery-artifact teardown (unlink sidecar/symlink — cold-only, NEVER the parent's real transcript).
  - **startup re-nomination (cold specs only)** — completed sidecar + NO watch + NO retracted marker ⇒ forced byte-0 continuous observation IGNORING the persisted offset, candidacy-evidence ONLY (never re-emits edges, never commits offsets `offset.py:58-67`), clean⇒re-nominate / dirty⇒NULL.
  - **the NINE-cell re-arm matrix — every cell asserted** ({present+validates, present+mismatch, missing} × {watched, closing, retracted}); `status=closing` resumes the close in EVERY cell (never retract); crash-between-unlink-and-record ⇒ self-heal (record gone, NO retraction, span stays `recovered`).
- **GREEN:** implement the startup scan (re-arm + re-nomination, ordered before teardown ticks), the retraction/horizon/close lifecycle, and the nine-cell dispatch.
- **Done:** `pytest tests/claude_tail/test_finality.py -k "rearm or retract or horizon or startup or nine or selfheal or sticky" -q` green. Re-arm-disable and sticky-retract are named deny-proofs (T-12).
- **NON-goals:** do NOT retract on transient tool failures; the FLAP deny-proof (warm deregister→draining→re-register never finalizes, even across restart) is asserted here and consolidated in T-12.

### T-9 — D6: retention F5 fix + transcript index + purge windows

(v3 pin, grok r0 P2: the 56-day values are set by the T-11 cron/timer artifacts — `run.py` code
defaults stay 30; T-9 tests the boundary behavior against an explicit env, it does NOT change the
code default.)

- **Files:** `src/arb_memory/transcript.py` (`purge_expired`, `:225-245`), `src/arb_memory/run.py` + `src/arb_memory/schema.sql` (index).
- **RED first** (`tests/arb_memory/test_transcript_purge.py`, mirror `test_eval_purge.py`): purge deletes rows past retention on **`inserted_at`, NOT on `ts`** (insert a row with old `ts` but recent `inserted_at` → survives; old `inserted_at` but recent `ts` → purged); `transcript_io_inserted_at_idx` EXISTS after `setup_schema`; span rows survive a raw `eval_event_raw` purge (span tables are not purged by `eval-purge`); the cron unit's env defaults resolve to 56 (assert `run_eval_purge`/`run_transcript_purge` read `ARB_*_RETENTION_DAYS` with the cron-set 56, code default remains 30).
- **GREEN:** change `transcript.py`'s purge `WHERE ts < …` → `WHERE inserted_at < …` (`:234-235`); add `CREATE INDEX IF NOT EXISTS transcript_io_inserted_at_idx ON transcript_io (inserted_at)` to BOTH `run.py:setup_schema` and `schema.sql`.
- **Done:** `pytest tests/arb_memory/test_transcript_purge.py tests/arb_memory/test_eval_purge.py -q` green. F5-boundary is a named deny-proof.
- **NON-goals:** do NOT change the eval purge (already `inserted_at`); no span-table purge job in this slice (span rows survive; a future span purge would use the same 56d window on the span's own `inserted_at`).

### T-10 — D7: grants (extend eval-consumer + new retention role)

- **Files:** `src/arb_memory/mcp/grants.py` (`apply_eval_grants` `:233-272`; new `apply_retention_grants`); `src/arb_memory/run.py` (`run_grants` wiring `:219-262`).
- **RED first** (`tests/arb_memory/test_eval_grants.py` + new `test_retention_grants.py`, `_has_priv` pattern): the eval-consumer role now has SELECT+INSERT+UPDATE+DELETE on `eval_turn`/`eval_tool_call`/`eval_task` (ledger lock/read+COALESCE need SELECT, O2 supersede needs DELETE), INSERT on `span_deadletter`, USAGE on the new sequences; the local-reader + MCP roles get NO grant on the span tables (inherit the raw-eval REVOKE posture); the retention role has EXACTLY `DELETE` + column-level `SELECT (inserted_at)` on `eval_event_raw` and `transcript_io`, and NO access to span tables — **and a role-connected purge test (second conn AS the retention role) actually deletes**; if PG rejects the `ctid` subquery under column-level SELECT, escalate to table-level SELECT on the two raw tables ONLY and record it in deploy notes.
- **GREEN:** extend `apply_eval_grants`; add `apply_retention_grants`; wire both in `run_grants` (`ARB_RETENTION_ROLE`).
- **Done:** `pytest tests/arb_memory/test_eval_grants.py tests/arb_memory/test_retention_grants.py tests/arb_memory/test_run_grants.py -q` green.
- **NON-goals:** do NOT grant span-table read to MCP/local-reader/visibility roles (5b's future read role is out of scope).

### T-11 — Deploy/ops artifacts (implementor-buildable; orchestrator applies)

- **Files:** `deploy/retention-purge.sh` (invokes `docker compose exec` the `eval`/`memory` service running `python -m arb_memory eval-purge` and `transcript-purge` with `ARB_EVAL_RETENTION_DAYS=56`/`ARB_TRANSCRIPT_RETENTION_DAYS=56` and the retention-role DSN); `deploy/systemd/arb-retention.service` + `.timer` (prod droplet, nightly); a launchd plist fragment for the dev host; deploy-notes section (schema→grants ordering; `CREATE ROLE ARB_RETENTION_ROLE LOGIN` + password/DSN wiring; the ONE-active-`EvalConsumer`-per-group pin).
- **RED first:** a shape test (mirror `test_compose_shape.py`) asserting the timer schedule + that the script pins BOTH retention envs to 56 and connects as the retention role, not owner; assert `docker-compose.yml`'s service SET is UNCHANGED (no new compose service).
- **GREEN:** author the script + units + notes.
- **Done:** `pytest tests/arb_memory/test_compose_shape.py tests/arb_memory/test_retention_deploy_shape.py -q` green.
- **NON-goals:** do NOT apply to prod, do NOT create the role live, do NOT install cron — orchestrator gate.

### T-12 — Consolidated deny-proof + integration checklist + CHANGELOG

- **Files:** test files above; `CHANGELOG.md`.
- **Deny-proofs (each: positive test + delete-to-red, record red output):**
  1. Remove the O3 stale-branch return → the O3/stale-INSERT test (T-1b) reds.
  2. Silent-drop: replace the D2.9 deadletter INSERT with ack-and-drop or a finish-INSERT arm → the orphan-finish test (T-3) reds.
  3. Project OUTSIDE `PostgresEvalSink.write`'s transaction (move `project_spans` after the `with` block) → the atomicity rollback test (T-1g) reds.
  4. Disable the quiescence zero-fd check (earn regardless of fds) → the O5 quiescence canary (T-7) reds.
  5. Disable watch re-arm (skip the startup scan) → the restart+same-size-in-range-rewrite canary (T-8) reds.
  6. Delete the `finality_evidence IS DISTINCT FROM 'retracted'` predicate → the sticky-retract test (T-5) reds.
  7. Flap deny-proof: a `deregister→draining→re-register` warm session (`service.py:118-124`+`:274-277`) NEVER produces a `turn_finalized` at any point, including across a service restart inside the flap window (T-8) reds if nomination is loosened to accept draining markers.
- **Integration/e2e tier:** the atomicity rollback (unit-fakeable, T-1g); the emit-routing e2e (T-7, RED without the routing change); O-gate branches — the six NULL/`clock_invalid` branches + four-arm canary + epoch-fence are **live-gate-only** (orchestrator, post-merge); this plan builds the fixtures/hooks (injectable `fd_probe`, watch store, digest range) they need. **Deploy pin surfaced:** exactly ONE active `EvalConsumer` per consumer group (ordering assumptions rest on single-group append order).
- **Done:** every deny-proof's red output recorded; `CHANGELOG.md` carries D1–D8 entries (what AND why); a clean-checkout run of the full targeted set is green.
- **NON-goals:** the live O-gate run itself (orchestrator).

---

## Commit discipline

Per-task commits, test-first, each message stating: the RED test(s) written first, the minimal impl, the exact targeted `pytest` invocation and its GREEN result from a **clean checkout of the committed SHA**, and (for deny-proof tasks) the delete-to-red evidence. Never run or gate on the full suite. Keep each task an independently reviewable commit; the suite stays green at every task boundary (T-5's D5 projection tolerates absent finalizer emits; T-6/7/8 build the emitter incrementally without regressing T-1–T-5).

## Explicit slice-wide NON-goals

5a-0 capture semantics (`tailer.py` scan lifecycle, `turn_clock_monotonic`, `event_ts` earning); the M2 owner-fenced recovery paths (`e2d16b0`); bridge dispatch code beyond `build_eval_record` REUSE; the 5b dashboard; token/cost rollups; table partitioning; historical absent-`attempt_epoch` rows; warm-session terminal-turn O5 recovery (descoped — projects NULL/`open`).

## Plan-fixture-smoke pre-flight (REQUIRED — this plan is fake-based)

Per [[plan-fixture-smoke-preflight]], run before dispatch:
`env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=src .venv/bin/python scripts/plan-fixture-smoke docs/superpowers/plans/2026-07-15-arb-observability-slice5a.md` — expect exit 0.

```python fixture-smoke
# (a) The eval extract boundary is PRODUCTION's extract_eval_payload, not a fake's. On the CURRENT
# (pre-impl) tree the three O5 fields are NOT allowlisted — proving T-5's allowlist test is a real
# red->green, and no fixture pre-supplies the field.
from agent_redis_bridge.eval_tee import extract_eval_payload
out = extract_eval_payload({"finality_evidence": "fd_quiescence", "observed_inode": 5, "observed_size": 10})
assert out == {}, f"O5 fields must NOT pass the allowlist pre-impl: {out}"
# Existing 5a-0 fields still pass (the boundary itself works).
assert extract_eval_payload({"attempt_epoch": 1}) == {"attempt_epoch": 1}
```

```python fixture-smoke
# (b) The finality module does not exist pre-impl — T-6's watch-store tests are a real red, not a
# fake pre-supplying the store.
red_claim(
    """
def test_finality_watch_store_precheck():
    from agent_redis_bridge.claude_tail.finality import FinalityWatchStore  # noqa: F401
""",
    expect_fail=["test_finality_watch_store_precheck"],
)
```

```python fixture-smoke
# (c) The tailer does not track the open turn's starting byte offset pre-impl (r1 terra P1 fold) —
# T-6's continuity-state test is a real red->green on the production tailer, not a fixture field.
from agent_redis_bridge.claude_tail.tailer import TranscriptTailer
fields = getattr(TranscriptTailer, "_TURN_STATE_FIELDS", ())
assert not any("start_offset" in f for f in fields), f"_turn_start_offset must NOT exist pre-impl: {fields}"
```
