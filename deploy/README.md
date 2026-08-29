# ARB Memory Phase 3 Deploy Runbook

Phase 3 go-live is operator-driven. Do not point real Claude or ChatGPT connectors at the MCP host until the local suite is green and a live connector canary passes against both real providers.

## Version floor

**PostgreSQL ≥ 17** is required for the bus-side claim gate. Readiness probes
`MAINTAIN` via `has_table_privilege` (Slice 1c `PsycopgClaimResolver.assert_ready`);
that privilege exists only on PostgreSQL 17+. Managed instances and local compose
must meet this floor before enabling `BRIDGE_CLAIM_GATE=1`.

## Provision

1. Provision a DigitalOcean managed Postgres instance with pgvector enabled (PostgreSQL ≥ 17).
2. As `doadmin`, pre-create the per-environment MCP role (`arbmemory-dev-mcp` for dev, `arbmemory-mcp` otherwise), create `mcp_auth` owned by the owner role, and set `mcp_auth` default privileges for future table DML.
3. Apply `src/arb_memory/schema.sql` as the owner role. This is DDL-only: extension, public tables, `mcp_auth`, and `mcp_auth` tables.
4. As the owner role, run `apply_mcp_grants(conn, role)` once for the configured MCP role. **This is the authoritative grant + default-privilege step** — its `ALTER DEFAULT PRIVILEGES`, run *as the owner*, is what makes *future* owner-created `mcp_auth` tables writable by the MCP role (Postgres default privileges are per-creating-role, so the doadmin step-2 defaults only bite for objects doadmin itself creates). **Do not skip step 4 even if step 2 ran**, or the OAuth door will silently fail to write its state. (`GRANT SELECT ON hints, artefacts TO <role>` is the minimum public read surface; `apply_mcp_grants` is preferred — it also sets `mcp_auth` DML + defaults and revokes audit.) Note: `schema.sql` no longer provisions the role, so any init automation that ran `psql < schema.sql` expecting role creation must do steps 2+4 separately. The role name is configurable via `ARB_MEMORY_MCP_ROLE` (default `arbmem_mcp`).
5. Run one SSL pooled grant check as the configured MCP role: `SELECT count(*) FROM hints` must pass; `INSERT INTO hints ...` must fail with insufficient privilege.
6. Create the Cloudflare tunnel from `deploy/cloudflared/config.example.yml`, update the hostname, and create DNS for `ARB_MEMORY_MCP_PUBLIC_BASE_URL`.
7. **REQUIRED for any CF-proxied hostname:** add a **hostname-scoped Cloudflare Skip rule** or claude.ai's
   MCP data-plane will be silently `403`'d at the edge (it connects as `Claude-User`, which CF's managed
   bot / AI-Scrapers / Browser Integrity Check blocks — OAuth still passes because its backend uses a
   generic UA, so the connector fails *after* a clean token exchange). Rule: `expression: http.host eq
   "<your-host>"`, action **Skip**, skipping `http_ratelimit` + `http_request_sbfm` +
   `http_request_firewall_managed` + Browser Integrity Check (`bic`). Keep it hostname-scoped — do NOT
   disable bot protection zone-wide. Verify: a request with `User-Agent: Claude-User/1.0` must return `401`
   (reaches origin), not `403`.

## Local Dev

For the local compose overlay only, ensure the default MCP role exists and set the dev password before
running role tests. Use the same throwaway value you set for `ARB_LOCAL_PG_PASSWORD`:

```sh
docker compose -f deploy/docker-compose.local.yml exec postgres psql -U arb_memory -d arb_memory -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'arbmem_mcp') THEN CREATE ROLE arbmem_mcp LOGIN; END IF; END \$\$; ALTER ROLE arbmem_mcp PASSWORD '$ARB_LOCAL_PG_PASSWORD'"
```

Then set `ARB_MEMORY_MCP_DSN` to the `arbmem_mcp` DSN when running role tests locally.

## Secrets

