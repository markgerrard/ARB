# Fleet-wide ARB Visibility — Slice 1 (seat-watch) — design spec (v3)

**Status:** v3. v1 design-panel: 3/3 BLOCK (forks) → v2 resolved them. v2 spec-panel: cold-Opus
APPROVE-WITH-NITS (no P0 — forks verified fixed), codex/agy BLOCK on **under-specified builds** (not design
flaws). v3 tightens the bounded builds: async tees + drop-marker channel, eval purge (mirrors the existing
transcript purge) + `inserted_at` index, `setup-schema` scoping, `apply_visibility_grants` read-role,
periodic token re-validation, seam-race fix. Foundation: the verified build-state audit (2026-06-27).

**Goal:** one `arb-watch-go` watcher showing the whole fleet's **live + historical seat activity**, fed by
shared managed infra. Slice 1 = **seat-watch only** (eval + transcript, as the watcher's *telemetry detail*).
No audit/votes/grading, no web UI, no orchestrator changes.

## Resolved forks (the panel exposed these; decided with Mark)
- **A — Store:** **same `arbmemory` db, separate tables, ROLE isolation** (NOT a dedicated `arb_eval` db).
  The gateway uses ONE DSN for both OAuth (`mcp_auth.access_tokens`) and backfill (`eval_event_raw`/
  `transcript_io`) — a separate db would break that (panel P0). Isolation moves to the role layer: a
  write-role granted ONLY on the eval/transcript tables + deadletters (`apply_eval_grants`), a read-role
  for the gateway. Volume handled by retention, not db separation.
- **B — Roster (B1):** **decouple `_tee_live_event` onto a configurable `live_redis`** (today hardcoded to
  the control bus `self.redis`, `bridge.py:1774`; the eval/audit/trace tees are already configurable).
  Point `live_redis` at the **shared managed Valkey** → fleet roster converges there; the gateway reads the
  roster from shared Valkey. **The control bus stays local — the dispatch plane never crosses the WAN.**
- **C — Durable WAN reliability:** **slice 1 = C2-with-marking.** Slice-1 data is *telemetry, not graded
  evidence* (audit/votes deferred), so the durable tees may fail-soft over the WAN — but a failed durable
  `XADD` MUST be **marked**, never silent. **Marker channel (panel P1 — can't mark to the failed stream):**
  on durable-tee `XADD` failure, increment a per-seat local failure counter AND emit a `dropped` marker
  event on the **live stream** (`events:live`, the channel `arb-watch-go` already tails) carrying
  `{seat, run, dropped_count}` → the watcher renders a *visible gap*. If the live stream is also down, the
  local counter + log is the fallback (the seat is plainly offline then anyway). The **central consumer
  keeps the only PG cred**; hosts touch only Valkey (containment preserved). **Slice-2 = C1**; **C3 REJECTED**
  (would hand every node a PG write client).
- **C′ — Async tees (panel P1):** the eval + live tees are currently **synchronous `XADD`s**
  (`bridge.py:1753-1772`); over a *remote* Valkey that is WAN latency on the turn hot path. **Route eval +
  live tees through a bounded background queue + flusher** (mirror the existing `TranscriptFlusher`), so the
  remote write is off the hot path; queue-full → drop-with-marker (per C). Transcript already does this.

## Global Constraints
- **Go/Python boundary:** gateway + consumers + bridge tees stay **Python**; `arb-watch-go` stays **Go**.
- **Reuse ARB Memory OAuth** for the gateway Bearer token (`oauth_store`). No CF Access (Go watcher is pure
  data-plane); gateway reachable via a CF **tunnel** (transport) + Bearer auth.
- **Structural containment:** only the central consumer holds a PG cred; hosts/seats write Valkey only.
- **No silent loss:** live = fail-soft *marked*; durable consumer = no-silent-drop (deadletters exist).
- **No orchestrator changes** (eval/transcript tees fire on config; audit/votes seam is out).

## Topology (slice 1: one host)
```
Mac dev seats ─┐ tees (fail-soft, off hot path; failed durable XADD → MARKED)
 (host 1)       │   live (events:live)  → live_redis ─────────► shared managed VALKEY (1 db, distinct keys)
                │   transcript          → trace_redis ─────────►        │  (roster + transcript live)
                │   eval                → eval_redis  ─────────►        │
               ─┘                                                       │
arb-prod droplet:  TranscriptConsumer + EvalConsumer ─drain─► shared managed PG: `arbmemory`, eval/transcript
                                                              tables (role-isolated)  ▲ (ONLY PG-cred holder)
                   Visibility gateway ─read backfill(PG) + live(Valkey)──────────────┘
                        │  CF tunnel: arb-visibility.example.com → :8810 (NO CF Access), long-lived visibility OAuth token
Go arb-watch-go (Mac) ──Bearer token──► gateway  ⇒ whole-fleet view
```

## Components & changes
### Code (built + locally-testable now)
1. **`bridge.py` — B1 live-tee decouple:** add `self.live_redis` (configurable, default `self.redis` for
   back-compat); `_tee_live_event` writes `events:live` to `live_redis`. Config points it at shared Valkey.
2. **`bridge.py` — C/C′ async tees + drop-marking:** route the eval + live tees through a bounded queue +
   flusher (mirror `TranscriptFlusher`); on `XADD` failure or queue-full, emit the `dropped` marker on
   `events:live` + bump a local counter (per §C). Never a silent drop; never a synchronous WAN `XADD`.
3. **Schema-provision (E):** a `python -m arb_memory setup-schema` mode that **idempotently creates ONLY the
   eval/transcript tables + their indexes + deadletters** (NOT the memory tables — do not `psql -f` the whole
   `schema.sql` on a shared db). Consumer entrypoints don't create schema and crash on an empty db (panel).
   Test: setup-schema on an empty db → consumer runs; re-run is a no-op.
4. **Retention/purge (D):** transcript purge **already exists** (`transcript.py:249-268`, batched ctid-LIMIT).
   **Build the eval purge mirroring it** for `eval_event_raw`, keyed on **`inserted_at`** (NOT `created_at`);
   **add an index on `eval_event_raw(inserted_at)`** so the purge doesn't seq-scan (panel: existing indexes
   are `run_id`/`task_id` only). Default 30-day, env-tunable; purge runs under the **owner cred**, not the
   consumer write-role.
5. **Gateway read-role (new, panel P2):** add `apply_visibility_grants` — a SELECT-only gateway role on
   `eval_event_raw` + `transcript_io` + `mcp_auth.access_tokens` (for auth), **no INSERT/UPDATE/DELETE, no
   `hints`/`artefacts`**. The deployed gateway uses this read-role DSN, not the owner DSN.
6. **SSE auth (F):** mint a **long-lived, revocable, visibility-scoped OAuth token** (far-future `expires_at`)
   so reconnects don't 401 after the 1h default — AND **promote periodic mid-stream re-validation** (panel
   P1: a far-future token otherwise can't be revoked mid-stream) so a revoked token cuts an in-flight SSE
   within a bounded interval.
7. **Gateway seam-race fix (panel P1):** capture the live/trace **stream end-IDs BEFORE** the DB backfill
   query, then tail from those IDs — so events in the backfill→tail gap are neither missed nor duplicated.
8. **Test-hygiene:** the 3 visibility-auth tests skip (not `KeyError`) when `ARB_MEMORY_DSN` is unset.

### Deployment (operator, morning — Mark's hands on prod)
9. Provision a **shared Valkey db**; run `setup-schema` on the `arbmemory` cluster; apply the eval/transcript
   **write-role** grants + **`apply_visibility_grants`** read-role; mint a long-lived visibility OAuth token.
   Add tunnel ingress `arb-visibility.example.com → :8810`.
10. Deploy `TranscriptConsumer` + `EvalConsumer` (owner-or-write-role) + the gateway (**read-role DSN**) on
    `arb-prod`, pointed at shared Valkey + `arbmemory` PG. Point the Mac seats' `live_redis`/`eval_redis`/
    `trace_redis` at the shared Valkey.
11. Point `arb-watch-go` at `https://arb-visibility.example.com` with the visibility token.

## Acceptance / close-conditions
- **Local E2E (autonomous, tonight):** against the local container — (a) `setup-schema` idempotently creates
  ONLY the eval/transcript tables+indexes+deadletters on an EMPTY db (memory tables untouched); consumer
  runs (no crash); re-run is a no-op. (b) B1: `live_redis` configurable, `events:live` lands where
  configured, default byte-unchanged. (c) C/C′: tees go through the flusher (off hot path — no synchronous
  WAN `XADD`); a forced durable-tee failure emits a `dropped` marker on `events:live` + bumps the counter,
  not silent. (d) eval purge deletes rows older than the window (count-verified) using the `inserted_at`
  index (no seq-scan). (e) `apply_visibility_grants` read-role can SELECT eval/transcript but **cannot**
  INSERT/UPDATE/DELETE nor read `hints`/`artefacts` (deny-proven). (f) token: long-lived authenticates,
  expired 401s, and a revoked token is cut mid-stream within the re-validation interval. (g) seam: no event
  missed/duplicated across backfill→tail. (h) consumer count-in=count-out (run-scoped) + consumer-death reclaim.
- **Fleet E2E (morning, post-deploy):** drive a Mac seat → roster + transcript appear in `arb-watch-go`
  through the tunnel; durable rows in shared PG; kill a consumer → reclaim, no loss.

## Out of scope (deferred)
Audit/votes + orchestrator dispatch-marking seam; C1 spool+forward (slice 2, evidence); cross-host fan-out
beyond host 1 (config repetition); Slice-5 analytical spans; web UI + CF Access; mid-stream token re-validation.

## Risks
- Live roster over WAN: fail-soft, self-healing (a dropped roster event re-establishes on the next) —
  acceptable, marked.
- Droplet load: 2 consumers + gateway co-located with the memory door — modest; split later if needed.
- Schema-provision on a shared db: `setup-schema` must create ONLY the eval/transcript tables (not touch
  memory tables) and be idempotent.
