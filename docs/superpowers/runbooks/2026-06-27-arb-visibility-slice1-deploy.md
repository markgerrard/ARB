# ARB Visibility fleet-wide slice 1 — DEPLOY RUNBOOK (operator)

**For Mark, on prod. NOT run by the build session.** Prereq: branch `feat/arb-vis-slice1` merged to dev
(or deploy from it), and `arb-prod` on that code. Fill in the `<...>` values from your managed-infra secrets
(`~/.arb-*`, disk-only — never echo). Everything below is built + tri-reviewed + locally E2E-green.

This deploys the *seat-watch* plane (eval + transcript) fleet-wide. Audit/votes, the seam-race fix, and
cross-host fan-out beyond the Mac are deferred to follow-ups.

---

## 0. Decisions already locked (no re-litigation)
Same `arbmemory` db + role isolation · gateway+consumers on `arb-prod` behind a CF **tunnel** (NO CF Access)
· reuse ARB Memory OAuth · live tee decoupled to a shared Valkey (`ARB_LIVE_REDIS_URL`) · durable consumer
keeps the **sole PG cred** (hosts touch Valkey only) · drops are marked, never silent.

## 1. Provision shared Valkey db (managed cluster)
Pick a dedicated db number on the shared managed Valkey (memory uses db5/db3). Note the URL:
```
ARB_SHARED_VALKEY=rediss://<user>:<pw>@<managed-valkey-host>:<port>/<dedicated-db>
```

## 2. Apply schema to `arbmemory` (creates ONLY eval/transcript tables+indexes — does NOT touch memory tables)
On `arb-prod` (or any host with the prod DSN), as the **owner**:
```
ARB_MEMORY_DSN=<arbmemory-owner-dsn> python -m arb_memory setup-schema
```
Idempotent; safe to re-run. Verify `eval_event_raw` (+ `eval_event_raw_inserted_at_idx`), `transcript_io`,
and the deadletter tables exist; artefacts/hints untouched.

## 3. Grants — write-role for consumers, read-role for the gateway
As owner, against `arbmemory`:
```python
from arb_memory.mcp import grants
import psycopg
with psycopg.connect("<arbmemory-owner-dsn>") as c:
    grants.apply_eval_grants(c, "<eval_consumer_role>")          # SELECT/INSERT eval tables only
    grants.apply_transcript_grants(c, "<transcript_consumer_role>")
    grants.apply_visibility_grants(c, "<visibility_gateway_role>")  # SELECT-only read-role (deny-proven)
```
(Roles must pre-exist with login + password — provision like the local-read reader roles.)

## 4. Mint a long-lived, revocable visibility OAuth token
Use the Task-6 mint helper / `oauth_store` to issue a far-future-expiry token (revocable via `revoked_at`).
Store it disk-only; this is what `arb-watch-go` carries. Revoke = set `revoked_at` (cut within the
re-validation interval, ~60s).

## 5. Deploy consumers + gateway on `arb-prod`

**Compose-native (preferred — the stack already carries these services as of `feat/deploy-prep`):**
`deploy/docker-compose.yml` defines `eval`, `transcript`, and `visibility` services, each wired to its
own least-privilege role DSN env var (NOT the owner). Set in `deploy/.env`: `ARB_EVAL_DSN`,
`ARB_TRANSCRIPT_DSN`, `ARB_VISIBILITY_DSN` (+ `ARB_EVAL_REDIS_URL`, `ARB_TRACE_REDIS_URL`,
`ARB_BRIDGE_BUS_URL` — point the three Valkey URLs at `$ARB_SHARED_VALKEY` or per-plane dbs), then:
```
docker compose -f deploy/docker-compose.yml up -d --build eval transcript visibility
```
The vars are fail-loud: a service whose role DSN is unset will not silently fall back to owner.

**Manual (equivalent, if running outside compose):** three long-running units, all on `arb-prod`:
```
# EvalConsumer (write-role DSN + shared Valkey)
ARB_MEMORY_DSN=<eval-write-role-dsn> ARB_EVAL_REDIS_URL=$ARB_SHARED_VALKEY  python -m arb_memory eval
# TranscriptConsumer (write-role DSN + shared Valkey)
ARB_MEMORY_DSN=<transcript-write-role-dsn> ARB_TRACE_REDIS_URL=$ARB_SHARED_VALKEY  python -m arb_memory transcript
# Gateway (READ-ROLE DSN + bridge bus for live roster + PG for backfill)
ARB_MEMORY_DSN=<visibility-READ-role-dsn> ARB_BRIDGE_BUS_URL=$ARB_SHARED_VALKEY \
  ARB_VISIBILITY_PORT=8810  python -m arb_memory visibility
```
**The gateway MUST use the read-role DSN** (least-privilege), not the owner. Consumers use their write roles.
(Note: `run_visibility` reads `ARB_BRIDGE_BUS_URL` + `ARB_MEMORY_DSN` only — it does NOT read
`ARB_TRACE_REDIS_URL`; the gateway gets transcript via PG backfill + the bridge bus live tail.)

## 6. CF tunnel ingress (NO CF Access — pure data plane, Bearer-token auth)
Add to the existing `arb-memory-prod` tunnel (or a new one): hostname `arb-visibility.example.com` →
`http://localhost:8810`. Do **not** add a CF Access app (the Go watcher is a token client, no browser SSO).

## 7. Point the Mac fleet at shared Valkey (host 1)
On the seat launchers (the plists / env), set the tees to the shared Valkey and restart:
```
ARB_LIVE_REDIS_URL=$ARB_SHARED_VALKEY   # roster (events:live) -> shared, async flusher (off hot path)
ARB_EVAL_REDIS_URL=$ARB_SHARED_VALKEY    # eval traces -> shared
ARB_TRACE_REDIS_URL=$ARB_SHARED_VALKEY   # transcript -> shared
```
`bootout`+`bootstrap` the seats (plist env change). **Sanity:** with these UNSET the bridge is byte-unchanged
(local sync tees) — so you can roll back by unsetting + restarting.

## 8. Point `arb-watch-go` at the gateway
```
ARB_VISIBILITY_TOKEN=<long-lived-visibility-token> ./arb-watch-go --base https://arb-visibility.example.com
```

## 9. Fleet E2E (close-condition)
- Drive a known action on a Mac seat → it appears in `arb-watch-go` (roster + transcript `⏺`) through the tunnel.
- Confirm durable rows land in shared PG (`eval_event_raw`/`transcript_io`).
- Kill a consumer mid-drain → it reclaims (consumer-group), no loss; restart, backfill catches up.
- Revoke the token → the live watch is cut within ~60s (re-validation).

## Rollback
Unset `ARB_LIVE_REDIS_URL`/`ARB_EVAL_REDIS_URL`/`ARB_TRACE_REDIS_URL` on the seats + restart → bridge reverts
to local byte-unchanged behavior. Stop the `arb-prod` consumer/gateway units. The schema + grants are
additive (no rollback needed; harmless if unused).

## Deferred (own tracks, NOT this deploy)
Audit/votes + orchestrator dispatch-marking seam · cross-host fan-out beyond the Mac · Slice-5 analytical
spans · the backfill→tail seam-race exactly-once fix · web UI + CF Access · visibility-scope token enforcement.