Set these outside git, for example in the host environment or deployment secret store:

- `ARB_MEMORY_DSN`: owner/writer DSN for `memory` and `audit`.
- `ARB_MEMORY_REDIS_URL`: Valkey URL for `memory` and `audit` only.
- `ARB_MEMORY_MCP_PUBLIC_BASE_URL`: public HTTPS tunnel URL, no trailing slash.
- `ARB_MEMORY_MCP_DSN`: configured MCP role DSN with SSL/pool settings.
- `ARB_MEMORY_MCP_ROLE`: MCP database role name. Defaults to `arbmem_mcp`; set to the DigitalOcean per-environment role when `ARB_MEMORY_MCP_DSN` is derived from `ARB_MEMORY_DSN`.
- `ARB_MEMORY_MCP_LOGIN_SECRET`: passphrase required for MCP OAuth login.
- `ARB_MEMORY_MCP_TOTP_SECRET`: TOTP secret provisioned once into the operator's authenticator app.
- `OPENAI_API_KEY`: embedding owner key.
- `ARB_GATE_READER_ROLE` / `ARB_GATE_READER_DSN`: SELECT-only credential for the bus-side claim
  gate (Slice 1c). See **Bus-side claim gate reader** below. Never point a seat at the owner DSN
  for admission reads.
- `BRIDGE_CLAIM_GATE`: fleet enforcement switch. **Default `0` in Slice 1c** — leave off until
  Slice 1d lands the exempt-lane writer and arm/release compensation.
- `BRIDGE_WORKTREE_LANE`: `gated` (default) or `exempt`. Server-side only; dispatchers never
  assert lane membership.
- **Exempt lane (Slice 1d-iii):** one machine-user GitHub identity and SSH key, not a per-repo
  deploy key and not a fine-grained PAT. Supervisor process env only:
  - `BRIDGE_EXEMPT_GIT_SSH_COMMAND` — e.g. `ssh -i ~/.ssh/arb-exempt-bot -o IdentitiesOnly=yes`
  - `BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT` — recorded `SHA256:…` of that key
  - `BRIDGE_EXEMPT_PROVISIONING_LEDGER` — path to the JSON ledger of eligible `owner/repo` targets
  - Target repository is always derived from the seat checkout's actual `origin`.
    **`BRIDGE_EXEMPT_GIT_REMOTE_URL` is not authoritative** (and not required).
  - Full one-time owner setup, ledger fields, live proof, rotation, and the §9.3 residual:
    **[`docs/runbooks/exempt-seat-machine-user.md`](../docs/runbooks/exempt-seat-machine-user.md)**
    — do not summarize as “with its supervisor-provided key.”

## Bus-side claim gate reader (Slice 1c)

Operator-owned provisioning order. Application code never creates this role.

1. As cluster admin, create a dedicated LOGIN role with **no memberships**, no superuser /
   create-role / create-db / bypass-RLS capability, and ownership of **no** gate relation
   (`claims`, `attestations`, `seat_posture`, `lease_lanes`, or the three gate views).
2. As the ARB Memory owner, set `ARB_GATE_READER_ROLE` and run `python -m arb_memory grants`.
   The command calls `apply_gate_reader_grants`, which first runs `assert_gate_role_isolation`
   and **must abort** if the role has memberships or owns a gate relation. Do not weaken that
   assertion. Role creation is deliberately not in `schema.sql` or bridge startup.
3. Connect using the **reader DSN itself**. Prove SELECT on `claim_admissibility_v`,
   `seat_posture_v`, and `lease_lane_v`. Prove write failure on both `seat_posture_v` /
   `lease_lane_v` (automatically updatable views) and a base table such as `claims`.
4. Store `ARB_GATE_READER_DSN` in the **seat supervisor's secret/process environment**, not the
   app-repo `.env` / `--env-file` (that file is loaded from the seat worktree — the local-control
   residual). Install a package build that includes core `psycopg`.
