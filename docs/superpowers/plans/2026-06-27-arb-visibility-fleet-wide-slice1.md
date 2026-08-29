# Fleet-wide ARB Visibility — Slice 1 (seat-watch) — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

> **v2 (post plan-panel — agy/codex BLOCK, cold-Opus APPROVE-WITH-NITS):** Task 3 hardened (simple
> EventFlusher not TranscriptFlusher; sync-local/async-remote for byte-unchanged; non-blocking marker —
> the marker must NOT be a hot-path xadd either). Task 4 EXPLAIN→index-exists+functional. Task 5 adds schema
> USAGE. Task 6 scope-enforcement deferred. **Task 7 (seam-race) DROPPED** (pre-existing; eval lacks a dedup
> key; half-correct fix worse than deferring). Build = Tasks 1,2,3,4,5,6,8.

**Goal:** the CODE changes that let the built observability spine run fleet-wide on shared managed infra
(seat-watch: eval + transcript). Deployment is a separate operator runbook (end of doc), executed by Mark.

**Architecture:** activation + bounded builds on the existing Python spine. Mac seats tee live/eval/
transcript (async, off hot path, marked-not-silent) → shared Valkey; a central consumer (sole PG-cred
holder) drains → `arbmemory` PG (eval/transcript tables, role-isolated); one gateway (read-role) serves
`arb-watch-go`. Spec: `docs/superpowers/specs/2026-06-27-arb-visibility-fleet-wide-slice1-design.md` (v3).

**Tech Stack:** Python (bridge tees, consumers, gateway, grants); `arb-watch-go` (Go, unchanged this slice).
Tests: pytest against the local OrbStack container (`ARB_MEMORY_DSN=postgresql://arb_memory:$ARB_LOCAL_PG_PASSWORD@127.0.0.1:5544/arb_memory`).

## Global Constraints
- **Containment:** only the central consumer holds a PG cred; seats/hosts write Valkey only. No host gets a PG client.
- **No silent loss:** durable tee failure/queue-full → a `dropped` marker on `events:live` + a local counter. Never silent.
- **No synchronous WAN XADD on the hot path:** eval + live tees go through a background queue + flusher (mirror `TranscriptFlusher`).
- **Back-compat:** flag/config-off ⇒ byte-unchanged behavior (`live_redis` defaults to `self.redis`; tees default local).
- **Same db:** eval/transcript tables in `arbmemory`; role isolation via grants. `setup-schema` touches ONLY eval/transcript tables.
- **Reuse:** mirror `TranscriptFlusher` (`src/agent_redis_bridge/transcript_flusher.py`) for async tees; mirror `purge_expired` (`src/arb_memory/transcript.py:249-268`) for the eval purge; mirror `apply_eval_grants`/`apply_transcript_grants` (`src/arb_memory/mcp/grants.py`) for the gateway read-role.
- All pytest runs: `PYTHONPATH="$(pwd):$(pwd)/src" ARB_MEMORY_DSN=<local-container> uv run --extra arb-memory --with pytest pytest <files> -q`.

---

### Task 1: `setup-schema` mode (scoped, idempotent)
**Files:** Modify `src/arb_memory/run.py` (arg choices ~178-209, add handler); maybe `src/arb_memory/eval.py`/`transcript.py` (a `setup_schema(conn)` that creates ONLY their tables+indexes+deadletters). Test: `tests/arb_memory/test_setup_schema.py`.
**Produces:** `python -m arb_memory setup-schema` → creates `eval_event_raw`, `transcript_io`, `*_deadletter`, their indexes (incl. the new `eval_event_raw(inserted_at)` from Task 4) — idempotently, NOT touching `hints`/`artefacts`/`mcp_auth`.
- [ ] **Step 1 (failing test):** against an EMPTY scratch db, `setup_schema(conn)` then assert `to_regclass('public.eval_event_raw')` and `transcript_io` + deadletters are non-null, AND `to_regclass('public.artefacts')` IS null (memory tables untouched). Re-run → no error (idempotent).
- [ ] **Step 2:** run → FAIL (`setup-schema` not a choice / function absent).
- [ ] **Step 3:** implement: a `setup_schema(conn)` applying just the eval+transcript `CREATE TABLE IF NOT EXISTS` + indexes from `schema.sql` (do NOT `psql -f` the whole file); wire `setup-schema` into `run.py` choices.
- [ ] **Step 4:** run → PASS. **Step 5:** commit.

