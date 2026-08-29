# ARB audit/votes grading plane — DEPLOY / ACTIVATION RUNBOOK (operator)

**For Mark, on prod + the Mac bridge. NOT run by the build session.** Prereq: dev (`579e164`) on `arb-prod`
and on the Mac bridge clone. Fill `<...>` from your managed-infra secrets (disk-only, never echo).

This activates the **audit/votes grading plane**: every panel run records its dispatch manifest, each seat's
vote, and the reconcile-gated verdict into `audit_events` on prod `arbmemory` — gradable, durable, and
anti-laundering (a verdict that doesn't match the recorded votes is REFUSED). The
spine and its supervised consumers are built and merged; this runbook activates them.

---

## 0. What's already in place (no build needed)
- **Schema:** `audit_events` + `audit_deadletter` are created by `python -m arb_memory setup-schema` (idempotent;
  already run for slice 1). Verify they exist.
- **Consumer:** the `audit` service already exists in `deploy/docker-compose.yml` (`command: ["audit"]`) and runs
  the `AuditConsumer` (Valkey `arbmem:audit` stream → `audit_events`, deadletter on bad rows).
- **Close consumer:** `audit-close-consumer` receives bus close requests, performs the
  privileged Postgres reconciliation, and emits the single verdict.
- **Emitters:** the bridge daemon self-emits each seat's vote (`_emit_vote`, gated on audit redis + run_id +
  `payload.audit_vote_expected`); the orchestrator emits the manifest with
  `scripts/panel-run start` and requests verdict closure with
  `scripts/arb-audit-close-request`.

So activation = **(A) point the bridge's audit tee at prod, (B) run real panels
through the audited dispatch path, (C) close them over the bus.**

## 1. Confirm both audit consumers are up on `arb-prod`
The `audit` service connects with `${ARB_MEMORY_DSN}` (owner) and `${ARB_MEMORY_REDIS_URL}` (the audit bus,
db `/5` prod). It comes up with the rest of the stack:
```
docker compose -f deploy/docker-compose.yml up -d audit audit-close-consumer
docker compose -f deploy/docker-compose.yml logs --tail=20 audit audit-close-consumer
```
> **Least-privilege note (known follow-up):** unlike eval/transcript/visibility, there is no
> `apply_audit_grants` helper yet, so the audit consumer runs as **owner** for now. Giving it a dedicated
> write-role (SELECT/INSERT on `audit_events` + `audit_deadletter` only) is a deferred hardening task — track
> it; do not block activation on it.

## 2. Point the **Mac bridge** audit tee at prod (the activation key)
The bridge reads `ARB_MEMORY_REDIS_URL` to resolve the audit bus (`resolve_audit_redis`, prod db `/5`). On the
seat launchers (plists / bridge env), set:
```
ARB_MEMORY_REDIS_URL=rediss://default:<pw>@<managed-valkey-host>:25061/5   # prod audit bus (dev = /3)
```
`bootout` + `bootstrap` the bridge seats (plist env change). **Sanity:** with this UNSET the bridge is
byte-unchanged (votes simply don't emit — fail-soft). This is the rollback.

## 3. Run a panel through `panel-run` (orchestrator workflow)
For each gradable panel the orchestrator drives, set `ARB_MEMORY_REDIS_URL` to the
configured audit bus. The orchestrator does not receive a Postgres DSN or SSH access:
```
# 1. emit the dispatch manifest (mints/echoes the run_id):
RUN_ID=$(.venv/bin/python scripts/panel-run start \
  --roster seat:codex-bridge-dev-example,seat:agy-bridge-dev,seat:cold-opus \
  --note "design panel: <topic>")

# 2. dispatch each BRIDGE reviewer with the run marked — the bridge self-emits its vote:
agent-dispatch --engine codex --target-id codex-bridge-dev-example --run-id "$RUN_ID" --audit-panel "<brief>"
agent-dispatch --engine agy-print --target-id agy-bridge-dev --run-id "$RUN_ID" --audit-panel "<brief>"

# 3. capture each NON-bridge reviewer (cold-Opus, in-session) from its reply text:
.venv/bin/python scripts/panel-run vote --run-id "$RUN_ID" --actor seat:cold-opus --report /tmp/cold-opus-reply.txt

# 4. request reconcile-gated closure over the bus. verdict.json carries kind,
# roster, stances, decision, and rationale. REFUSED reconciliation exits 4:
scripts/arb-audit-close-request \
  --run-id "$RUN_ID" \
  --payload-file verdict.json \
  --requested-by "$ORCHESTRATOR_ID"
```
Review seats must end their reply with the fenced ```vote block (now mandated by `scripts/review-brief`).
Every `verdict.json` roster and stance key must use those same exact actor IDs:

```json
{
  "kind": "verdict",
  "roster": ["seat:codex-bridge-dev-example", "seat:agy-bridge-dev", "seat:cold-opus"],
  "stances": {
    "seat:codex-bridge-dev-example": "approve",
    "seat:agy-bridge-dev": "approve",
    "seat:cold-opus": "needs-changes"
  },
  "decision": "needs-changes",
  "rationale": "Evidence-based panel synthesis."
}
```

## 4. Activation E2E (close-condition)
- Run one real panel as above → confirm `audit_events` for that `run_id` holds `{dispatch:1, vote:N, verdict:1}`:
  ```sql
  SELECT kind, count(*) FROM audit_events WHERE run_id = '<RUN_ID>' GROUP BY kind;
  ```
- **Anti-laundering proof:** on a separate unclosed test run, submit a close-request
  payload whose stance contradicts a recorded vote. It must return
  `refused_reconcile` / exit 4 and write no verdict; then close that run with the
  correct recorded stances.
- Kill the audit consumer mid-run → it reclaims (consumer-group), no loss; deadletter holds any unparseable row.

## Rollback
Unset `ARB_MEMORY_REDIS_URL` on the bridge seats + restart → votes stop emitting,
bridge byte-unchanged. Stop the `audit` and `audit-close-consumer` services. Schema is
additive (no rollback needed).

## Deferred (not this activation)
Dedicated audit write-role + `apply_audit_grants` (least-privilege for the consumer) · cross-host orchestrators
emitting from beyond the Mac · the seam-race exactly-once fix.