5. Leave `BRIDGE_CLAIM_GATE=0` for the fleet in Slice 1c. A canary may use `1` only after the
   readiness checks pass and test data gives it a viable claim path. Slice 1d owns fleet
   enablement after the exempt-lane writer lands.

**Do not restart a seat with enforcement on if any provisioning/negative check is missing.**
With `BRIDGE_CLAIM_GATE=1`, missing reader DSN or failed readiness refuses registration before
the seat serves.

## Slice 1d-vi rollout (Task 7) — executable prerequisites

Both fleet flags stay **default off** until the matching checklist is green and the **owner**
flips them: `BRIDGE_TASK_REF_REQUIRED=0`, `BRIDGE_CLAIM_GATE=0`. This section documents
operator-owned cluster/fleet actions and the preflight surface; it does not flip flags for you.

### Deployment keys (process / secret env)

| Key / posture | Notes |
|---|---|
| `ARB_GATE_LANE_WRITER_ROLE` / `ARB_GATE_LANE_WRITER_DSN` | Per-seat LOGIN writer; never owner DSN |
| `BRIDGE_WORKTREE_LANE` | `gated` (default) or `exempt` |
| `BRIDGE_EXEMPT_GIT_SSH_COMMAND` | One `arb-exempt-bot` key; `IdentitiesOnly=yes` |
| Target checkout `origin` | Resolved GitHub target repo — **no** `BRIDGE_EXEMPT_GIT_REMOTE_URL` authority |
| `ARB_MEMORY_LOCAL_DSN` / `ARB_MEMORY_LOCAL_MCP` | Local hydration; plist `EnvironmentVariables` on live seats |
| `BRIDGE_WORKER_VANTAGE` | Nonblank supervisor-owned value (e.g. `bridge-dev-mac`) |
| `BRIDGE_CLAIM_GATE=0` | Fleet default until claim-gate checklist + owner canary |
| `BRIDGE_TASK_REF_REQUIRED=0` | Fleet default until ref-required checklist + owner canary |
| `ENV_SCRUB_CAPABILITY=bus-and-gate-daemon-creds-v2` | Advertised only after executed scrub self-check |
| `task_wire=legacy-or-ref-v1` | Dual-parse advertisement (registry) |
| `brief_hydrate=v1` | Only after `prove_brief_hydrate_readiness` |

### E25 deploy findings (must not regress)

1. **Redis-capable interpreter on the supervisor PATH.** `scripts/agent-dispatch` invokes bare
   `python3` for the authority subprocess, and `scripts/arb-memory-harness-publish` uses
   `#!/usr/bin/env python3`. Launchd plists' PATH often lacks a redis-importable interpreter.
   **Deploy either** puts the venv `bin/` on the supervisor PATH **or** pins
   `BRIDGE_SUPERVISOR_PYTHON` to that interpreter. `scripts/seat-preflight` check
   `supervisor-interpreter` proves `import redis` against the **checked input's PATH** (plist
   `EnvironmentVariables` / env-file `PATH`) when present; otherwise it falls back to the
   invoking shell PATH with an explicit limitation message. Pin `BRIDGE_SUPERVISOR_PYTHON` for a
   faithful check.
2. **Publish credential is process-env.** `--env-file` covers bus settings for the seat only, by
   design. The harness publish credential `ARB_MEMORY_REDIS_URL` must be in the **process
   environment** of the short-lived FABA/driver publisher — not only in an app-repo env file.
   Seat children must continue to scrub bus/gate-daemon secrets (`bus-and-gate-daemon-creds-v2`).

### Process-env-only keys (provenance)

These keys are read by `bridge.py` from **process environment** (launchd
`EnvironmentVariables` / shell export), not from `--env-file` alone:

| Key | Runtime reader |
|---|---|
| `BRIDGE_WORKER_VANTAGE` | `bridge.py` register path |
| `BRIDGE_WORKTREE_LANE` | must be exactly `gated`\|`exempt` |
| `ARB_GATE_LANE_WRITER_DSN` | lane writer (process-env secret only) |
| `ARB_MEMORY_LOCAL_DSN` | `prove_brief_hydrate_readiness` (process-env attested) |