### Task 2: B1 — decouple `_tee_live_event` onto `self.live_redis`
**Files:** Modify `src/agent_redis_bridge/bridge.py` (ctor ~236-336 add `self.live_redis`; `_tee_live_event` ~1774). Test: `tests/test_bridge_live_redis.py`.
**Interfaces:** Produces `self.live_redis` (configurable via `resolve_*`/env, default `self.redis`); `_tee_live_event` writes `events:live` to `self.live_redis`.
- [ ] **Step 1 (failing test):** construct the bridge with a fake `live_redis` distinct from `self.redis`; drive `_tee_live_event(...)`; assert the `events:live` XADD landed on `live_redis`, not the control bus. Second test: default (no config) → `live_redis is self.redis` (byte-unchanged).
- [ ] **Step 2:** FAIL. **Step 3:** add `self.live_redis` (env-resolved, default `self.redis`); point `_tee_live_event`'s xadd at it. **Step 4:** PASS. **Step 5:** commit.

### Task 3: Async tees + drop-marking (the meaty one — panel-hardened)
**Files:** Create `src/agent_redis_bridge/event_flusher.py` (a SIMPLE discrete-event flusher); Modify
`src/agent_redis_bridge/bridge.py` (`_tee_eval_event` ~1753, `_tee_live_event` ~1774, ctor). Test:
`tests/test_async_tees_marking.py`.
**Interfaces:** Produces a small `EventFlusher` — a bounded `queue.Queue` + a thread that `XADD`s each
**discrete, complete** event (NO TranscriptFlusher chunk-reassembly / `_PendingItem` / turn-epoch buffering —
panel: eval/live events are not streamed deltas). Tees `put_nowait` onto it.
**Panel-mandated semantics (all three reviewers):**
- **Sync-when-local, async-when-remote (back-compat):** if the tee's redis is the default local `self.redis`,
  keep the existing **synchronous** `xadd` (fast, reliable, byte-unchanged for existing seats). Use the
  EventFlusher ONLY when a *remote* `live_redis`/`eval_redis` URL is configured. (agy P1 + opus: routing
  async unconditionally breaks "config-off ⇒ byte-unchanged" and degrades local durability.)
- **Marker is NON-BLOCKING (codex+opus P1):** on `queue.Full` (or flusher `xadd` failure), the `dropped`
  marker must NOT be a synchronous `xadd` on the calling thread — `put_nowait` it onto the flusher's marker
  path; if THAT is also full / sink down, increment a per-seat counter + log (the unemittable-marker
  fallback). The flusher emits queued `dropped` markers on `events:live` (`{seat, run, dropped_count}`).
- [ ] **Step 1 (failing tests):** (a) remote configured → a tee `put_nowait`s and the calling thread invokes
  **NO** `redis.xadd` (patch/capture). (b) default local → the tee writes synchronously (byte-unchanged).
  (c) queue-full → a `dropped` marker is eventually emitted by the FLUSHER, the calling thread does **NO**
  `xadd` **including the marker path**, the counter increments, no exception reaches the turn. (d) flusher
  `xadd` raises → marker queued/counter bumped, item not silently lost.
- [ ] **Step 2:** FAIL. **Step 3:** implement `EventFlusher` (simple) + the sync-local/async-remote branch +
  the non-blocking marker. Keep `_emit_vote`/audit OUT (deferred). **Step 4:** PASS. **Step 5:** commit.

