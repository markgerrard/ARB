# ARB Observability — audit + eval/trace to prod, with the ARB Visibility seam — design

**Status:** design v3 — re-panel APPROVED (unanimous); 3 P2 nits folded; Slice 1 plan-ready
**Date:** 2026-06-25
**Author:** warm-Opus orchestrator (with Mark), synthesized from a 3-seat design panel (codex + agy + cold-Opus)

## Goal
Make **prod `arbmemory`** the durable **system-of-record for bridge observability**:
1. **Eval/trace** — capture seat execution traces (tool_call / turn / usage) for seat evaluation (quality + cost across models).
2. **Audit** — a queryable record of orchestration decisions (dispatch manifest, each seat's vote, the verdict), correlated by `run_id`.
3. **ARB Visibility (future; the capture must enable it now)** — a live view to watch seats execute in real time.

Both land in prod: Valkey **db-5 = audit bus**, **db-6 = eval**; PG = `arbmemory`. The **bridge runs on the Mac** and already uses the managed Valkey cluster.

## Context — what already exists (panel corrections to the original framing)
- **There is no separate db-12→db-6 tee to build.** The bridge already tees eval events: every task event
  flows through `push_task_event` → `build_eval_record` (**`bridge.py:93-106`** — emits `run_id`, `task_id`,
  `seat_id`, `event_type`, `sent_at`, `payload`; the allow-list lives in `eval_tee.py` `extract_eval_payload`/
  `EVAL_ALLOWLIST`) → `eval:events`, and the bridge honors `ARB_EVAL_REDIS_URL`/`ARB_EVAL_REDIS_DB`
  (`bridge.py:200,209`, `socket_timeout=0.5`). **But "eval→prod" is NOT pure deploy** — see Slice 1's
  concrete change-list (schema_version, env-file resolution, the dispatch gate, grants wiring).
- **The multi-emitter audit model is partly built:** `scripts/arb-audit-emit`, `agent-dispatch --audit-panel` → `arb-panel-vote` (`agent-dispatch:334,375`), and `panel_audit.reconcile()` (`panel_audit.py:81`) which refuses a verdict unless committed `vote` rows reconcile against the roster.
- `src/arb_memory/audit.py` — `audit_emit()` (`:52`), `AuditRun.emit()` (`:81`), `AuditConsumer` (`:236`), `seq` via Redis `INCR` (`:40`), `audit_events` idempotent on `UNIQUE(run_id, seq) ON CONFLICT DO NOTHING` (`:118`).
- `src/arb_memory/eval.py` — `EvalConsumer` → `eval_event_raw`/`eval_deadletter`; `run_eval()` (`run.py:49`) exists but **prod compose runs only `memory/audit/writer/mcp/cloudflared`** — no `eval` service yet. `DEFAULT_EVAL_DB=4` (`eval_config.py:7`).
- `src/agent_redis_bridge/envelope.py` — the request envelope **already carries `run_id`** (`:27`, validated `:85`, serialized `:113`) — the correlation carrier.
- `src/arb_memory/stance.py` — `parse_stance` (`:15`): a fixed-schema decoder (fenced/trailing JSON, `stance`/`severity` enums, `StanceError` on anything else). A loose **bare-`{` trailing fallback** (`:23-29`).

## Architecture
```
  Mac (local)                                            Prod (DO droplet + managed services)
  ┌─────────────────────────────────────┐               ┌───────────────────────────────────────┐
  │ orchestrator (Claude Code)           │  run_id       │ Valkey cluster                          │
  │  - mints run_id per panel run        │──manifest────▶│  db-5 arbmem:audit   db-6 eval:events   │
  │  - emits dispatch manifest + verdict │  + verdict    │     │                     │             │
  │  - dispatches seats w/ envelope.run_id│              │     ▼                     ▼             │
  │            │                          │              │  AuditConsumer        EvalConsumer       │
  │            ▼                          │              │     │                     │             │
  │ bridge daemon (choke point)          │              │     ▼                     ▼             │
  │  - lifecycle audit (start/finish/...) │──audit──────▶│  PG arbmemory: audit_events / eval_event_raw │
  │  - VOTE extraction (parse_stance)     │──vote───────▶│                                         │
  │  - eval tee (allowlist) ARB_EVAL_DB=6 │──eval───────▶│  ARB Visibility gateway (later):        │
  │  - db-12 task:<id>:events (live, TTL) │              │   XREAD db-5/db-6 (+db-12 live) by run_id│
  └─────────────────────────────────────┘               └───────────────────────────────────────┘
```
Trust/latency boundary: high-frequency raw telemetry stays on the Mac (db-12, short TTL); only **allow-listed** metadata crosses the WAN to prod (no raw model output / command output leaves the box). All cross-WAN emits are **fail-soft with short timeouts** so a slow/blocked prod write never hangs seat execution.

## Settled decisions (unanimous unless noted)
1. **`run_id` = the logical *run*, not the bridge task-id.** Orchestrator mints one per panel; every audit + eval row carries `run_id` + `task_id` (per-seat envelope id) + `seat_id` (bridge `agent_id`). Audit↔eval join on `run_id`. Propagated via the envelope.
2. **`run_id` is MANDATORY on panel dispatches + fail-loud.** Today a missing `run_id` makes eval **drop silently** (`bridge.py:93-106`) — a forgetful orchestrator gets *zero audit and zero eval with no error*. This is the top risk; close it.
3. **Eval = the smallest slice (but NOT pure deploy).** It needs a handful of real changes (schema_version,
   the dispatch gate, the env-file fix, grants wiring) — see Slice 1's concrete change-list — plus the deploy
   (`ARB_EVAL_REDIS_DB=6`, `eval` compose service) so rows land in `eval_event_raw`.
4. **Audit emitter split:** orchestrator owns the **manifest + verdict + `reconcile()`**; the **bridge** owns **mechanical lifecycle** (dispatch/start/finish/reply/timeout) and **vote extraction** (decision below). Lifecycle is auto-emitted, fail-soft.
5. **Vote extraction = bridge-daemon (option A), unanimous (cold-Opus flipped C→A on the code).** The bridge parses each seat's explicit stance block via `parse_stance` and emits the `vote` row — it's the one component that reliably sees every reply with its `run_id` + bus; it removes the wrapper's env/exit false-negative gap; and `parse_stance` is *transcription, not inference* (decorrelation is "who forms the judgment" — the seat — "not who transcribes it"). **Four guards (all must hold):**
   - **a.** Emit a vote **only** when the run is a *declared panel*: the request carries an explicit `audit_vote_expected` marker (and the dispatch manifest rosters this `task_id` as a voting seat) **and** `run_id`.
   - **b.** Require the **explicit fenced ` ```vote ` block** — NOT `parse_stance`'s loose bare-`{` fallback (`stance.py:23-29` is fine in-session, too loose for unattended bridge emission). The bridge path uses strict parsing.
   - **c.** `StanceError` / missing stance ⇒ **deadletter + `reconcile()` fails loud** — **never** synthesize a vote (no fabricated `abstain`/`approve`). Anti-laundering rule.
   - **d.** Extraction runs in **try/except, fail-soft** — a parse failure must never block reply delivery or crash the bridge worker (the parse sits near `send_reply`).
6. **ARB Visibility = Redis-stream-first.** The live UI `XREAD`s the streams directly (db-5 audit, db-6 eval, db-12 per-task live), through an SSE/WebSocket gateway (bolt onto the existing `writer` FastAPI app); PG `XRANGE` for mid-run backfill/replay. **Do now:** freeze a stable event schema — every event carries `run_id`, `task_id`, `seat_id`, `event_type`, `sent_at`, `schema_version` — so live-watch is cheap later. Don't build the UI; design the seam.
7. **Defer span tables** (`eval_turn`/`eval_tool_call`/`eval_io`, time-partitioned) until raw volume is observed — `eval_event_raw` (indexed by `run_id`/`task_id`, `schema.sql:77`) suffices; premature normalization freezes the wrong vocabulary.

## Slice roadmap
- **Slice 1 — Eval → prod (this spec's implementable unit). Concrete change-list** (the spec-review panel
  proved this is *not* pure deploy — each item verified against source):
  1. **`schema_version`** (P1, all 3 seats): add it end-to-end — default `"1"` in `eval_config.py`, write it in
     `build_eval_record` (`bridge.py:93`), parse it in `EvalConsumer._parse_event` (`eval.py:122`), and add a
     `schema_version text` column to `eval_event_raw` **and** `eval_deadletter` (`schema.sql:77`). Without this,
     Slice 1's own acceptance test cannot pass.
  2. **Dispatch-time run_id gate + panel marker** (P1): enforce at **dispatch**, not bridge — `agent-dispatch
     --panel`/`--audit-panel` **requires `--run-id`** (today it warns + exits 0) and stamps
     `payload.audit_vote_expected=true`. Require run_id on **panel/eval-emitting** dispatches only — do NOT
     reject every request lacking run_id (the bridge intentionally treats non-panel tasks as non-eval-tracked).
     **Update the two stale tests** that lock the old behavior (`test_agent_dispatch_audit_panel.py:19,29`).
  3. **Env-file resolution bug** (P1): `bridge.py:200` reads `ARB_EVAL_REDIS_URL` from `os.environ` only,
     ignoring the parsed `.env` (`env` dict) — fix to `os.environ.get(...) or env.get(...)`, or eval silently
     never tees when the var is in `.env` but unexported. **Apply the same fallback to `ARB_EVAL_REDIS_DB`
     (`bridge.py:204`) and `ARB_EVAL_PREFIX` (`:218`)** — else the DB silently stays on the db-4 default when
     set only in `.env`.
  4. **Grants deployment wiring** (P1, security boundary): `run_eval` connects as the **owner**; the
     `arbmemory-mcp` **eval-REVOKE** lives in `apply_mcp_grants` (`grants.py:48` — the per-role REVOKE;
     `apply_eval_grants` only revokes `PUBLIC`), applied out-of-band by tests only. Add a `grants` command to
     `run.py` (**exposed in the argparse `choices`** so `python -m arb_memory grants` runs) and **verify the
     REVOKE against the NAMED `arbmemory-mcp` role on prod** (deny-proof: that named role cannot `SELECT
     eval_event_raw`/`eval_deadletter`; owner can).
  5. **`ARB_EVAL_REDIS_DB=6` on BOTH** the prod bridge **and** the new `eval` compose service (default is db-4
     on both sides — flipping only the bridge leaves the consumer on db-4).
  6. **Eval telemetry is explicitly best-effort/sampled** — `eval_event_raw` is NOT authoritative cost data
     (the WAN emit is fail-soft, no at-least-once in Slice 1; a bounded local spool is a deferred enhancement).
  - **Done = a real `--panel` dispatch (with run_id) lands seat traces in prod `eval_event_raw` (joinable by
    run_id, carrying schema_version); a `--panel` dispatch *without* run_id fails loud at dispatch; the door
    role provably cannot read eval.**
- **Slice 2 — Bridge lifecycle audit:** auto-emit dispatch/start/finish/reply/timeout → db-5 `arbmem:audit`, fail-soft; lands in `audit_events`.
- **Slice 3 — Vote + verdict semantics:** bridge extracts the explicit stance → `vote` rows (guards a–d); orchestrator emits manifest/verdict; `reconcile()` gates the verdict (fail loud on roster gaps); unparseable → `audit_deadletter`.
- **Slice 4 — ARB Visibility gateway:** read-only SSE over the streams by `run_id` on the `writer` app (`XRANGE` backfill → `XREAD` live).
- **Slice 5 — Span tables + retention:** map raw → `eval_turn`/`eval_tool_call`/`eval_io`; partition/TTL once volume is measured.

## Error handling
- **Silent double-loss (the headline risk):** missing `run_id` ⇒ make it a loud reject at dispatch (Slice 1), not a dropped eval row.
- **Cross-WAN emit:** short socket timeouts + fail-soft (existing `0.5s`); a blocked prod write logs + drops *that telemetry row*, never hangs the seat. (Telemetry is best-effort; the *vote*'s loud-failure is enforced by `reconcile`, not by the emit.)
- **Out-of-order:** votes can land before the manifest; consumers idempotent via `UNIQUE(run_id, seq)`.
- **Vote integrity:** unparseable/missing stance ⇒ deadletter + reconcile fails loud; never a fabricated vote.
- **Allowlist:** only `EVAL_ALLOWLIST` keys cross the WAN; a leakage test asserts no raw strings escape.

## Testing (Slice 1)
- **Dispatch gate:** `--panel` without `--run-id` ⇒ **loud reject at dispatch** (deny-proof: remove the
  guard ⇒ it warns+exits 0 again, the old behavior). A non-panel task without run_id is still accepted
  (regression: don't break the intentional non-eval-tracked path, `test_push_task_event_tee.py:80`).
- **schema_version:** `build_eval_record` emits `schema_version`; the consumer parses it; the row in
  `eval_event_raw` carries it. (The two stale `test_agent_dispatch_audit_panel.py` assertions are updated.)
- **Env-file resolution:** `ARB_EVAL_REDIS_URL` set in the `.env` (unexported) ⇒ the bridge resolves it and
  tees (deny-proof: revert to `os.environ`-only ⇒ tee silently skipped).
- **Eval lands in prod:** a real `--panel` dispatch (with run_id) ⇒ rows in prod `eval_event_raw` keyed by
  `run_id`/`task_id`/`seat_id` (skip-if-no-prod-creds).
- **Door-role REVOKE (security):** `arbmemory-mcp` **cannot** `SELECT eval_event_raw`/`eval_deadletter`
  (deny-proof, both directions: owner can; door cannot).
- **Allowlist leakage:** assert no raw model/command output in the emitted event (`extract_eval_payload`).

## Out of scope (this spec / deferred)
- The Visibility **UI** (only the stream seam + SSE gateway are in scope, Slice 4).
- Span-table normalization (Slice 5).
- Dev-side audit/eval (this targets prod system-of-record; dev stays the sandbox).

## Open questions — resolved by the spec-review panel
1. **"Is this a panel dispatch"** → an explicit `payload.audit_vote_expected=true` marker stamped by
   `agent-dispatch --panel`; run_id required only on **panel/eval-emitting** dispatches, not all (the bridge
   keeps treating ordinary tasks without run_id as non-eval-tracked). Enforced at **dispatch**, not bridge.
2. **Eval consumer on prod** runs on the droplet (alongside audit/memory). It uses `eval_deadletter` from day 1
   (already in schema); retention/partitioning is deferred to Slice 5. Telemetry is **best-effort** (Slice 1
   adds no at-least-once; a bounded local spool is a later enhancement).
3. **Prod eval grants** → the owner (`arbmemory`) writes `eval_event_raw`; the **`arbmemory-mcp` door role must
   be REVOKED** from eval (security boundary). Slice 1 wires a `grants` deploy step + a deny-proof; this is a
   load-bearing Slice-1 item, not an open question.