`ARB_MEMORY_REDIS_URL` is **not** process-env-only at the seat: `bridge.py`
`resolve_audit_redis` (process env first, `--env-file` fallback — `bridge.py:205-212`)
arms vote emission from either source. Seat-preflight reports presence but does not
FAIL env-file placement. Short-lived FABA/driver publishers still carry the publish
credential in **process environment** (see “Publish credential is process-env” above);
do not persist that credential into a long-lived seat plist.

When `scripts/seat-preflight` is invoked on an env **file**, it can only attest file contents for
the process-env-only keys above and labels PASS messages accordingly (`attested from env file
only; bridge.py reads process env …`). Prefer a **plist** whose `EnvironmentVariables` carry
those keys so the check can report process-env provenance.

### Preflight and checklist invocation

```sh
# Default seat posture (flags stay 0; gated seats need no exempt Git vars)
# Prefer the launchd plist so process-env keys are attested from EnvironmentVariables.
scripts/seat-preflight /path/to/seat.plist

# Before owner canary of BRIDGE_TASK_REF_REQUIRED=1
scripts/seat-preflight /path/to/seat.plist --checklist ref-required

# Before owner canary of BRIDGE_CLAIM_GATE=1
scripts/seat-preflight /path/to/seat.plist --checklist claim-gate

# Before separate legacy-removal code wave (after zero-legacy observation window)
scripts/seat-preflight /path/to/seat.plist --checklist legacy-removal
```

Checklist evidence files (JSON paths in process/env) are **operator-authored attestations**, not
live fleet probes: the operator (or a named collection tool they run) writes the JSON; preflight
checks shape, roster coverage, and window bounds against those files. They are not re-verified
against Redis/Postgres at checklist time. Exception: `exempt-git` performs live git/SSH I/O in
checklist/default mode when the seat lane is `exempt`.

| Env key | Used by |
|---|---|
| `BRIDGE_PREFLIGHT_TARGET_REGISTRY` | `target-dual-parse`, `target-advertises-hydrate`, roster source for hydration/parse-only cross-binds. **Required shape:** top-level `roster` (array of every selected dual-parse/hydrate `agent_id`), top-level `parse_only` (array of parse-only `agent_id`s; use `[]` for zero), and `targets[]` with `agent_id`, `task_wire`, `brief_hydrate`, `worker_vantage` covering every roster id |
| `BRIDGE_PREFLIGHT_SENDER_MIGRATION` | `sender-migration` (`all_senders_migrated`, `remaining`) |
| `BRIDGE_PREFLIGHT_REF_HYDRATION` | `ref-hydration-success` (`targets[]` with `ok`); must cover **every** registry `roster` id with `ok: true` |
| `BRIDGE_PREFLIGHT_PARSE_ONLY` | `parse-only-legacy`, `parse-only-ref-refusal` (`authority_emits`, `direct_ref_error=brief_hydration_unavailable`); empty `targets` only passes when registry `parse_only` is `[]` |
| `BRIDGE_PREFLIGHT_LEGACY_OBSERVATION` | `zero-legacy-observation` (`zero_authority_legacy`, ISO-8601 `window_start`/`window_end`) |

**Observation window bounds (enforced):** minimum duration **24 hours**; maximum staleness
(`now - window_end`) **7 days**; `window_end` must not be in the future (allowed skew **60
seconds** for clock jitter). FAIL messages name missing/unparseable bounds, `window_end <=
window_start`, duration below floor, staleness beyond the bound, and a future `window_end`.

**Lane binding keys (not live reconcile):** checklist check `lane-binding-keys` attests DSN/ROLE/
consumer/lane **key presence** and lane-string match only — not DB reconcile readiness.

### Ordered fifteen-item rollout (operator-owned)