### Task 4: Eval purge + `inserted_at` index
**Files:** Modify `src/arb_memory/eval.py` (add `purge_expired`); `src/arb_memory/schema.sql` (index `CREATE INDEX IF NOT EXISTS eval_event_raw_inserted_at_idx ON eval_event_raw (inserted_at)`); `src/arb_memory/run.py` if a purge CLI exists (mirror transcript-purge). Test: `tests/arb_memory/test_eval_purge.py`.
**Interfaces:** Produces `eval.purge_expired(conn, older_than_days, *, batch_size=10000) -> int` — ctid-LIMIT batched DELETE keyed on `inserted_at` (mirror `transcript.purge_expired:249-268`). Runs under owner cred.
- [ ] **Step 1 (failing test):** insert eval rows with `inserted_at` spanning old+new; `purge_expired(conn, 30)`; assert old rows gone, new kept, returned count correct (FUNCTIONAL). Separately assert the **index EXISTS** via `pg_indexes` (`eval_event_raw_inserted_at_idx`). Do NOT assert EXPLAIN-uses-index — flaky on small tables (panel: planner seq-scans tiny tables).
- [ ] **Step 2:** FAIL. **Step 3:** add the index to `schema.sql` AND ensure Task-1 `setup_schema` creates it (sequencing: setup_schema must include this index); implement `purge_expired` mirroring transcript's ctid-LIMIT keyed on `inserted_at`. **Step 4:** PASS. **Step 5:** commit.

### Task 5: `apply_visibility_grants` (gateway read-role)
**Files:** Modify `src/arb_memory/mcp/grants.py` (add `apply_visibility_grants`); `tests/arb_memory/test_visibility_grants.py`.
**Interfaces:** Produces `apply_visibility_grants(conn, role)` — **`GRANT USAGE ON SCHEMA public, mcp_auth`**
(panel P0: without schema USAGE the SELECT fails → auth breaks on connect), GRANT SELECT on `eval_event_raw`,
`transcript_io`, `mcp_auth.access_tokens`; REVOKE INSERT/UPDATE/DELETE on them; REVOKE ALL on `hints`/`artefacts`. (Mirror `apply_eval_grants:152-191`.)
- [ ] **Step 1 (failing test):** create a unique test role; `apply_visibility_grants`; assert `has_schema_privilege(role,'mcp_auth','USAGE')` = t, `has_table_privilege(role,'eval_event_raw','SELECT')` = t, `…'INSERT'` = f, `has_table_privilege(role,'artefacts','SELECT')` = f. (Deny-proven both directions.)
- [ ] **Step 2:** FAIL. **Step 3:** implement mirroring `apply_eval_grants`. **Step 4:** PASS. **Step 5:** commit.

### Task 6: SSE token — long-lived mint helper + periodic mid-stream re-validation
**Files:** Modify `src/arb_memory/visibility.py` (the SSE loops ~407/514 — re-validate the bearer token every N seconds; on invalid → close the stream); a mint helper (script or `oauth_store` call) for a far-future visibility-scoped token. Test: `tests/arb_memory/test_visibility_token_revalidate.py`.
**Interfaces:** Produces: the SSE generator re-checks `authenticated(token)` every `re_validate_s` (default e.g. 60); a revoked/expired token → stream closes within that interval. **Scope enforcement is OUT of slice 1** (panel P1): the gateway accepts any valid OAuth token; the **read-role DSN bounds what it can do** (least-privilege at the role layer). A visibility-`scope` check is a deferred follow-up — this task only fixes availability (long-lived token) + revocation (re-validation).
- [ ] **Step 1 (failing test):** open an SSE stream with a valid token; revoke it (or expire) mid-stream; assert the stream closes within the interval (not "runs forever on open TCP"). Plus: a long-lived (far-future) token authenticates at connect; an expired one 401s at connect.
- [ ] **Step 2:** FAIL. **Step 3:** add periodic re-validation to the SSE loops (bounded interval); document/mint the long-lived token. **Step 4:** PASS. **Step 5:** commit.