1. Record a Slice 1d base SHA and the six reviewed stage SHAs.
2. Provision isolated `arb_gate_reader`; provision one lane login per seat plus `NOLOGIN` function
   owner and exact role→consumer/lane binding. Run owner grants and live cross-seat deny proofs.
3. Place reader/writer DSNs only in supervisor secret/process environment. Inject
   `ARB_MEMORY_REDIS_URL`/future harness publish material only into the short-lived FABA driver;
   prove Bash, Go, `ctl`, warm/in-process callers, and every engine child lack it. Run the
   selected-engine subprocess/self-check; require `bus-and-gate-daemon-creds-v2` before
   `register()`.
4. Configure the local-reader credential and helper prerequisites for every prospective gate
   seat, but do not advertise hydration readiness before the Stage 1d-v executable check exists
   and passes.
5. The owner executes the one-time machine-user runbook outside this implementation: manually
   create `arb-exempt-bot` and its one SSH key; add it to the paid `example-org`
   `arb-exempt-readonly` Read team; provision every target in the ledger; and move any private
   personal target into an organization rather than granting write-capable personal collaborator
   access. For each exempt seat, resolve the target from its actual checkout and require the same
   fingerprint/account, matching ledger entry, target-specific read-positive, classified push
   denial, and the isolated writable control's loud blocker. Any target without machine-user Read
   hard-refuses; do not fall back to operator credentials, a PAT, or a per-repository deploy key.
6. Keep both flags 0. Deploy Stage 1d-iv receive-only dual-accept bridges first. Query the complete
   target roster and require every target to advertise `task_wire=legacy-or-ref-v1` plus its
   nonblank supervisor-owned `worker_vantage`; an absent/stale target blocks. Confirm
   `brief_hydrate` is absent at this stage.
7. Deploy the single authority and every enumerated caller/doc/test migration. Run the direct
   enqueue/string-task and publish-credential tripwires, full suite, and
   `scripts/check-doc-drift`. Prove the authority still emits legacy to every parse-only target,
   and a deliberately injected ref gets exact `brief_hydration_unavailable` before engine start
   with no `str(dict)` prompt.
8. Deploy Stage 1d-v hydration seat-by-seat. On each seat, run the real helper/local-reader,
   pointer-prompt, receipt, and cleanup readiness check before advertising `brief_hydrate=v1`.
   Prove the authority switches only that parse+hydrate seat to ref emission while undeployed or
   failed-readiness seats remain legacy. After the complete roster advertises both capabilities,
   prove successful pinned ref hydration on every target and record zero non-authority ordinary
   enqueue paths. Until all of steps 6–8 pass fleet-wide, do not enable ref-required.
9. Canary `BRIDGE_TASK_REF_REQUIRED=1`; require `ref-required-v2`, exact legacy-string refusal, and
   successful ref dispatch/hydration. Roll seat-by-seat with rollback to 0 on any failure.
10. With claim gate still 0, restart with lane writer active and require clean locked two-record
    startup reconcile. Inject/observe the heartbeat-mid-arm barrier in the canary verifier.
11. Arm exempt; confirm row says bound exempt/consumer; dispatch a published ref without claim;
    prove domain-separated hash, ordinary push denial, and zero enqueue on store outage; release
    and confirm row/worktree retired.
12. Run gated control: no claim gives exact `missing_claim_ref`.
13. Only then canary `BRIDGE_CLAIM_GATE=1`, confirm all readiness precedes registration, and repeat
    both paths.
14. After the bounded zero-legacy observation window, remove the compatibility string branch in a
    **separate code/deployment wave**, retire `BRIDGE_TASK_REF_REQUIRED`, rerun mixed-fleet
    negatives/full suite/doc drift, and advertise final `ref-only-v2`. Any observed legacy sender
    cancels removal and points to the owning caller.
15. Wait for Slice 1e–1h before fleet-wide claim-gate enablement; then roll one seat at a time with
    rollback `BRIDGE_CLAIM_GATE=0` if any prerequisite fails.

### Delivered-prerequisites map

| Enablement prerequisite | Delivered by |
|---|---|
| Reader role applied/ready | Slice 1c, rechecked Task 7 |
| Per-seat writer binding/functions applied/ready | Tasks 1, 7 |
| Daemon lane secret absent from every child/session/transcript | Tasks 1, 7 |
| Arm/release atomic from caller perspective | Task 2 |
| Heartbeat race + crash replay converge | Task 2 |
| One machine-user key resolves each actual target; every provisioned target fetches but cannot push normally; unprovisioned targets hard-refuse | Task 3 |
| One dispatch-time-vantage enqueue authority; FABA-only publish identity; outage stops real enqueue | Task 4 |
| Receive-only dual transition + every caller/recipe migrated + named parse-only ref refusal | Task 5 |
| Worker hydrates domain-separated exact hash using local read credential and advertises readiness only after execution | Task 6 |
| One audited admission entry point | Task 6 |
| Ref-required then separate legacy removal | Task 7 |
| Exempt arm/run/release end to end | Task 7 live canary (owner) |
| Full confirmation/verifier machinery | Slices 1e–1h, external prerequisite to fleet-wide flip |

## Start

0. Copy `deploy/.env.example` → `deploy/.env` (gitignored) and fill every value: the DO DSNs + Valkey URL + `OPENAI_API_KEY`, `ARB_MEMORY_MCP_PUBLIC_BASE_URL` (must equal the tunnel hostname exactly, no trailing slash), the two operator secrets (`ARB_MEMORY_MCP_LOGIN_SECRET`, `ARB_MEMORY_MCP_TOTP_SECRET`), and `TUNNEL_TOKEN`. The `cloudflared` service is **token-model** (Option A): it runs `tunnel run` with `TUNNEL_TOKEN`; the public-hostname→`http://mcp:8000` route is configured dashboard-side (no `config.yml`/credentials-file mounted). `docker compose` auto-loads `deploy/.env`.
1. Build and start: `docker compose -f deploy/docker-compose.yml up -d --build`.
   > **Rebuild gotcha:** only the `memory` service carries the `build:` block; `audit`/`mcp`/`cloudflared` just reuse the shared `arb-memory:phase3` image. So `docker compose build mcp` (or `up --build mcp`) is a **silent no-op** — to pick up code changes you must `docker compose build memory` (or `build` with no service / `up --build` for all), then `up -d --force-recreate`.
2. Confirm `memory`, `audit`, `audit-close-consumer`, `mcp`, and `cloudflared` all have `restart: unless-stopped`; the tunnel flips from `inactive` to active once `cloudflared` connects. The `mcp` server binds `0.0.0.0` (via `ARB_MEMORY_MCP_HOST`, default `0.0.0.0`) so cloudflared can reach it over the compose network — a `127.0.0.1` bind is loopback-only inside the container and unreachable.
3. Check MCP readiness: `docker compose -f deploy/docker-compose.yml exec mcp python -c "from arb_memory.mcp.health import readiness; print(readiness())"`.
4. Confirm liveness remains process-level during a temporary database blip; degraded readiness should not be treated as a restart signal.

## Close an Audit Run

After the dispatch manifest and votes have been committed, request closure over the
audit bus from the repository checkout. Use the orchestrator's honest identity:

```sh
scripts/arb-audit-close-request \
  --run-id <id> \
  --payload-file verdict.json \
  --requested-by <orchestrator-agent-id>
```

Only `{"outcome":"emitted"}` is success. Reconcile refusal is a panel gap; fix the
recorded roster or stances rather than bypassing the consumer. The caller needs
`ARB_MEMORY_REDIS_URL`, not a Postgres DSN or SSH access.

The container-local `python -m arb_memory audit-close` command is a privileged
break-glass recovery surface for an operator already inside the production Compose
boundary when the bus consumer itself is unavailable. It is not the normal close path
and must not be exposed to orchestrator hosts. The command lives in the shared image;
rebuild `memory`, then recreate the services when deploying changes to it.