### Task 7: ~~Gateway backfill→tail seam-race fix~~ — DEFERRED out of slice 1 (panel)
**Dropped from this slice.** Panel (agy P0 / codex / opus P1): capturing stream end-IDs first closes the
*miss* window but opens a *duplicate* window, and **`eval_event_raw` backfill carries no `stream_entry_id`
to dedup on** — so "exactly once" isn't cheaply achievable. It is also a **pre-existing** gateway behavior
(NOT introduced by fleet-wide), affecting only a momentary reconnect-time watcher glitch (telemetry, not
evidence). Shipping a half-correct seam fix is worse than deferring. **Follow-up:** add `stream_entry_id` to
the eval backfill projection + an overlap-and-dedup tail, with a deterministic concurrent-drain test.

### Task 8: Test-hygiene — visibility-auth tests skip (not KeyError) without DSN
**Files:** Modify `tests/arb_memory/test_visibility_auth.py` (+ `_seat`/`_sse` if same). 
- [ ] **Step 1:** add a `pytest.mark.skipif`/guard so the 3 tests SKIP when `ARB_MEMORY_DSN` is unset (mirror `test_eval_consumer`'s skip). **Step 2:** verify they skip (not KeyError) with DSN unset, pass with DSN set. **Step 3:** commit.

---

## Local E2E (autonomous close-condition, after the tasks)
Run the full `arb_memory` + bridge suites against the local container; then the slice-specific E2E: a fake
seat tees eval+transcript+live (async-when-remote, off hot path) → local Valkey → consumer → local PG; the
gateway serves it; assert the spec's acceptance items that the BUILT tasks cover — the `dropped`-marker on a
forced failure (no hot-path xadd), eval purge via the `inserted_at` index, the read-role deny-proof (incl.
schema USAGE), and mid-stream revocation. (Seam no-miss/no-dup is deferred with Task 7.)

## Deployment runbook (OPERATOR — Mark, morning; NOT executed by the build)
1. Shared Valkey db on the managed cluster; note the URL (secrets to `~/.arb-*`, disk-only).
2. `python -m arb_memory setup-schema` against `arbmemory` (creates ONLY eval/transcript tables+indexes).
3. Apply eval/transcript **write-role** grants + **`apply_visibility_grants`** read-role; mint a long-lived visibility OAuth token.
4. Deploy on `arb-prod`: `EvalConsumer` + `TranscriptConsumer` (write/owner role) + the gateway (**read-role DSN**), pointed at shared Valkey + `arbmemory` PG. Add tunnel ingress `arb-visibility.example.com → :8810`.
5. Point the Mac seats' `live_redis`/`eval_redis`/`trace_redis` at the shared Valkey; restart seats.
6. Point `arb-watch-go` at `https://arb-visibility.example.com` + the token. Fleet E2E: drive a seat → it appears; durable rows in shared PG; kill a consumer → reclaim, no loss.

## Self-Review
- Spec coverage: §forks A (Task 1/5 same-db+roles), B1 (Task 2), C/C′ (Task 3), D (Task 4), E (Task 1), F (Task 6), test-hygiene (Task 8). Seam-race (spec §Code 7) DEFERRED with Task 7 (panel) — spec follow-up noted.
- Naming consistency: `setup_schema`, `live_redis`, `purge_expired`, `apply_visibility_grants`, `dropped` marker, `events:live`, `inserted_at` index — used consistently.
- Patterns reused (not reinvented): TranscriptFlusher, transcript.purge_expired, apply_eval_grants.
- External caveat: EXPLAIN-index assertions + the SSE revalidation interval are pinned against the actual installed code in each task's Step 1.
