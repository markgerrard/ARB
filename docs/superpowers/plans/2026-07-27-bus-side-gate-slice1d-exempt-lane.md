# Bus-Side Gate — Slice 1d: exempt lane and brief-artefact dispatch

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the bus-side gate's usable front door: arm store-authoritative gated/exempt
worktree leases through a separately isolated lane-writer credential, make exempt worktrees use a
push-less `origin`, replace body-shaped request tasks with immutable brief artefact references, and
make the worker hydrate the pinned brief through its own local read credential. Sequence the fleet
transition from `BRIDGE_CLAIM_GATE=0` without enabling a total dispatch lockout.

**Architecture:** Every daemon receives a distinct PostgreSQL login role. It has `EXECUTE` only on
three owner-controlled `SECURITY DEFINER` functions: arm, retire, and list. Those functions derive
`armed_by` and the allowed lane from an owner-only binding keyed by `session_user`; no runtime
caller supplies either value and no runtime role has direct `lease_lanes` DML. The daemon-only DSN
and role are scrubbed from every Popen and Agent SDK child, session store, and transcript while
`ARB_MEMORY_LOCAL_DSN` remains available to the worker. The bridge mints a lease id, takes its
per-lease lock, and holds it across worktree creation, filesystem publication, row arm, durable
result, and reply. Heartbeat/startup reconciliation uses the same lock, so an in-flight arm is
not mistaken for an orphan; request-id persistence makes crash replay deterministic.

Exempt worktrees resolve `origin` from the worker's actual target-repository checkout, then receive
worktree-local Git config for one operator-provisioned read-only machine-user SSH identity shared
across the target fleet. They must pass a target-specific fetch-positive, specifically classified
push-denied live proof before their row is armed. Dispatch moves through a sequenced compatibility
protocol rather than a flag-day:
gate-off bridges first dual-accept legacy prose and versioned refs and advertise that parse
capability, while the authority remains legacy-emitting. One Python send authority then validates
at the selected worker's dispatch-time vantage and alone enqueues; its FABA driver can publish,
while Bash, Go, `ctl`, and other non-FABA callers can present only a separately harness-published
ref and receipt plus the exact receipt-hashed brief bytes needed for temporary legacy fallback.
Every caller migrates through the authority. Worker hydration then lands
seat-by-seat and advertises a distinct `brief_hydrate=v1` readiness capability only after the
helper and receipt path are live; ref emission is permitted only when the frozen target advertises
both parse and hydration readiness. A ref that reaches a parse-only seat is refused by name before
prompt construction and can never become `str(dict)` model input. Only after both capabilities and
ref emission are proved fleet-wide is ref-required admission enabled and, in a later cleanup wave,
legacy acceptance removed. The bridge gates only the immutable reference, constructs a
pointer-only hydration instruction, and requires the worker to invoke a local hydrator using
`ARB_MEMORY_LOCAL_DSN`; the bridge never queries `artefacts` or reads the hydrated body.
Dispatch-resolution and hydration receipts are audit records, never admission credentials.

**Tech Stack:** Python 3.11+, `psycopg` 3, PostgreSQL 17, pytest, Git worktree config, Bash
`scripts/agent-dispatch`, Go `tools/go-client`, and the existing FABA harness publisher in
`tools/faba/faba_launch.py`.

**Spec:** `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` — ARB Memory
`art-8742dfc1ca4b8be8` v6. Binding sections: §3 (two lanes), §5.2 (stored dispatch, assumptions,
audit-not-authority, outage posture), §5.3 (consumer-armed store fact), §6 (credential-shaped
exemption and closed arm schema), §9.2 (probe contents return only through gated review), §9.3
(honest credential residual), and §10 (Slice 1 cannot ship without the exempt lane). Also read
`docs/defect-classes/refusal-is-ambient-assert-the-code.md` before writing refusal tests and
`docs/defect-classes/prediction-written-as-result.md` before recording implementation evidence.

**Prerequisite — database-backed evidence must not skip.** Export the supplied owner DSN before
every verification command:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
```

`tests/arb_memory/` skips without `ARB_MEMORY_DSN`; a skipped run proves nothing. Before Task 1,
confirm the current substrate and the relevant Slice 1b/1c, worktree, dispatcher, local-read, and
FABA baselines:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python - <<'PY'
import os
import psycopg
with psycopg.connect(os.environ["ARB_MEMORY_DSN"]) as conn:
    row = conn.execute(
        "SELECT current_setting('server_version'), current_user, current_database(), "
        "EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arb_gate_reader'), "
        "EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arb_gate_lane_writer')"
    ).fetchone()
print(
    f"server={row[0]} user={row[1]} db={row[2]} "
    f"arb_gate_reader_exists={row[3]} arb_gate_lane_writer_exists={row[4]}"
)
PY
.venv/bin/python -m pytest \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_claim_resolver.py \
  tests/arb_memory/test_run_grants.py -q
.venv/bin/python -m pytest \
  tests/test_bridge_worktree_lease.py \
  tests/test_envelope_claim_fields.py \
  tests/test_agent_dispatch.py \
  tests/arb_memory/test_local_memory_injection_codex.py \
  tests/test_seat_preflight.py -q
(cd tools/go-client && go test ./...)
.venv/bin/python -m pytest \
  tools/faba/tests/test_faba_schema.py \
  tools/faba/tests/test_author_round_guard.py -q
```

Observed in this worktree on 2026-07-27, not predicted:

```text
server=17.10 (Debian 17.10-1.pgdg12+1) user=arb_memory db=arb_memory arb_gate_reader_exists=False arb_gate_lane_writer_exists=False
46 passed in 18.17s
71 passed, 25 warnings in 8.03s
ok  	github.com/markgerrard/ARB/go-client	0.782s
69 passed in 0.27s
```

The 25 warnings are the existing Redis `retry_on_timeout` deprecation at `bridge.py:564`, not a
lane or brief failure. The fixed `arb_gate_lane_writer` existence probe is retained only for
baseline comparability with the two reviews; this revision provisions per-seat writer logins
instead of that shared role.

The deployment shape was also observed directly:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
git remote -v
git rev-parse --git-common-dir
git rev-parse --git-dir
git config --show-origin --get-regexp \
  '^(remote\..*\.(url|pushurl)|url\..*\.insteadof|credential\.)' || true
```

Observed:

```text
origin  git@github.com:markgerrard/ARB.git (fetch)
origin  git@github.com:markgerrard/ARB.git (push)
/Users/<user>/<workspace>/.git
/Users/<user>/<workspace>/.git/worktrees/i1d-planrem
file:/Applications/Xcode.app/Contents/Developer/usr/share/git-core/gitconfig credential.helper osxkeychain
file:/Users/<user>/.gitconfig credential.https://github.com.helper
file:/Users/<user>/.gitconfig credential.https://github.com.helper !/opt/homebrew/bin/gh auth git-credential
file:/Users/<user>/.gitconfig url.git@github.com:.insteadof https://github.com/
file:/Users/<user>/.gitconfig url.git@github.com:.insteadof git://github.com/
file:/Users/<user>/<workspace>/.git/config remote.origin.url git@github.com:markgerrard/ARB.git
```

That evidence rules out pretending the current worktree has a separate remote or credential. The
push-less mechanism and its residual are explicit below.

## Remediation decisions and dispatch stages

These decisions are binding for this revision:

1. **Writer authority:** choose per-seat login roles plus owner-controlled `SECURITY DEFINER`
   functions, not the originally planned shared table-DML role. The shared role would remain
   fleet-wide exemption authority wherever it leaked. A per-seat authenticated role, an
   owner-only role→`(consumer_id,lane)` binding, and functions with no caller-supplied identity or
   lane make the binding real in PostgreSQL and bound the remaining credential blast to one seat.
2. **Wire transition:** adopt the remediation brief's mandated sequenced transition, not a
   literal flag-day. `BRIDGE_CLAIM_GATE=0` is not treated as wire compatibility because envelope
   validation precedes the claim gate. `task_wire=legacy-or-ref-v1` means parse-capable only and
   is receive-only during Stage 1d-iv. A separate `brief_hydrate=v1` advertisement, introduced in
   Stage 1d-v only after live helper readiness, authorizes ref emission. The authority selects a
   ref only when the frozen target has both capabilities; otherwise it selects legacy. A
   parse-only seat refuses any received object task as `brief_hydration_unavailable` before prompt
   construction. This receive-only interval preserves an independently green Stage 1d-iv.
3. **Arm/reconcile serialization:** use the existing per-lease file lock, acquired immediately
   after explicit lease-id mint and held through reply/result durability. Reconcile may apply
   missing-row⇒reclaim only after it acquires that same lock.
4. **Send authority:** ordinary dispatch has one enqueue seam,
   `dispatch_authority.publish_and_enqueue`; Bash, Go, `ctl`, FABA, and in-process callers cannot
   enqueue ordinary requests elsewhere. Lifecycle/control messages remain allowed through their
   existing narrow paths.
5. **Gate entry point:** `claim_gate.evaluate` is the only admission entry point. Migrate callers
   and remove `check`; no compatibility wrapper or duplicate raw-resolver path remains.
6. **Harness publish identity:** the short-lived FABA driver process is the only caller class
   allowed to hold `ARB_MEMORY_REDIS_URL` or future harness-publish write material and invoke
   `publish_artefact_and_gate`. Author/reviser agents and engine children never receive it. Bash,
   Go, `ctl`, and non-FABA in-process callers have no publish credential and may call the enqueue
   seam only with a pre-minted `{artefact_id, version}` plus a target-bound receipt produced by a
   separate FABA-driver publish step. During the legacy window they also provide the exact original
   brief bytes; the authority verifies their domain hash against the receipt before they may be
   used for legacy emission. Those bytes never authorize publication and are dropped when legacy
   emission is removed. The warm orchestration path coordinates that driver but has no direct
   `memory_write`, `store_artefact`, `psycopg`, or store-write path.
7. **Exempt Git identity:** use one manually created `arb-exempt-bot` machine-user account and one
   SSH key across every provisioned target repository, not one deploy key per repository and not a
   fine-grained PAT. GitHub documents machine users as the multi-repository SSH shape, whereas a
   fine-grained PAT is limited to resources owned by one selected user or organization. The
   machine user receives only an organization Read role. A private personal-account repository
   cannot enter this lane through an ordinary collaborator grant because GitHub makes that grant
   read/write; it must first move under an organization where Read can be assigned. The runbook
   describes these one-time owner actions; this plan and its implementation create no GitHub
   account and grant no GitHub access.

Implementation is **six separate dispatches**, each separately reviewed and green before the next
begins. No worker receives this whole plan as one implementation dispatch:

| Stage | Scope | Independently green exit |
|---|---|---|
| **1d-i** | Per-seat function authority and daemon-secret containment | Live function/grant isolation, real child-process scrub tests for every spawn family, capability/pre-registration proof |
| **1d-ii** | Locked two-record arm/release/reconcile and crash replay | Barrier race test, replay crash matrix, compensation mutations, no successful reply on reclaimed state |
| **1d-iii** | Multi-repository machine-user SSH provisioning and classified push denial | Runbook-owned identity exists; each actual target read succeeds; writable/archived/hook/auth/network controls classify correctly |
| **1d-iv** | Receive-only dual-accept wire, credential-scoped publish-and-enqueue authority, dispatch-time vantage, and complete caller migration | Full callers/tests/docs migrated, doc drift clean, every target advertises parse capability, authority emits legacy to every target lacking hydration readiness, and object tasks on parse-only seats refuse by name before prompt construction |
| **1d-v** | Domain-correct worker hydration, hydration-ready advertisement, ref selection, and single-entry audit | Local-reader hash/path mutations RED; `brief_hydrate=v1` follows executed readiness; authority emits refs only to targets advertising both capabilities; bridge has one resolver entry and cannot admit from audit |
| **1d-vi** | Ref-required wave, legacy-removal wave, preflight, canary, rollout | Fleet parse+hydration advertisement and ref-emission proof, ref-required canary, later zero-legacy proof/removal, full suite and live E2E |

The implementation orchestrator records one commit range and review result per stage. A failed
exit leaves the fleet at the preceding green/default-off stage; it does not get bundled into a
later dispatch. In particular, a completed 1d-iv is safe to hold indefinitely: callers are
migrated but the authority still emits legacy to parse-only targets. A partially deployed 1d-v is
also safe: only seats whose executed helper readiness produced `brief_hydrate=v1` receive refs.

Stage 1d-i is intentionally the largest dispatch despite the round-1 F7 review-depth warning. Its
files implement one indivisible authority boundary: the static functions/grants establish the
credential, while every spawn-family scrub, pre-registration self-check, and live deny proof
contains that same credential. Splitting at either side would create a nominally green interval
where a writer credential exists without proven containment, or a scrub capability advertises
without the authority it claims to contain. The stage therefore stays one dispatch, but its review
is bounded to Task 1, one closed privilege-coverage table, one central scrub predicate, real
subprocess probes, and a stop-for-review gate before any worktree lifecycle change.

## Global Constraints

- **Scope is the Slice 1c enumeration, exactly.** The only implementation subjects are: the
  exempt-lane Git credential/configuration, consumer arm-time `lease_lanes` write and two-record
  compensation/reconciliation, brief-artefact dispatch, worker hydration, and the rollout that
  makes those prerequisites true before gate enablement. Slice 1c names those four items at
  `docs/superpowers/plans/2026-07-27-bus-side-gate-slice1c-resolver-wiring.md:239-241`.
- **The lane writer is not the reader and not the owner.** `arb_gate_reader` is deliberately
  SELECT-only on three views (`src/arb_memory/mcp/grants.py:423-486`) and its runtime resolver is
  built around that contract (`claim_resolver.py:266-310`). It never receives `lease_lanes` DML.
  `PsycopgLaneWriter` uses only `ARB_GATE_LANE_WRITER_DSN`, sourced from the supervisor process
  environment, with no fallback to `ARB_GATE_READER_DSN`, `ARB_MEMORY_DSN`,
  `ARB_MEMORY_MCP_DSN`, or the app-repo env file.
- **Writer identity and lane are database-bound, not application convention.** Add an owner-only
  `lane_writer_bindings(db_role name PRIMARY KEY, consumer_id text UNIQUE NOT NULL, lane text NOT
  NULL CHECK (lane IN ('gated','exempt')))` and exactly these functions:
  `public.arm_lease_lane(p_lease_id text) RETURNS public.lease_lanes`,
  `public.retire_lease_lane(p_lease_id text) RETURNS boolean`, and
  `public.list_lease_lanes() RETURNS SETOF public.lease_lanes`. Each is `SECURITY DEFINER SET
  search_path=pg_catalog`, fully qualifies every object, looks up the binding by `session_user`,
  and either acts as that bound consumer/lane or raises a named unbound-role refusal. There is no
  `armed_by`, `lane`, or consumer parameter. `retire` deletes only
  `(p_lease_id, bound.consumer_id)` and `list` returns only that consumer's rows.
- **Function bodies have one static source.** The complete bodies of
  `arm_lease_lane`, `retire_lease_lane`, and `list_lease_lanes` live only in checked-in
  `src/arb_memory/schema.sql`; the production schema/grants apply path executes that file and then
  applies grants. `lane_writer.py`, `grants.py`, `run.py`, and tests must not assemble, format, or
  interpolate function-body SQL from `lease_id`, role, consumer, or lane values. Runtime calls
  bind `p_lease_id` as a query parameter. Live function/grant tests create the functions from the
  same checked-in `schema.sql` bytes production applies, then run `apply_gate_lane_writer_grants`;
  no test-only Python function body or hand-copied SQL fixture is allowed.
- **The writer's minimum privilege set is closed and justified.** Each per-seat login gets schema
  `USAGE` and `EXECUTE` on exactly those three functions. It gets no direct table, view, sequence,
  binding-table, function-owner, membership, or ownership authority. The function owner is
  `NOLOGIN`, owns no runtime role, and is not usable via `SET ROLE`. The functions themselves need
  owner-side INSERT/DELETE/SELECT to arm, retire, and reconcile; that authority is never granted
  to the authenticated runtime role.
- **Privilege coverage is executable data, not a privilege paragraph.** Define closed
  PostgreSQL-17 table/view/column/function privilege vocabularies and one lane-writer coverage
  table whose every `(object, privilege, channel)` entry is either `allowed-because-*` or
  `probed-denied-by-*`. Runtime readiness and tests derive from it. The only runtime grants are
  the three exact function EXECUTEs plus schema USAGE. The parametrized live KILL must cover
  relation, view, column, function, PUBLIC, membership, ownership, and function-owner channels;
  deleting any denied arm makes the injected authority return unnoticed and the test fail. This
  reuses Slice 1c's coverage-table pattern without importing its private constants.
- **Isolation has deployment and behavioral assertions.** Add
  `assert_gate_lane_writer_isolation`, separate from `assert_gate_role_isolation`, and require the
  per-seat login's `session_user`, binding, expected `consumer_id`, and configured lane to agree;
  require no membership and no ownership of any gate relation or function. `NOINHERIT` is not
  accepted as isolation; `grants.py:364-420` documents and enforces why. Live tests must prove
  seat A cannot list or retire seat B's row, cannot forge `armed_by` or lane through the function
  signatures, and gets PostgreSQL `42501` on raw SELECT/INSERT/DELETE. A mutation that drops the
  bound-consumer DELETE predicate must make the cross-seat test RED.
- **The lane DSN is a first-class daemon-only secret.** Replace the reader-specific child
  predicate in `engines/_stdio.py:32-53` with one gate-daemon-credential predicate covering reader
  and lane-writer DSN/role names. Use it in the Popen scrub, both Agent SDK blanking overlays,
  spawn-time final-env assertion, `ScrubbedSessionStore`, stderr/transcript payload scrub, and
  fixture cleanup. Keep `ARB_MEMORY_LOCAL_DSN` present because hydration intentionally runs in the
  worker. Bump `ENV_SCRUB_CAPABILITY` from `bus-creds-v1` to
  `bus-and-gate-daemon-creds-v2`, update the FABA mirror/parity test, and make seat preflight run
  the same selected-engine self-check before `register()`. Every supported engine advertises v2
  only after that engine-specific check passes; the current Agent-SDK-only advertisement exception
  is removed. Each Popen adapter family and both keyed/subscription Agent SDK overlays get a real
  subprocess probe: local hydration DSN present, lane-writer DSN/role absent or blank.
- **Harness publish material uses the same child-deny boundary.** The FABA driver receives
  `ARB_MEMORY_REDIS_URL` (and any later harness-publish token) through its own short-lived,
  allowlisted environment; no author/reviser agent, Bridge engine child, session store, stderr, or
  transcript receives the key, its value, or the target-bound publish receipt's authentication
  material. `ARB_MEMORY_REDIS_URL` is already in the `is_bus_credential` class; future publish
  variables must enter that same central class before use. Add the publish sentinel to every real
  Popen and both Agent SDK overlay probes and require absent/blank, while the dedicated FABA-driver
  probe alone proves it is present and can publish. A subprocess allowlist and static call-site
  tripwire must fail if Bash, Go, `ctl`, a warm/in-process caller, or an engine child can invoke
  `memory_write`/`publish_artefact_and_gate` or receives harness write material.
- **Arm success means both records and the result are durable under one lock.** The live arm path
  creates the worktree and filesystem lease at `bridge.py:1935-1947` and currently replies success
  at `bridge.py:1948-1959`. Split id mint from filesystem publication: mint, acquire that
  lease-id's lock, create the worktree, durably write the filesystem record with `arm_request_id`,
  invoke `arm_lease_lane`, durably write status/result, send reply, then release. The lock is held
  across the entire mint→filesystem→row→result→reply sequence.
  If row insertion fails, reclaim the worktree, tombstone the filesystem record with
  `lane-row-arm-failed`, and return the specific `worktree-lane-arm-store-failed` refusal. If either
  compensation step also fails, return a specific composite failure; the tombstoned/missing
  registration must still make `validate_worktree_lease` fail at `bridge.py:1988-2000`.
- **Release retires both facts before success.** The live path currently reclaims then tombstones
  at `bridge.py:1965-1974`. Preserve the safe direction: acquire the lease lock; reclaim the
  worktree; tombstone the filesystem record; call bound `retire_lease_lane(lease_id)`; only then
  durably record and reply success. A false/zero-row retire is a named mismatch, never silent
  success. A row-delete failure returns `worktree-lane-release-store-failed`; the
  already unavailable filesystem lease prevents execution while the next reconcile retries the
  delete.
- **Reconciliation owns every partial state.** Extend the existing startup/operation reconcile
  (`bridge.py:1871-1884`; `worktree_lease.py:234-280`) so, for this consumer identity:
  armed filesystem + matching row/lane stays armed; armed filesystem + missing/mismatched row is
  reclaimed and tombstoned; row + no armed filesystem record is deleted; tombstoned/expired/
  missing-registration records have their row deleted. A database failure stops startup or the
  lifecycle operation; it never becomes "probably gated" or "probably exempt". This rule is safe
  **only after reconcile acquires the same per-lease lock**. Lock-busy means an arm/release is
  in-flight and is non-actionable for that pass; it is not evidence of an orphan. The heartbeat
  timer at `bridge.py:963-965`, startup, and operation-triggered reconciliation all use this one
  implementation.
- **Arm replay is idempotent by request id.** Persist `arm_request_id=envelope.id` in the
  filesystem record. Before minting, an arm replay searches that consumer's records: a valid
  record+row pair returns the original deterministic success fields; a tombstoned/retired partial
  returns its recorded refusal and never mints a replacement for the same request id; a durable
  task result is replayed unchanged. Inject crashes after filesystem publication, row arm, status
  write, result write, and before reply. Every replay must return the original pair or a closed
  failure after retiring the unacknowledged lease—never a second lease.
- **Lane choice is server-side and one posture per seat.** `worktree_arm`'s closed schema is
  enforced at `bridge.py:1921-1923`; do not add a lane field there or to either dispatcher arm
  payload. Read `BRIDGE_WORKTREE_LANE` only from supervisor process configuration, default it to
  `gated`, validate exactly `gated|exempt`, and persist the chosen lane in the filesystem record.
  An exempt seat must also pass push-less readiness before it can register or arm. Dispatchers
  choose a suitably provisioned target seat; they never assert lane membership.
- **Push-less means a real target-remote denial, with an honest residual.** Worktrees share the
  common Git config and the operator's writable SSH setup, as the observed prerequisite proves.
  The exempt credential is one manually provisioned GitHub machine user, `arb-exempt-bot`, with
  one SSH key. It is **not** a per-repository deploy-key set and **not** a fine-grained PAT.
  [GitHub's machine-user guidance](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys#machine-users)
  names a machine user plus one SSH identity as the multi-repository automation shape, while
  [fine-grained PAT guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens)
  limits each token to resources owned by one selected user or organization. That token therefore
  cannot span both `example-org` organization repositories and personal-account repositories.

  The owner grants `arb-exempt-bot` membership in the paid `example-org` organization and its
  `arb-exempt-readonly` team, whose target-repository access is exactly Read; organization base
  permissions and any other team/direct grants must not widen that effective role. Non-org
  repositories may use only an exact read-only role. Current GitHub private personal repositories
  do not have such a collaborator role—[personal-repository collaborators can push](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)—so
  a private personal repository such as `markgerrard/ARB` must move into an
  organization and receive the Read role before it may use the exempt lane. Adding the machine
  user as a write-capable personal collaborator and calling it “Read” is forbidden. A public
  personal repository needs no collaborator grant for fetch; its no-grant push denial still must
  pass the live classifier.

  For every exempt arm, resolve the target repository from the worker's actual checkout before
  applying any override. Read that checkout's effective `origin`, reject missing, ambiguous,
  option-shaped, NUL/newline, non-GitHub, or uncanonicalizable values, and normalize the supported
  GitHub SSH/HTTPS forms to the exact target repository's SSH URL. Never consult a fleet-wide
  `BRIDGE_EXEMPT_GIT_REMOTE_URL`, the ARB repository, or a runbook default as remote authority.
  Enable Git's worktree config, set that worktree's `origin` fetch URL and push URL to the resolved
  target SSH URL, and set worktree-local `core.sshCommand` to the machine-user key with
  `IdentitiesOnly=yes`. The common checkout's `origin` remains unchanged.

  Registration preflight resolves the same target from the seat's actual checkout; arm resolves it
  again from the newly created target worktree. Both first prove the configured SSH identity is the
  recorded `arb-exempt-bot` key, then prove `git ls-remote origin` succeeds for that exact target,
  then prove
  `git push --dry-run origin HEAD:refs/heads/arb-exempt-deny-proof-<nonce>` is rejected by the
  target remote for permission—not merely nonzero because DNS/network is down. A target for which
  the machine user lacks Read is `exempt-remote-read-unavailable`: registration/arm hard-refuses
  and never retries with an operator credential, SSH agent, credential helper, alternate URL, PAT,
  or per-repository deploy key.

  Classification is an allowlist, not “any nonzero.” For each actual GitHub target remote, accept
  only the runbook-captured machine-user Read-role permission-denial signature (normalized exact
  lines plus exit class) after the exact identity/repository `ls-remote` succeeded. Keep explicit
  fixtures that do **not** match: `Permission denied (publickey)`/repository-not-found
  authentication, DNS/timeout/refused/reset network failures, archived/read-only repository state,
  protected-branch or pre-receive-hook rejection, and generic `remote rejected`. A dry-run exit 0
  is `exempt-push-credential-writable`, logs loudly, writes no lane row, and blocks arming and
  rollout.

  This provably blocks the ordinary `git push`/`git push origin` path from that worktree with its
  configured target remote and machine-user credential. It does **not** stop a same-UID process
  from rewriting worktree config, naming a writable target URL directly, or using the operator's
  ambient SSH agent/credential helper. Filesystem modes cannot remove those authorities from a
  same-user process. That is the §9.3 honest-drift residual. A future stronger deployment uses a
  separate OS account/container with no writable Git credential; do not claim this slice already
  provides it.
- **The ref wire has a versioned mixed-fleet transition.** Final ordinary `request` and
  `worktree_run` envelopes use the exact
  `payload.task={"artefact_id":<nonblank string>,"version":<positive int>}` shape at
  `envelope.py:61-67`; lifecycle operations remain taskless. First ship dual acceptance while gate
  is off and advertise `task_wire=legacy-or-ref-v1`. That value proves only envelope parsing; it
  does not authorize ref execution or emission. Stage 1d-iv is receive-only: the send authority
  emits legacy for a target that lacks `brief_hydrate=v1`, including a target that advertises
  dual parsing. If an independently produced/canary ref reaches a parse-only seat, the bridge
  returns exact `brief_hydration_unavailable` before `build_task_prompt` or engine start; an object
  task is never formatted, concatenated, or stringified into model input. Stage 1d-v advertises
  `brief_hydrate=v1` only after the helper, local-reader check, pointer prompt, receipt requirement,
  and named refusal path execute successfully. The authority emits a ref only when the frozen
  target advertises both `task_wire=legacy-or-ref-v1` and `brief_hydrate=v1`; it never sends an
  object to an old or parse-only bridge. After every target advertises both capabilities, every
  sender is migrated, and ref emission/hydration is observed fleet-wide, set
  `BRIDGE_TASK_REF_REQUIRED=1` seat-by-seat, advertise `task_wire=ref-required-v2`, and reject
  strings with `invalid-payload-task-ref`. Only after fleet telemetry/test fixtures show zero
  legacy sends is the string branch removed and the dedicated flag retired. This ordering is
  mandated by both remediation briefs; the claim-gate flag is not used as a wire-version flag.
- **Malformed refs die before the gate.** `Envelope.from_json` runs first at
  `bridge.py:1204-1209`; therefore a malformed task ref logs
  `envelope-invalid invalid-payload-task-ref`, sends no engine work, and never touches the claim
  resolver or lane writer. CLI clients must reject the same malformed shape before enqueue, so the
  normal operator sees an immediate exit 2 rather than a dispatcher timeout. Tests assert this
  exact reason, not a bare rejection.
- **Store-before-send has one enqueue authority and a closed publish-credential boundary.** Add
  `dispatch_authority.publish_and_enqueue(source, *, target_agent_id, ...)` as the only ordinary
  request enqueue seam, where `source` is exactly one of: a FABA-driver-owned draft, or a
  pre-minted `{artefact_id, version}` plus a `HarnessPublishReceipt` and the exact original brief
  bytes while legacy fallback exists. It freezes the target
  registry record and validates the brief/receipt against that target. In FABA draft mode, only
  the short-lived FABA driver calls the existing `publish_artefact_and_gate` machinery at
  `tools/faba/faba_launch.py:434-582` using its injected `ARB_MEMORY_REDIS_URL`. In pre-minted mode,
  the seam performs no publish: it verifies that the separate FABA-driver receipt binds the exact
  ref, target id, target registry generation, and `worker_vantage`; stale or retargeted receipts
  require a new publish. The receipt also binds the store's domain-separated content hash; before
  any legacy emission, the authority hashes the supplied original bytes and requires an exact
  match. It then chooses the target-compatible wire shape and calls Redis enqueue exactly once.
  The bytes are never included in ref emission or returned metadata. Its return is pointer-only
  metadata.

  Caller ownership is closed: FABA uses draft mode inside its driver process; Bash CLI, Go, and
  `ctl` have no harness write credential and must receive the ref+receipt from the separate
  `arb-memory-harness-publish` FABA-driver step and may provide their original brief bytes only for
  receipt-hash-verified legacy fallback; wiki refresh, learn intake, diagnose, seat E2E, and every
  other non-FABA in-process caller follow the same pre-minted route. All then invoke
  `publish_and_enqueue`; their raw ordinary-request `rpush` and publish paths are removed or
  limited to lifecycle/control. The warm orchestrator may request the FABA-driver publish and
  pass on its pointer receipt, but it never imports/calls `memory_write`, `store_artefact`,
  `publish_artefact_and_gate`, or `psycopg` and never writes `artefacts` directly. No broker or
  implicit client credential fallback is permitted.
- **The assumptions section is structured but not falsely exhaustive.** A dispatch brief contains
  a `## Assumptions` JSON block with `{"items":[]}` for an explicit no-precondition claim, or items
  with exact keys `statement`, `status`, and `vantage`; `status` is
  `demonstrated|assumed`. Demonstrated items additionally require an exact
  `artefact_id`/positive `version`; assumed items must not carry a demonstration ref. A
  demonstration's nonblank vantage must match the **selected target's registry-advertised worker
  vantage at dispatch time** to count as demonstrated; publish-time caller input is not authority.
  `BRIDGE_WORKER_VANTAGE` is a required nonblank supervisor value advertised as
  `worker_vantage`; it names the concrete execution environment against which assumptions were
  demonstrated and has no dispatcher override.
  The FABA publish receipt binds the target id, registry generation, and vantage used for
  validation. The authority rechecks that the frozen target record/capabilities are still current
  immediately before enqueue. A retarget or generation/vantage change requires revalidation and
  republishing; it cannot reuse the first target's receipt. The validator proves presence and
  shape, not completeness; omitted real assumptions remain a review/judgment residual exactly as
  spec §5.2 states.
- **A store outage stops every ordinary dispatch at the real enqueue.** If brief validation,
  FABA-driver publish, pre-minted receipt validation, target recheck, or wire selection fails,
  `dispatch_authority.publish_and_enqueue` must not call the Redis enqueue primitive—for gated and
  exempt work alike. Instrument that primitive, not an injected wrapper, and assert zero calls.
  The named mutation enqueues and then raises; the test must fail because observing any enqueue is
  failure even though the harness ultimately raised. A receipt-confirmed ref cannot be replayed
  through a raw edge because no such ordinary edge remains.
- **The worker, not the bridge, hydrates.** The bridge turns the ref into a pointer-only instruction
  requiring the worker to run `arb-memory-brief-hydrate` with the exact id/version. The helper runs
  inside the worker tool environment, calls `local_read_dsn(os.environ)` at
  `src/arb_memory/local_read_policy.py:9-23`, fetches the exact artefact version, and computes
  `arb_memory.hash.artefact_hash(content, content_bytes, content_mime)` from
  `src/arb_memory/hash.py:4-12`; that domain-separated
  `sha256("arbmem:artefact:v1\\0" || mime || "\\0" || kind || "\\0" || payload)` must equal the
  fetched row's `content_hash`. Raw SHA-256 of the body is wrong and has its own RED mutation.
  Stage under a bridge-created mode-0700 `mkdtemp` beneath an operator-owned runtime root, using
  constant filenames unrelated to `artefact_id`, O_EXCL/atomic writes, mode 0600 files, and
  unconditional `finally` cleanup for every helper/engine/receipt exception. Traversal-shaped ids
  never become path components. The bridge may parse the receipt and remove the stage directory;
  it must never connect to or select from `artefacts`, read the staged body, or widen
  `arb_gate_reader`. Missing helper, local read DSN, artefact, version, hash match, or receipt
  yields exact `brief_hydration_*` failure before a successful turn can be reported. Before
  hydration readiness exists, any syntactically valid object task yields
  `brief_hydration_unavailable` before prompt construction; the guard remains after rollout as a
  fail-closed defense against stale or forged capability state.
- **The immutable ref binds gate and worker.** The claim gate evaluates the envelope carrying the
  exact ref; the worker receipt must repeat the same id/version and stored hash. Tests use a body
  sentinel and prove it is absent from the request envelope while the hydrated file hashes to the
  stored version. A newer version published after enqueue must not replace the pinned version.
- **Resolution is audit, not authority, through one entry point.** Public
  `claim_gate.evaluate` returns the refusal/admit outcome plus the posture/lane/claim facts read
  during that one evaluation. Migrate every caller and remove `claim_gate.check`; no bridge code
  may call raw resolver admission methods outside `evaluate`. The bridge persists the evaluation
  metadata, immutable brief ref, and hydration receipt as a dispatch audit event. No request field
  accepts that record, no later admission reads it, and Slice 2's close contract is explicitly
  `fresh resolver calls only`. Tests statically enumerate bridge call sites and dynamically route
  every gated operation through an instrumented `evaluate`; an attempted unaudited resolver path
  fails. A second test changes live facts after a recorded admission and proves the next
  evaluation refuses despite injected stale audit. The published brief bytes are never rewritten.
- **Depend on Slice 1c's public boundary only.** The parallel remediation may change resolver
  coverage internals. This slice may call only the public constructor, `assert_ready()`,
  `seat_requires_claim_ref()`, `lease_lane()`, `claim()`, `close()`, and the gate seam at
  `bridge.py:1244-1271`. Any additional 1c behavior becomes a named contract obligation, not an
  import from its private vocabulary or coverage table.
- **Fleet enablement remains fail-closed and doubly sequenced.** `BRIDGE_CLAIM_GATE=0` and
  `BRIDGE_TASK_REF_REQUIRED=0` remain code defaults. First deploy dual-accept and prove
  `legacy-or-ref-v1` on every target while authority emission remains legacy; then migrate and
  prove every sender uses the single authority. Next deploy worker hydration, require executed
  helper readiness before each seat advertises `brief_hydrate=v1`, and prove the authority emits
  refs only to targets carrying both advertisements. Only after both advertisements and successful
  ref hydration are fleet-wide may the ref-required canary roll
  `BRIDGE_TASK_REF_REQUIRED=1`. Gate-on startup additionally requires reader ready, bound per-seat
  lane writer ready, `bus-and-gate-daemon-creds-v2` proved before registration, two-record
  reconcile clean, server lane valid, hydration ready, and—for exempt seats—the real
  actual-target resolution plus machine-user fetch-positive/classified push-denied proof. A target
  without machine-user Read hard-refuses rather than borrowing operator credentials. Prove exempt
  arm/run/release end to end before canary `BRIDGE_CLAIM_GATE=1`. Remove legacy acceptance only in
  a later wave after zero-legacy proof. Never flip either flag merely because unit tests pass.
- **Every planned refusal pins its code/message and every test can go RED.** Default-deny tests
  assert the exact `worktree-lane-*`, `invalid-payload-task-ref`,
  `brief_hydration_unavailable`, or other `brief_hydration_*` reason.
  Every mutation injection below must fail for the behavior it names, then be reverted before
  commit. A skipped live run, a bare `ok is False`, or a generic nonzero command proves nothing.
- **Do not modify** `tests/arb_memory/test_schema.py`,
  `tests/arb_memory/test_gate_schema_deny_proof.py`, `opus-5-orch-log.md`, or
  `fable-5-orch-log.md`.

## Remainder of Slice 1

These spec §10 items are deliberately not implemented by 1d. They remain explicit contract
obligations, not silently dropped:

| Follow-on | Contents | Dependency/placement |
|---|---|---|
| **Slice 1e — probe-package artefact** | Typed package schema containing probe source, fixtures, seed/harness config, and red run log; tombstone-surviving re-execution and gated re-landing path (§6, §9.2). | Starts from 1d's exempt worker + artefact hydration, but owns package semantics and permanent regression-test re-landing. |
| **Slice 1f — verifier assignment and family advertisement** | Consumer-side unpredictable verifier assignment, actual family advertisement/provenance, and outcome family mismatch refusal (§7.3). | Consumes Slice 1b claim/attestation schema; does not widen the 1d lane writer. |
| **Slice 1g — verifier harness re-run and F4 consumer check** | Harness-produced verifier re-run artefact plus consumer-side allowlisted-harness-author check before attestation INSERT (§7.1 F2/F4). | Owns the attestation write path and harness-identity allowlist; 1d merely supplies ref-only briefs/hydration. |
| **Slice 1h — Devin named refusal** | Replace `devin_acp.py:144`'s set-model failure fallback with a named refusal so configured family cannot silently diverge from the running family (§7.3). | Engine-adapter change, independent of lane/brief mechanics. |

Also out of scope:

- **Slice 2:** sampler and close-time fresh re-resolution, including the expired-claim case. This
  plan defines the audit-not-authority contract only.
- **Result-delivery admissibility preview:** remains in `docs/BACKLOG.md`; no reply-frame or
  delivery gate is added.
- **Slice 3:** probe re-landing convention and standing mutation gate.

The §10 hard rule still applies: the claim gate cannot ship without 1d's working exempt lane.
Fleet-wide production enablement should additionally wait for 1e–1h so the full Slice 1
confirmation/verifier path exists; a 1d canary is permitted only with explicitly seeded viable
claim data and the end-to-end exempt escape path proven.

## File Structure

- **Create `src/agent_redis_bridge/lane_writer.py`** — separate connection, closed privilege
  coverage, readiness, bound arm/retire/list function calls, reconnect, and cleanup.
- **Modify `src/arb_memory/schema.sql` and `src/arb_memory/mcp/grants.py`** — owner-only
  role→consumer/lane binding, three hardened functions, per-seat isolation assertion, and exact
  function grants.
- **Modify `src/arb_memory/run.py`** — optional deployment application for the separately
  provisioned per-seat lane-writer role and binding.
- **Create `tests/test_lane_writer.py`** — fake-connection mapping/recovery/readiness/coverage.
- **Create `tests/arb_memory/test_lane_writer.py`** — live least-privilege positive and
  parametrized deny-proof, binding, and cross-seat matrix.
- **Modify `tests/arb_memory/test_gate_grants.py` and
  `tests/arb_memory/test_run_grants.py`** — deployed reader/writer cross-denials and command path.
- **Modify `src/agent_redis_bridge/engines/_stdio.py`,
  `src/agent_redis_bridge/engines/agent_sdk_models.py`,
  `src/agent_redis_bridge/engines/agent_sdk.py`, and
  `src/agent_redis_bridge/engines/agent_sdk_session.py`** — central reader/writer daemon-secret
  predicate, Popen removal, Agent SDK blanking/final assertion, and session/transcript redaction.
- **Modify `tests/test_stdio_child_env.py`, `tests/test_agent_sdk_models.py`,
  `tests/test_agent_sdk_session.py`, `tests/test_bridge_identity.py`,
  `tools/faba/subagent/bridge_round.py`, and `tools/faba/tests/test_bridge_round.py`** — real
  subprocess probes per spawn family and `bus-and-gate-daemon-creds-v2` advertisement parity.
- **Modify `src/agent_redis_bridge/worktree_lease.py`** — persist lane on filesystem records and
  persist `arm_request_id`, split mint/publication, and expose deterministic locked reconciliation.
- **Modify `src/agent_redis_bridge/bridge.py`** — lane-writer lifecycle, two-record compensation,
  replay/locking, server-side lane, push-less readiness, transition capability, one gate entry,
  and worker hydration receipt.
- **Modify `tests/test_bridge.py` and `tests/conftest.py`** — process-secret construction,
  no-fallback, readiness/startup order, cleanup, and ambient env scrubbing.
- **Modify `tests/test_bridge_worktree_lease.py`** — atomic arm/release/reconcile and exempt
  push-deny integration.
- **Create `src/agent_redis_bridge/exempt_git.py` and `tests/test_exempt_git.py`** — worktree-local
  actual-checkout target resolver, one-machine-user SSH configuration, per-target
  fetch-positive/push-denied classification, and no-operator-fallback proof.
- **Create `docs/runbooks/exempt-seat-machine-user.md`** — one-time owner setup for the single
  `arb-exempt-bot` account/SSH identity, `example-org` Read-team membership, private-personal-repo
  transfer requirement, known-host/mode setup, per-target provisioning ledger and live
  identity/read/push-denial proof, paid-seat record, rotation, and revocation. The implementation
  does not create the account or make GitHub grants.
- **Create `src/arb_memory/brief_hydrate.py` and `tests/arb_memory/test_brief_hydrate.py`** — exact
  ref fetch/domain-separated hash/mode-0700 stage/receipt/cleanup through the local reader.
- **Modify `pyproject.toml`** — install `arb-memory-brief-hydrate`.
- **Create `src/agent_redis_bridge/dispatch_authority.py` and
  `tests/test_dispatch_authority.py`** — single target-bound validate→publish→receipt→enqueue seam,
  FABA-draft versus pre-minted-ref source types, dispatch-time vantage, dual+hydration capability
  selection, credential deny proofs, and zero-enqueue proof.
- **Create `scripts/arb-memory-harness-publish`** — short-lived FABA-driver-only publish step that
  receives harness write material, validates against a frozen target, and emits a target-bound
  pointer receipt containing the published domain hash for non-FABA callers; it never enqueues an
  ordinary request or returns credentials/body bytes.
- **Modify `tools/faba/faba_schema.py`,
  `tools/faba/subagent/run_author_round.py`, and `tools/faba/faba_launch.py`** — typed dispatch brief
  and publisher used only inside the send authority.
- **Modify `tools/faba/tests/test_faba_schema.py` and
  `tools/faba/tests/test_author_round_guard.py`; create
  `tools/faba/tests/test_dispatch_brief.py`** — assumptions schema, receipt, and outage stop.
- **Modify `src/agent_redis_bridge/envelope.py`,
  `tests/test_envelope.py`, and `tests/test_envelope_claim_fields.py`** — versioned dual-accept,
  ref-required, then ref-only task shape.
- **Modify `src/agent_redis_bridge/redis_io.py` and `tests/test_redis_io.py`** — register and read
  `task_wire`, separately proved `brief_hydrate`, supervisor-owned `worker_vantage`, and the seat
  generation used for target freeze.
- **Modify `scripts/agent-dispatch`, `scripts/dispatch-dev`,
  `tests/test_agent_dispatch.py`, `src/agent_redis_bridge/ctl.py`, and
  `tests/test_ctl_worktree.py`** — accept only a pre-minted ref+receipt for ordinary non-FABA
  dispatch, delegate enqueue to the authority, and retain only narrow lifecycle/control edges.
- **Modify `tools/go-client/envelope.go`, `build.go`, `main.go`, Go tests, and golden envelopes** —
  accept only a pre-minted ref+receipt for ordinary dispatch, delegate enqueue to the authority,
  and keep byte-identical lifecycle/control edges.
- **Modify `README.md`, `docs/fragments/dispatch-recipe.md`,
  `skills/using-agent-bridge/SKILL.md`, `docs/orchestrator-patterns.md`,
  `src/agent_redis_bridge/wiki_refresh.py`, `src/agent_redis_bridge/learn_intake.py`,
  `skills/diagnose/panel.py`, `scripts/arb-memory-seat-e2e`, and `scripts/check-doc-drift`** —
  migrate every production recipe/caller and keep generated fragments in sync.
- **Modify string-task fixtures including `tests/test_bridge_worktree.py`,
  `tests/test_bridge_parallelism.py`, `tests/test_envelope_run_id.py`, and
  `tests/test_push_task_event_tee_integration.py`** — exercise dual compatibility and final refs
  without leaving a hidden legacy sender.
- **Modify `src/agent_redis_bridge/claim_gate.py`, `tests/test_claim_gate.py`,
  `tests/test_bridge_claim_gate.py`, and relevant task-event tests** — one-pass evaluation audit
  that is never authority.
- **Modify `scripts/seat-preflight`, `tests/test_seat_preflight.py`, `deploy/.env.example`, and
  `deploy/README.md`** — scrub capability, per-seat writer, push-less, hydration, wire-transition,
  and fleet rollout gates.

---

## Stage 1d-i — Per-seat writer authority and daemon-secret containment

**Dispatch boundary:** implement only Task 1. Review its live PostgreSQL authority and every child
spawn family before Stage 1d-ii. No worktree lifecycle change belongs in this dispatch.

### Task 1: Provision the bound writer and prove daemon-secret containment

**Files:**
- Create: `src/agent_redis_bridge/lane_writer.py`
- Modify: `src/arb_memory/schema.sql`
- Modify: `src/arb_memory/mcp/grants.py`
- Modify: `src/arb_memory/run.py`
- Create: `tests/test_lane_writer.py`
- Create: `tests/arb_memory/test_lane_writer.py`
- Modify: `tests/arb_memory/test_gate_grants.py`
- Modify: `tests/arb_memory/test_run_grants.py`
- Modify: `src/agent_redis_bridge/engines/_stdio.py`
- Modify: `src/agent_redis_bridge/engines/agent_sdk_models.py`
- Modify: `src/agent_redis_bridge/engines/agent_sdk.py`
- Modify: `src/agent_redis_bridge/engines/agent_sdk_session.py`
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `tests/test_stdio_child_env.py`
- Modify: `tests/test_agent_sdk_models.py`
- Modify: `tests/test_agent_sdk_session.py`
- Modify: `tests/test_bridge_identity.py`
- Modify: `tests/test_bridge.py`
- Modify: `tools/faba/subagent/bridge_round.py`
- Modify: `tools/faba/tests/test_bridge_round.py`
- Modify: `scripts/seat-preflight`
- Modify: `tests/test_seat_preflight.py`

**Interfaces:**
- Produces:
  `PsycopgLaneWriter(dsn, *, expected_role, expected_consumer_id, expected_lane,
  schema="public", connect=psycopg.connect)` with `assert_ready()`, `arm(lease_id)`,
  `retire(lease_id)`, `rows()`, and `close()`.
- Produces: owner-only `lane_writer_bindings`; `arm_lease_lane(text)`,
  `retire_lease_lane(text)`, and `list_lease_lanes()`; `assert_gate_lane_writer_isolation`; and
  `apply_gate_lane_writer_grants`.
- Produces: `bus-and-gate-daemon-creds-v2` plus a selected-engine pre-registration self-check.
- Consumes: stable Slice 1b `lease_lanes(lease_id, lane, armed_by, armed_at)`.

- [ ] **Step 1: Write the bound-function and privilege-coverage tests**

Create closed PostgreSQL-17 relation/column/function privilege vocabularies and a
`LANE_WRITER_PRIVILEGE_COVERAGE` covering all gate tables/views, the binding table, and all gate
functions. Unit tests require the only positive runtime authorities to be:

```python
assert allowed_pairs == {
    ("FUNCTION", "arm_lease_lane(text)", "EXECUTE"),
    ("FUNCTION", "retire_lease_lane(text)", "EXECUTE"),
    ("FUNCTION", "list_lease_lanes()", "EXECUTE"),
}
assert not unclassified_pairs
```

Pin the three SQL signatures and their security properties: `session_user` binding, fully
qualified objects, fixed `search_path`, no identity/lane parameter, exact bound-consumer DELETE,
and list filtering. Pin `src/arb_memory/schema.sql` as the only function-body source: an AST/static
test forbids `CREATE FUNCTION`/`CREATE OR REPLACE FUNCTION` strings in `lane_writer.py`,
`grants.py`, `run.py`, or test helpers, and runtime `p_lease_id` calls must use bound query
parameters rather than interpolation. `PsycopgLaneWriter` calls only those functions. Cover
duplicate key, unbound role, connection failure/discard/reconnect, close,
expected-role/binding/lane mismatch, and every readiness class.

- [ ] **Step 2: Run unit tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/test_lane_writer.py -q
```

Expected RED (prediction): the helper/functions and coverage do not exist.

- [ ] **Step 3: Write live cross-seat authority kills**

Provision fresh seat-A/seat-B logins, an independent `NOLOGIN` function owner, and bindings with
different consumer ids/lanes. Build the scratch schema by reading the checked-in
`src/arb_memory/schema.sql` bytes that the production schema path applies, then run the real grants
apply path; no Python-authored test function may substitute. Positive assertions:

- each writer passes `assert_ready()` only for its exact authenticated role/binding;
- A arms only an A/gated row, B arms only a B/exempt row;
- A lists only A and cannot retire B; B cannot retire A;
- raw table/binding SELECT/INSERT/DELETE and forged lane/identity calls fail (`42501` or exact
  function-signature absence);
- reader still selects the three gate views and cannot execute writer functions or DML.

Parametrize the lane-writer refusal KILL over every forbidden effective privilege:

- every PostgreSQL-17 relation/column privilege on all gate relations and the binding table;
- applicable column-level SELECT/INSERT/UPDATE/REFERENCES grants;
- every privilege on `claims`, `attestations`, `seat_posture`, and all three views;
- non-allowlisted function EXECUTE, PUBLIC EXECUTE, membership/`SET ROLE`, function/relation
  ownership, and login as the function owner.

Each case first proves the injected privilege is effective, then requires
`assert_ready()` to raise a message naming the exact relation/privilege/channel. Independently
mutate `retire_lease_lane` to drop its bound-consumer predicate and require A's attempt against B
to make the test RED; restore it before proceeding.

Extend `python -m arb_memory grants` with required per-seat
`ARB_GATE_LANE_WRITER_ROLE`, `ARB_GATE_LANE_WRITER_CONSUMER_ID`, and
`ARB_GATE_LANE_WRITER_LANE`. Missing, shared, duplicate, or unisolateable roles abort before
commit. The command creates no login or secret.

- [ ] **Step 4: Write child-secret, transcript, and advertisement tests**

The central predicates must classify reader/lane-writer DSN/role keys and harness publish write
material. Add the lane DSN, a harness-publish `ARB_MEMORY_REDIS_URL` sentinel, and a sentinel
`ARB_MEMORY_LOCAL_DSN` to real subprocess probes for every current Popen adapter family: Codex,
Gemini ACP, Grok ACP, Cursor ACP, Devin ACP, pi RPC, pi SDK, and agy-print. Each subprocess prints
presence booleans only; require hydration present and lane writer/publish material absent. For
Agent SDK, run keyed and subscription final merged overlays in real Python subprocesses; require
hydration present and writer/publish material blank/absent. A dedicated short-lived FABA-driver
probe is the only positive control: it receives the publish sentinel while the author/reviser
child it launches does not.

Session-store, stderr, and structured transcript tests inject the literal DSN and variable name
and require `[REDACTED]`. Bump/mirror `ENV_SCRUB_CAPABILITY`; preflight invokes the selected-engine
self-check and bridge construction refuses before `register()` when it is absent/stale. A
declarative registry string without the executed self-check is not sufficient.

- [ ] **Step 5: Run all Stage 1d-i tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_lane_writer.py \
  tests/arb_memory/test_lane_writer.py \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_run_grants.py \
  tests/test_stdio_child_env.py \
  tests/test_agent_sdk_models.py \
  tests/test_agent_sdk_session.py \
  tests/test_bridge_identity.py \
  tests/test_bridge.py \
  tests/test_seat_preflight.py \
  tools/faba/tests/test_bridge_round.py -q
```

Expected RED (prediction): bound functions, lane-writer scrub, v2 capability, and pre-registration
self-check do not exist.

- [ ] **Step 6: Implement the authority and one central scrub**

Use an autocommit, locked, daemon-scoped connection with the same bounded connect/query/dead-idle
failure posture as the public Slice 1c resolver contract, but do not import its private constants.
Any `psycopg.Error` closes/discards and raises `LaneStoreUnreachable`; no in-operation retry.

Harden the three functions exactly as specified above. `apply_gate_lane_writer_grants` revokes all
effective direct/PUBLIC relation and function access, grants only schema USAGE plus three EXECUTEs,
and writes the owner-only binding as an owner transaction. Function creation/replacement comes
only from the checked-in `schema.sql` bytes used by the production apply path; neither grants nor
runtime Python formats function bodies or interpolates lease ids. Readiness derives all positive
and negative checks from the coverage table and calls a binding-safe function probe.

Rename the reader-specific predicate to a gate-daemon predicate and reuse it everywhere named in
the file list. Extend Agent SDK `_scrub_material()` with both lane secret values and variable
names. The pre-registration self-check must consume the same predicate/overlay builders as real
spawn paths so the capability cannot drift into an unproved label.

- [ ] **Step 7: Verify GREEN and execute the kills**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_lane_writer.py \
  tests/arb_memory/test_lane_writer.py \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_run_grants.py \
  tests/test_stdio_child_env.py \
  tests/test_agent_sdk_models.py \
  tests/test_agent_sdk_session.py \
  tests/test_bridge_identity.py \
  tests/test_bridge.py \
  tests/test_seat_preflight.py \
  tools/faba/tests/test_bridge_round.py -q
```

Expected GREEN (prediction): all pass with zero skips.

Temporarily, one at a time: drop the bound-consumer DELETE predicate; grant a seat raw
`lease_lanes` DELETE; remove the lane-writer keys from the central predicate; skip Agent SDK
blanking; expose the harness publish sentinel to one engine child; move one function body into a
Python f-string containing `lease_id`; and return the v2 capability without running the
self-check. Each named test must RED. Restore, rerun GREEN, and inspect the diff for no mutation
residue.

- [ ] **Step 8: Commit and stop for stage review**

```bash
git add \
  src/agent_redis_bridge/lane_writer.py \
  src/agent_redis_bridge/engines/_stdio.py \
  src/agent_redis_bridge/engines/agent_sdk_models.py \
  src/agent_redis_bridge/engines/agent_sdk.py \
  src/agent_redis_bridge/engines/agent_sdk_session.py \
  src/agent_redis_bridge/bridge.py \
  src/arb_memory/schema.sql \
  src/arb_memory/mcp/grants.py \
  src/arb_memory/run.py \
  tests/test_lane_writer.py \
  tests/test_stdio_child_env.py \
  tests/test_agent_sdk_models.py \
  tests/test_agent_sdk_session.py \
  tests/test_bridge_identity.py \
  tests/test_bridge.py \
  tests/test_seat_preflight.py \
  tests/arb_memory/test_lane_writer.py \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_run_grants.py \
  tools/faba/subagent/bridge_round.py \
  tools/faba/tests/test_bridge_round.py \
  scripts/seat-preflight
git commit -m "feat(claim-gate): bind and contain lane writer"
```

---

## Stage 1d-ii — Locked two-record lifecycle and crash replay

**Dispatch boundary:** start only from a reviewed-green Stage 1d-i. Implement only Task 2. Stage
exit requires the real heartbeat reconcile to lose the injected race harmlessly.

### Task 2: Own the writer lifecycle and make two-record state converge

**Files:**
- Modify: `src/agent_redis_bridge/worktree_lease.py`
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `tests/test_bridge.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_bridge_worktree_lease.py`

**Interfaces:**
- Consumes: process-only `ARB_GATE_LANE_WRITER_DSN`,
  `ARB_GATE_LANE_WRITER_ROLE`, and `BRIDGE_WORKTREE_LANE`.
- Produces: `Bridge.lane_writer`, `Bridge.worktree_lane`, two-record arm/release, and convergent
  startup/heartbeat/operation reconciliation with request-id replay.

- [ ] **Step 1: Write construction/readiness/no-fallback tests**

Prove:

- `BRIDGE_WORKTREE_LANE` defaults `gated` and accepts only exact `gated|exempt`;
- gate-off with no lane-writer DSN preserves old seats until rollout;
- gate-on without lane-writer DSN refuses startup before registration;
- app-env or process `ARB_MEMORY_DSN`, MCP DSN, and reader DSN never substitute;
- the writer is constructed with expected authenticated role, `consumer_id=self.agent_id`, and
  server-side lane, and readiness proves all three match its database binding;
- writer readiness and two-record reconcile precede `register()` and engine start;
- readiness/reconcile failure cleans up both resolver and writer;
- ambient variables are scrubbed from unrelated unit fixtures.

- [ ] **Step 2: Write failing atomicity and reconciliation tests**

Extend `WorktreeLeaseRecord` with `lane="gated"` and `arm_request_id=None` as
backwards-compatible defaults for existing JSON. Split `mint_lease_id()` from durable
`create(lease_id=...)`. Instrument the per-lease lock and fake writer to assert exact states:

| Injection | Required response/state |
|---|---|
| filesystem create fails | no row write attempted; exact existing filesystem error |
| filesystem record durable, row INSERT fails | worktree reclaimed, record tombstoned `lane-row-arm-failed`, exact `worktree-lane-arm-store-failed` |
| row INSERT fails and reclaim/tombstone also fails | exact composite error naming both failures; arm never replies success |
| release reclaim fails | row remains; exact filesystem refusal; no success |
| reclaim+tombstone succeed, row DELETE fails | lease unavailable, row remains for reconcile, exact `worktree-lane-release-store-failed` |
| armed record missing row | reconcile reclaims/tombstones before serve |
| armed record missing row while its lease lock is held | heartbeat/startup reconcile skips it as in-flight; no reclaim/tombstone |
| row missing armed record | reconcile deletes row |
| record/row lanes differ | reconcile makes lease unavailable and returns named mismatch |
| writer store unavailable | startup/operation refuses; never defaults lane |

The success case asserts the function-created row contains the exact bound bridge `agent_id` and
lane. Call order is exactly mint, acquire lock, worktree create, filesystem create, row arm,
status write, result write, reply, unlock. Release similarly holds the lock through reclaim,
tombstone, bound retire, status/result/reply.

- [ ] **Step 3: Write the adversarial heartbeat race and crash-replay matrix**

Use barriers, not sleeps. Pause arm immediately after the real filesystem create and invoke the
real `Bridge.reconcile_worktree_leases()` through the same path used by
`heartbeat_loop` (`bridge.py:963-965`). Required: reconcile observes lock-busy and takes no action;
arm then either completes both records and success or compensates and fails closed. It must never
reply success for a reclaimed/missing worktree. Repeat at row-arm and result-durability barriers.

For one fixed envelope id inject process-shaped crashes after filesystem create, row arm, status
write, result write, and immediately before reply. Replay the same reliable-inbox request. Require:
valid pair→same lease/path/base/expires success; retired/tombstoned partial→same closed refusal;
durable result→same result. Count minted ids and assert one. A new envelope id may create a new
lease; the replay may not.

- [ ] **Step 4: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_bridge.py \
  tests/test_bridge_worktree_lease.py -q
```

Expected RED (prediction): bridge fixtures have no writer/lane lifecycle, arm does not hold a
pre-publication lock, and replay can mint another lease.

- [ ] **Step 5: Implement writer ownership and compensated locked lifecycle**

Instantiate the writer only from process environment. If `BRIDGE_CLAIM_GATE=1`, absence is fatal.
If the writer DSN is present with gate off, readiness and row writes are active so rollout can
populate/prove the lane substrate before enforcement.

Persist `lane` and `arm_request_id` in the filesystem record. Mint first, acquire its lock before
any worktree/filesystem visibility, and keep it until `_operation_result` has durably written
status/result and attempted reply. Add row arm after `WorktreeLeaseStore.create` and before
success. Compensation uses the just-created record, never reloads an untrusted payload. For
release, preserve the same lock through filesystem reclaim, tombstone, row retirement, and result.

Refactor reconciliation only enough to compare `lane_writer.rows()` with
`worktree_lease_store.records()` for this consumer. Do not add a second independent sweep whose
ordering can drift from `reconcile_worktree_leases`. Every missing-row destructive action first
acquires the per-lease lock; busy is non-actionable. Document this condition next to the rule.
Before new arm mint, resolve `arm_request_id` to the prior deterministic result or refusal.

- [ ] **Step 6: Verify GREEN and run failure injections**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_bridge.py \
  tests/test_bridge_worktree_lease.py \
  tests/test_lane_writer.py -q
```

Expected GREEN (prediction): all pass.

Temporarily (one mutation at a time): unlock after filesystem create; make reconcile treat busy as
missing; invoke real reconcile at the barrier; reply before row arm; omit request-id lookup; crash
after row arm; drop arm compensation; reply release success before row retire; and skip orphan-row
retirement. Each focused test must RED. Restore all, inspect `git diff`, rerun GREEN.

- [ ] **Step 7: Commit and stop for stage review**

```bash
git add \
  src/agent_redis_bridge/worktree_lease.py \
  src/agent_redis_bridge/bridge.py \
  tests/test_bridge.py \
  tests/conftest.py \
  tests/test_bridge_worktree_lease.py
git commit -m "feat(claim-gate): atomically arm lease lanes"
```

---

## Stage 1d-iii — Provision and prove the multi-repository push-less exempt credential

**Dispatch boundary:** start only from reviewed-green Stage 1d-ii. Task 3 owns both repository code
and the operator runbook; an assumed machine-user identity or assumed target-repository grant is
not a green exit. This stage writes the runbook and enforcement code only: it does not create
`arb-exempt-bot`, add an organization member, grant repository access, or move a repository.

### Task 3: Make each target checkout's exempt `origin` push-less on this fleet

**Files:**
- Create: `src/agent_redis_bridge/exempt_git.py`
- Create: `tests/test_exempt_git.py`
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `tests/test_bridge_worktree_lease.py`
- Create: `docs/runbooks/exempt-seat-machine-user.md`
- Modify: `scripts/seat-preflight`
- Modify: `tests/test_seat_preflight.py`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: supervisor-only `BRIDGE_EXEMPT_GIT_SSH_COMMAND`, the recorded
  `arb-exempt-bot` public-key fingerprint, and the worker's actual checkout when
  `BRIDGE_WORKTREE_LANE=exempt`.
- Produces: `resolve_target_remote(worktree)` from that checkout's own `origin`, worktree-local
  target `origin`/`core.sshCommand` config using the one machine-user key, and a target-specific
  classified fetch-positive/push-permission-denied proof.
- Does not consume or fall back to `BRIDGE_EXEMPT_GIT_REMOTE_URL`, an ARB-repository constant,
  operator credentials, a PAT, or per-repository deploy keys.

- [ ] **Step 1: Write real-Git unit/integration tests**

Use temporary local repositories and an injectable `run_git`:

- gated lane makes no remote/config change;
- exempt lane refuses missing SSH config, missing/ambiguous `origin`, and any target remote that
  cannot be derived from the worker's actual checkout;
- two target checkouts with different owners/repositories resolve to their own remotes while using
  the same machine-user SSH command and fingerprint; neither may resolve to
  `markgerrard/ARB` unless that is the checkout's actual `origin`;
- `BRIDGE_EXEMPT_GIT_REMOTE_URL`, a runbook repository value, or another checkout cannot override
  the resolved target; a mutation that restores the singleton remote must make this test RED;
- enabling `extensions.worktreeConfig` leaves common `remote.origin.url` unchanged;
- exempt worktree's effective fetch/push `origin` is the normalized SSH URL for that same target
  and its `core.sshCommand` is worktree-local with the one `arb-exempt-bot` key and
  `IdentitiesOnly=yes`;
- the configured key fingerprint and GitHub SSH identity must match the recorded
  `arb-exempt-bot` identity before repository probing;
- each readable target remote plus permission-denied dry-run passes;
- a target repo on which the machine user lacks Read fails `exempt-remote-read-unavailable`, calls
  no operator credential/helper/alternate-URL fallback, and blocks both registration and arm;
- writable dry-run fails `exempt-push-credential-writable`;
- arbitrary nonzero without a recognized remote permission denial fails
  `exempt-push-denial-unproven`;
- GitHub archived/read-only repository, `Permission denied (publickey)`, repository-not-found,
  protected-branch/pre-receive-hook, DNS, timeout, refused, and reset fixtures all fail their
  distinct named class and never count as proof;
- no row arm occurs until this proof passes.

The catalog stores normalized complete line/exit fixtures, not a loose substring such as
`permission denied`. The accepted fixture is captured from the provisioned GitHub read-only
machine-user identity after exact target `ls-remote` identity/repository success. Every fixture is
keyed to the resolved target URL so success or denial for repository A cannot prove repository B.
A command that merely returns 1 is not a deny-proof. A writable control with dry-run exit 0 must
emit the loud
`exempt-push-credential-writable` blocker and prove the writer was never called.

- [ ] **Step 2: Write the owned provisioning runbook and preflight contract**

Name the human owner and `example-org` organization owner/team responsible for the identity. The
runbook describes, but does not execute, this one-time owner setup:

1. manually create the single GitHub machine-user account `arb-exempt-bot`—never automate account
   creation from this repository or implementation;
2. run `ssh-keygen` once for its dedicated SSH identity, add that public key to the machine-user
   account, pin GitHub `known_hosts`, and install the private key with parent directory 0700, key
   0600, and an SSH command containing `IdentitiesOnly=yes`;
3. add `arb-exempt-bot` as a paid `example-org` organization member, create/use the
   `arb-exempt-readonly` team, grant that team exactly Read on every intended org target, and
   verify organization base permission plus any direct/other-team grants do not raise its
   effective access above Read; record the paid organization-seat cost/owner;
4. for non-org targets, grant only an actual Read role. For a private personal-account repository
   such as `markgerrard/ARB`, document that GitHub offers collaborators read/write
   rather than Read: move it into the organization before provisioning, or mark it ineligible for
   exempt work. Never add a write-capable collaborator as a substitute;
5. maintain a provisioning ledger containing every eligible `owner/repo`, current owner
   (organization/public personal), exact grant path/team, machine-user account, public-key
   fingerprint, key label, seat ids/hosts, creation date, review/expiry date, and rotation owner;
6. probe the exact SSH identity and each ledger target, record the normalized fetch-success and
   push-permission-denial evidence, and document rotation, revocation, emergency account/team
   removal, and per-target removal.

State explicitly why the rejected alternatives do not meet the contract: deploy keys are
single-repository credentials, and a fine-grained PAT has one resource owner and would also replace
the required SSH mechanism with HTTPS. Never commit private material.

Preflight derives the target repository from the seat's actual checkout, checks the local
files/modes/fingerprint/identity and matching provisioning-ledger entry, then runs the same
target-specific read-positive/push-classifier proof. A missing ledger entry is not sufficient by
itself to classify Git behavior, but it is independently fail-closed; a missing Read grant,
unreadable target, or writable result blocks exempt registration. It never falls back to operator
credentials. Deployment docs link this runbook rather than saying “with its
supervisor-provided key.”

- [ ] **Step 3: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_exempt_git.py \
  tests/test_bridge_worktree_lease.py -q
```

Expected RED (prediction): no `exempt_git` module, classifier catalog, provisioning runbook, or
preflight proof exists.

- [ ] **Step 4: Implement worktree-local config and proof**

Run Git with explicit `-C <worktree>`. Before changing config, derive `owner/repo` from that
worktree's own effective `origin`; accept only the documented GitHub SSH/HTTPS forms, normalize to
the same repository's SSH URL, and retain the resolved identity with the proof result. Reject
missing/multiple origins, local/file remotes, other hosts, newlines/NUL, option-shaped values, and
owner/repository mismatches. No environment/config singleton supplies the repository.

Set `extensions.worktreeConfig=true` at the repository common-config level once, then use
`git config --worktree` for the resolved target's `remote.origin.url`,
`remote.origin.pushurl`, and `core.sshCommand`. The SSH command uses the one machine-user key with
`IdentitiesOnly=yes`; it does not consult the operator's agent/helper. Verify the effective config
still names the resolved target and recorded key fingerprint before probing. The read probe must
run before the push probe. Generate an unguessable ref suffix; use `--dry-run`; never create a real
remote ref.

Call this after `create_worktree` and before the filesystem lease record is published. On failure,
remove the unleased worktree and return the exact exempt-credential error. The row writer must not
be called. Preflight applies the same resolver/proof to the seat's actual checkout before
registration; arm re-resolves and re-proves the new worktree so a prior target's evidence cannot
carry over. Implement the exact rejection catalog and keep authentication, missing target Read,
network, repository state, hook/protection, permission denial, and writable acceptance as separate
outcomes. Every failure is terminal for exempt work; no branch retries with operator credentials.

- [ ] **Step 5: Verify GREEN, then execute the fleet deny-proof**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_exempt_git.py \
  tests/test_bridge_worktree_lease.py \
  tests/test_seat_preflight.py -q
```

Expected GREEN (prediction): all pass.

On the actual exempt canary seat, after the owner has separately completed the machine-user
runbook, run preflight and arm a throwaway exempt worktree for **each intended target repository**
in the roster; include at least two distinct target remotes so a singleton ARB hardcode cannot
pass. Record:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
git -C <armed-path> ls-remote origin HEAD
git -C <armed-path> push --dry-run origin \
  HEAD:refs/heads/arb-exempt-deny-proof-<nonce>
```

Required observed outcome before enablement: for every target, preflight identifies the same
expected `arb-exempt-bot` key fingerprint and that checkout's own resolved, provisioned,
non-archived repository; `ls-remote` exits 0; push exits with the catalog's exact
permission-denied class. The recorded remote must differ across the multi-repository control.
Deliberately include an unprovisioned target and require the exact read-unavailable hard refusal
with evidence that no operator credential path ran. Also run the documented isolated writable
control and require dry-run ACCEPTED to produce the loud blocker; never grant write to the
machine-user key as that control, and revoke/remove the disposable control afterward.
DNS/auth/hook/archive failure is not acceptable. Release each lease afterward. These are future
deployment observations; do not create the account or pre-fill their result in the implementation
report.

- [ ] **Step 6: Commit and stop for stage review**

```bash
git add \
  src/agent_redis_bridge/exempt_git.py \
  src/agent_redis_bridge/bridge.py \
  tests/test_exempt_git.py \
  tests/test_bridge_worktree_lease.py \
  docs/runbooks/exempt-seat-machine-user.md \
  scripts/seat-preflight \
  tests/test_seat_preflight.py \
  deploy/README.md
git commit -m "feat(claim-gate): enforce exempt push denial"
```

---

## Stage 1d-iv — Mixed-fleet wire and the single dispatch authority

**Dispatch boundary:** start only from reviewed-green Stage 1d-iii. Tasks 4 and 5 are one stage
because the authority and caller migration must land together; the stage remains dual-accept and
does **not** enable ref-required admission, hydration-ready advertisement, ref emission, or claim
enforcement. `task_wire=legacy-or-ref-v1` is receive-only here. The authority must keep selecting
legacy for every target because no Stage 1d-v seat may yet advertise `brief_hydrate=v1`.

### Task 4: Build the target-bound publish-and-enqueue authority

**Files:**
- Create: `src/agent_redis_bridge/dispatch_authority.py`
- Create: `tests/test_dispatch_authority.py`
- Create: `scripts/arb-memory-harness-publish`
- Modify: `src/agent_redis_bridge/redis_io.py`
- Modify: `tests/test_redis_io.py`
- Modify: `tools/faba/faba_schema.py`
- Modify: `tools/faba/faba_launch.py`
- Modify: `tools/faba/subagent/run_author_round.py`
- Modify: `tools/faba/tests/test_faba_schema.py`
- Modify: `tools/faba/tests/test_author_round_guard.py`
- Create: `tools/faba/tests/test_dispatch_brief.py`

**Interfaces:**
- Produces: `validate_dispatch_brief(text, *, target_vantage)`.
- Produces:
  `publish_and_enqueue(source, *, target_agent_id, operation, run_id, worktree_lease=None)`,
  where `source` is a FABA-driver-owned draft or exact
  `{artefact_id,version,HarnessPublishReceipt,legacy_brief_bytes}` during compatibility.
- Produces: `arb-memory-harness-publish --target-agent-id ... --brief ...`, run only as the
  short-lived FABA driver, which outputs a pointer-only target-bound receipt and never enqueues.
- Returns only `{request_id, artefact_id, version, target_agent_id, change_summary}` after enqueue.
- Reuses: `publish_artefact_and_gate` only in the FABA driver; no warm/non-FABA direct
  SQL/store/bus write.

- [ ] **Step 1: Write assumptions-schema tests**

Test valid empty, demonstrated, assumed, and mixed assumptions blocks. Reject missing section,
malformed JSON, unknown keys/status, blank statement/vantage, Boolean/nonpositive version,
demonstrated-without-ref, assumed-with-ref, duplicate keys, and a demonstration whose vantage does
not equal the **selected registry target's advertised vantage** unless it is `assumed`.

Also require a nonblank title and body/instructions after the assumptions block. The validator
does not claim it detected an omitted real precondition.

Write the closed source/identity matrix:

| Caller | Allowed source at `publish_and_enqueue` | Harness publish material |
|---|---|---|
| FABA driver | draft, published internally before enqueue | `ARB_MEMORY_REDIS_URL`, injected only into this short-lived process |
| Bash CLI | pre-minted ref + target-bound receipt + exact receipt-hashed bytes for legacy fallback | forbidden |
| Go client | pre-minted ref + target-bound receipt + exact receipt-hashed bytes for legacy fallback | forbidden |
| `ctl` | pre-minted ref + target-bound receipt + exact receipt-hashed bytes for legacy fallback | forbidden |
| other warm/in-process callers | pre-minted ref + target-bound receipt + exact receipt-hashed bytes for legacy fallback | forbidden |

Reject a draft from every non-FABA identity, a ref without a receipt, a receipt for another
ref/target/generation/vantage/content hash, legacy bytes whose domain hash differs from the
receipt, and any implicit fallback that obtains publish material from the caller's ambient
environment.

- [ ] **Step 2: Write target-freeze and real zero-enqueue tests**

Instrument the Redis `rpush` primitive used by the authority—not a dispatcher callback:

- valid receipt publishes exact bytes, rechecks the same target registry generation, selects its
  compatible wire, enqueues once, and returns pointer-only metadata;
- missing/stale target, blank/missing `worker_vantage`, invalid brief, publish exception,
  failed/wrong receipt, timeout, target generation/vantage/capability change, and store outage call
  `rpush` zero times;
- gated and exempt targets stop identically;
- retargeting A→B reruns vantage validation and publication; A's receipt cannot authorize B;
- FABA draft mode invokes the dedicated driver and publishes before enqueue;
- Bash, Go, `ctl`, and other non-FABA modes accept only a pre-minted ref+receipt and perform zero
  publish calls; their original bytes may feed only the receipt-hash-verified legacy branch;
- the warm/orchestrator path contains no `psycopg.connect`, `memory_write`, `store_artefact`,
  `publish_artefact_and_gate`, direct writer call, or harness publish credential.

- Mutation KILL: enqueue first and raise afterward. The test must RED on the observed enqueue,
  regardless of the raised exception.
- Credential KILL: expose `ARB_MEMORY_REDIS_URL` to a non-FABA caller or engine child, or let Bash,
  Go, or `ctl` pass a draft. The identity/env tripwire must RED before enqueue.

- [ ] **Step 3: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tools/faba/tests/test_faba_schema.py \
  tools/faba/tests/test_author_round_guard.py \
  tools/faba/tests/test_dispatch_brief.py -q
```

Expected RED (prediction): no dispatch authority or dispatch-time target binding exists.

- [ ] **Step 4: Implement the one authority**

Parameterize `publish_artefact_and_gate` with the selected validator rather than duplicating its
publish/receipt machinery. The dedicated `arb-memory-harness-publish` process selects and freezes
the target registry record, derives `worker_vantage` from it (never caller text), validates and
publishes as the harness, and emits a receipt binding ref, target id, owner token/registration
generation, vantage, and domain-separated content hash. `publish_and_enqueue` either invokes that
path from an authenticated FABA driver or accepts its pre-minted output plus the original bytes
from a non-FABA caller; it verifies receipt id/version/content hash,
rechecks the same target generation/capabilities/vantage, builds one envelope, and performs the
only ordinary-request enqueue. It never falls back from missing receipt/credential state. Other
authored stages retain `validate_authored_artefact`.

- [ ] **Step 5: Verify GREEN and prove outage stop**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tools/faba/tests/test_faba_schema.py \
  tools/faba/tests/test_author_round_guard.py \
  tools/faba/tests/test_dispatch_brief.py -q
```

Expected GREEN (prediction): all pass.

Temporarily, one at a time: use caller-supplied vantage; reuse A receipt after retarget to B;
continue after failed receipt; let a Bash/Go/ctl identity publish a draft; leak the publish
sentinel into an engine child; and enqueue then raise. Each named test must RED by observing the
wrong target/vantage, forbidden credential/identity, or nonzero enqueue. Restore and rerun.

- [ ] **Step 6: Hold commit until Task 5 completes**

Task 4 and Task 5 land together so no raw sender exists beside the new authority.

---

### Task 5: Ship dual acceptance and migrate every caller through the authority

**Files:**
- Modify: `src/agent_redis_bridge/envelope.py`
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `src/agent_redis_bridge/redis_io.py`
- Modify: `tests/test_redis_io.py`
- Modify: `tests/test_envelope.py`
- Modify: `tests/test_envelope_claim_fields.py`
- Modify: `scripts/agent-dispatch`
- Modify: `scripts/dispatch-dev`
- Modify: `tests/test_agent_dispatch.py`
- Modify: `src/agent_redis_bridge/ctl.py`
- Modify: `tests/test_ctl_worktree.py`
- Modify: `tools/go-client/envelope.go`
- Modify: `tools/go-client/build.go`
- Modify: `tools/go-client/main.go`
- Modify: `tools/go-client/build_test.go`
- Modify: `tools/go-client/*_test.go`
- Modify: `tools/go-client/testdata/golden/*.json`
- Modify: `README.md`
- Modify: `docs/fragments/dispatch-recipe.md`
- Modify: `skills/using-agent-bridge/SKILL.md`
- Modify: `docs/orchestrator-patterns.md`
- Modify: `src/agent_redis_bridge/wiki_refresh.py`
- Modify: `src/agent_redis_bridge/learn_intake.py`
- Modify: `skills/diagnose/panel.py`
- Modify: `scripts/arb-memory-seat-e2e`
- Modify: `scripts/check-doc-drift`
- Modify: `tests/test_bridge_worktree.py`
- Modify: `tests/test_bridge_parallelism.py`
- Modify: `tests/test_envelope_run_id.py`
- Modify: `tests/test_push_task_event_tee_integration.py`

**Interfaces:**
- Produces: dual `Envelope.from_json` acceptance plus
  `task_wire=legacy-or-ref-v1` registry advertisement.
- Consumes at FABA: a draft plus target inside the credential-bearing driver. Consumes at Bash,
  Go, `ctl`, and every non-FABA ordinary client: a pre-minted ref plus target-bound receipt and
  exact original brief bytes for the temporary hash-verified legacy branch. Every caller
  delegates to `publish_and_enqueue` rather than publishing,
  constructing/enqueuing a request, or acquiring harness write material.
- Produces legacy task strings for old and parse-only targets throughout this stage. The exact ref
  object with no body bytes is selectable only for a future target that also advertises
  `brief_hydrate=v1`; no Stage 1d-iv seat advertises it.
- Produces exact `brief_hydration_unavailable` before prompt construction if a ref nevertheless
  reaches a parse-only seat.

- [ ] **Step 1: Write bidirectional mixed-version tests**

With `BRIDGE_CLAIM_GATE=0` and `BRIDGE_TASK_REF_REQUIRED=0`, new bridges accept exact refs and
legacy nonblank strings for ordinary requests/worktree-run and advertise
`legacy-or-ref-v1`; arm/release remain taskless. Malformed ref objects still fail exactly
`invalid-payload-task-ref`. A syntactically valid ref on a parse-only seat fails exactly
`brief_hydration_unavailable` before `build_task_prompt` and engine start; assert the dict's string
representation never occurs in a prompt/event/transcript. With ref-required on, strings fail
before the gate. Model an old target with no capability: authority emits legacy. Model a
parse-only `legacy-or-ref-v1` target with no hydration advertisement: authority still emits
legacy. Model a future target with both `legacy-or-ref-v1` and `brief_hydrate=v1`: authority emits
ref. It must never emit an object to the old or parse-only target.

Update Bash/ctl/Go tests so ordinary requests delegate to the authority:

- FABA supplies target plus draft inside the driver; Bash/ctl/Go supply target plus pre-minted
  id/version/receipt and the exact original brief path/bytes, which may be consumed only after its
  domain hash matches the receipt;
- lifecycle/control commands retain their closed schemas;
- no ordinary client contains/calls its prior `rpush` implementation;
- no non-FABA caller imports/calls the harness publisher or receives `ARB_MEMORY_REDIS_URL`;
- authority dry-run for a dual+hydration target contains exact ref/no body sentinel, while a
  parse-only target remains legacy;
- mismatched legacy bytes fail before enqueue; matching bytes are used only in the legacy task and
  are absent from ref-mode envelopes/returns;
- worktree-run adds the lease beside the ref; claim/lane metadata stays outside `task`.

- [ ] **Step 2: Write Go parity/golden failing tests**

Remove Go's independent ordinary-request builder/enqueue path and delegate to the same installed
authority command/API. Require byte identity for the lifecycle/control shapes it still owns and
authority-produced ordinary/worktree-run ref goldens. Keep arm/release unchanged.

- [ ] **Step 3: Add the complete caller/recipe migration tripwire**

Migrate, explicitly: README, the shared dispatch recipe fragment, using-agent-bridge skill,
orchestrator patterns, `wiki_refresh`, `learn_intake`, diagnose panel, seat E2E, Bash, dev wrapper,
ctl, and Go. Update `scripts/check-doc-drift`. Update every string-task fixture found by repository
search, including the four named files above.

Add a test/allowlist script that searches production callers and canonical recipes for direct
ordinary-request `rpush` and string-shaped `payload.task`. The only allowed legacy construction is
the compatibility branch inside `dispatch_authority`; every allowance names its removal stage.
The tripwire must fail if any enumerated caller or new caller bypasses the authority. A second
closed allowlist permits harness publish calls/write material only in the FABA driver and
`arb-memory-harness-publish`; it fails if Bash, Go, `ctl`, a warm/in-process caller, or an engine
child gains either.

- [ ] **Step 4: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_agent_dispatch.py \
  tests/test_ctl_worktree.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_bridge_worktree.py \
  tests/test_bridge_parallelism.py \
  tests/test_envelope_run_id.py \
  tests/test_push_task_event_tee_integration.py -q
(cd tools/go-client && go test ./...)
scripts/check-doc-drift
```

Expected RED (prediction): envelope has no versioned dual capability and current callers/recipes
still construct string requests or enqueue independently.

- [ ] **Step 5: Implement compatibility, advertisement, and delegation**

Use a shared Python `BriefRef` value/parser if it reduces duplication between envelope and bridge;
do not create validators with different integer/string rules. Add `task_wire` and
`worker_vantage` registry fields only after dual parsing is active. Authority capability selection
is fail-closed on malformed/stale registry data and requires both
`task_wire=legacy-or-ref-v1` and `brief_hydrate=v1` for ref selection; absent hydration readiness
selects legacy, not ref.

Make wrappers and in-process callers invoke `publish_and_enqueue`; outside the FABA driver they do
not publish, fetch, select vantage, construct ordinary envelopes, or enqueue themselves.
`dispatch-dev` derives default labels from authority metadata rather than grepping prose. In
`Bridge.run_engine`, branch on task type before any call to `build_task_prompt`: a ref without
executed hydration readiness returns `brief_hydration_unavailable`; only a legacy string may reach
the legacy prompt builder in this stage.

- [ ] **Step 6: Verify GREEN, doc parity, and mixed-fleet kills**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_agent_dispatch.py \
  tests/test_ctl_worktree.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_bridge_worktree.py \
  tests/test_bridge_parallelism.py \
  tests/test_envelope_run_id.py \
  tests/test_push_task_event_tee_integration.py -q
(cd tools/go-client && go test ./...)
scripts/check-doc-drift
```

Expected GREEN (prediction): all pass.

Mutate, separately: advertise parse capability without accepting refs; treat parse capability as
hydration readiness; send a ref to an old or parse-only target; stringify a ref into
`build_task_prompt`; let one non-FABA caller publish or inherit write material; make one caller use
direct `rpush`; restore one body recipe; and delete one doc-fragment sync. Each corresponding
mixed-fleet/credential/tripwire/drift test must RED. Restore and rerun. Before proceeding, query
the registry and record that **every target** advertises `legacy-or-ref-v1`; until then all senders
remain in compatibility selection. Even after that proof, Stage 1d-iv remains legacy-emitting
because no target may advertise `brief_hydrate=v1` until Stage 1d-v readiness is live.

- [ ] **Step 7: Commit Stage 1d-iv and stop for review**

```bash
git add \
  src/agent_redis_bridge/dispatch_authority.py \
  src/agent_redis_bridge/envelope.py \
  src/agent_redis_bridge/bridge.py \
  src/agent_redis_bridge/redis_io.py \
  src/agent_redis_bridge/wiki_refresh.py \
  src/agent_redis_bridge/learn_intake.py \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  scripts/agent-dispatch \
  scripts/dispatch-dev \
  scripts/arb-memory-harness-publish \
  scripts/arb-memory-seat-e2e \
  scripts/check-doc-drift \
  tests/test_agent_dispatch.py \
  src/agent_redis_bridge/ctl.py \
  tests/test_ctl_worktree.py \
  tools/go-client \
  tools/faba \
  README.md \
  docs/fragments/dispatch-recipe.md \
  docs/orchestrator-patterns.md \
  skills/using-agent-bridge/SKILL.md \
  skills/diagnose/panel.py \
  tests/test_bridge_worktree.py \
  tests/test_bridge_parallelism.py \
  tests/test_envelope_run_id.py \
  tests/test_push_task_event_tee_integration.py
git commit -m "feat(dispatch): sequence immutable brief references"
```

---

## Stage 1d-v — Worker hydration and single-entry admission audit

**Dispatch boundary:** start from reviewed-green Stage 1d-iv. Dual acceptance remains on; Task 6
does not flip ref-required or claim-gate flags. At entry the authority still emits legacy to every
target. A seat may advertise `brief_hydrate=v1` only after its helper, local-reader policy,
pointer-only prompt, and receipt path pass executed readiness; only then may the authority emit a
ref to that seat. Other seats remain legacy and independently usable.

### Task 6: Hydrate in the worker and persist audit—not authority

**Files:**
- Create: `src/arb_memory/brief_hydrate.py`
- Modify: `pyproject.toml`
- Create: `tests/arb_memory/test_brief_hydrate.py`
- Modify: `src/agent_redis_bridge/redis_io.py`
- Modify: `tests/test_redis_io.py`
- Modify: `tests/test_dispatch_authority.py`
- Modify: `src/agent_redis_bridge/claim_gate.py`
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `tests/test_claim_gate.py`
- Modify: `tests/test_bridge_claim_gate.py`
- Modify: `tests/test_bridge_handle_raw.py`
- Modify: relevant task-event/transcript tests

**Interfaces:**
- Produces: bridge-assigned stage paths consumed by
  `arb-memory-brief-hydrate --artefact-id ID --version N --output PATH --receipt PATH`.
- Produces: `claim_gate.evaluate(...) -> GateEvaluation` as the only admission entry point;
  `check` is removed.
- Produces: pointer-only engine prompt and required worker hydration receipt.
- Produces: `brief_hydrate=v1` registry advertisement only after the exact executed readiness
  check used by the live helper/prompt/receipt path.

- [ ] **Step 1: Write live local-reader hydration tests**

In a scratch schema, store two versions with distinct sentinel bodies. Create an isolated local
reader with `apply_local_reader_grants`, connect the hydrator as that role, and assert:

- exact requested version is staged mode 0600 in a mode-0700 bridge-created directory and
  `artefact_hash(content, content_bytes, content_mime)` equals the fetched `content_hash`;
- receipt contains only id/version/hash/path, never body;
- latest/newer version is ignored when an older version is pinned;
- missing id/version, content-hash mismatch, cross-store policy failure, and DB outage return exact
  `brief_hydration_*` codes and leave no final file/receipt;
- a credential without SELECT on `artefacts` fails;
- `arb_gate_reader` is not used and retains no artefact SELECT.

Add a store-written text artefact for which raw `sha256(body)` differs from `content_hash`.
Replacing `artefact_hash` with raw SHA-256 must make the test RED. Test binary/text kinds and MIME
participation, traversal/option/NUL-shaped ids, symlink destinations, partial-write failure, helper
crash, engine crash, malformed receipt, and cleanup failure. IDs never influence filenames.

- [ ] **Step 2: Write bridge worker-boundary tests**

Use a fake engine that emulates the worker invoking the helper and writing a receipt. Assert the
prompt contains only ref/helper/output paths, no body sentinel. A valid matching receipt permits
the turn; absent/malformed/wrong-ref receipt converts an otherwise-successful engine result to the
exact hydration failure. A syntactically valid receipt hash is audit metadata, not a bridge-side
credential: the helper's live test owns the comparison with the fetched row because the bridge
cannot know that row's hash without violating the no-`artefacts` boundary. Cleanup removes staged
body/receipt and the mode-0700 directory in `finally` after every result/exception; repository diff
checks never see them.

Add a live or process-boundary test where the worker helper uses
`ARB_MEMORY_LOCAL_DSN` while owner and gate-reader DSNs are deliberately poisoned. The bridge
process must not call `store.fetch_artefact` or `psycopg.connect` for hydration.

Start with the Stage 1d-iv parse-only control: with `brief_hydrate` absent, a ref returns
`brief_hydration_unavailable` before `build_task_prompt`/engine start and the ref object's string
form is absent from prompt, event, and transcript captures. Executed helper readiness then enables
`brief_hydrate=v1`; a declarative registry value without that execution is refused. Authority
tests require legacy for parse-only and ref only for parse+hydrate targets, including a
target-generation change between readiness observation and enqueue.

- [ ] **Step 3: Write one-pass resolution-audit tests**

`GateEvaluation.audit` records the facts already read by that evaluation: immutable brief ref,
posture, lease id/resolved lane, claim ref/facts, and decision. It contains no body and no DSN.
Migrate every caller to `evaluate` and delete `check`. Static call-site tests forbid direct
`seat_requires_claim_ref`/`lease_lane`/`claim` admission calls outside `evaluate`; a dynamic bridge
test instruments `evaluate` and requires exactly one call before every gated engine turn.

Bridge tests assert the audit event occurs after envelope/sender validation and with the gate
decision, before engine work. Then:

1. admit and retain the audit record;
2. change resolver facts to unconfirmed/expired;
3. evaluate a new dispatch while injecting the stale audit record where task metadata lives;
4. require exact `unconfirmed_claim`.

There is no code path from audit record to admission. Mutating the bridge to call a raw resolver
method or admit from the prior audit must make one of these tests RED.

- [ ] **Step 4: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/arb_memory/test_brief_hydrate.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_claim_gate.py \
  tests/test_bridge_claim_gate.py \
  tests/test_bridge_handle_raw.py -q
```

Expected RED (prediction): no hydrator/evaluation or executed hydration-ready advertisement
exists, so an object task cannot proceed beyond Stage 1d-iv's named
`brief_hydration_unavailable` guard.

- [ ] **Step 5: Implement worker hydration and audit**

The helper alone opens `ARB_MEMORY_LOCAL_DSN` through `local_read_policy.local_read_dsn`; call
`arb_memory.hash.artefact_hash(content, content_bytes, content_mime)` and compare the result to the
fetched `content_hash`. Use atomic O_EXCL temp+fsync+replace for body and receipt. Validate stored
id/version/domain-separated hash before publishing either final file. Print only receipt metadata.

Before engine start, the bridge allocates a mode-0700 `mkdtemp` below an operator-owned runtime
root and passes constant `brief`/`receipt` paths unrelated to the id. Build a pointer-only prompt
requiring the worker to run the helper then read the staged brief. After the turn, require the
receipt to match the envelope ref and contain a well-formed hash; only the helper compares that
hash with the fetched row. The bridge can parse receipt JSON and remove the directory but cannot
open the body. A hydration failure cannot be overridden by a successful model reply; `finally`
cleanup covers every helper/engine/receipt failure.

Make hydration readiness one executed predicate shared by bridge construction, seat preflight,
and registry advertisement. It verifies helper installation, local-read policy against the
configured store, pointer-prompt construction, and receipt parser/cleanup before setting
`brief_hydrate=v1`. Missing/stale readiness keeps the advertisement absent and makes object tasks
fail `brief_hydration_unavailable`; it never falls back to `str(task)` or the legacy prompt
builder. The authority's frozen target recheck requires both parse and hydration advertisements.

Refactor the pure gate once: `evaluate` performs the existing resolver calls and accumulates audit.
Remove `check`, do not add a second resolver pass, and read no 1c private state. Persist the audit
through existing task event/result surfaces with the brief ref.

- [ ] **Step 6: Verify GREEN and run mutation kills**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/arb_memory/test_brief_hydrate.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_claim_gate.py \
  tests/test_bridge_claim_gate.py \
  tests/test_bridge_handle_raw.py \
  tests/arb_memory/test_local_memory_injection_codex.py -q
```

Expected GREEN (prediction): all pass with zero skips.

Mutations, separately:

- replace exact-version fetch with latest-version fetch;
- replace `artefact_hash` with raw SHA-256;
- skip hash comparison;
- derive a stage filename from traversal-shaped artefact id;
- skip `finally` cleanup on engine exception;
- let successful engine result override missing receipt;
- read staged body in bridge;
- bypass `evaluate` with a raw resolver call;
- consult injected prior audit instead of resolver.
- advertise `brief_hydrate=v1` without running readiness;
- select ref from parse capability alone;
- pass an object task to `build_task_prompt`/`str`.

Each named test must RED. Restore, rerun, and grep the final request/event fixtures for the body
sentinel; it must occur only in the store/hydrated-file assertion, never an envelope.

- [ ] **Step 7: Commit and stop for stage review**

```bash
git add \
  src/arb_memory/brief_hydrate.py \
  pyproject.toml \
  tests/arb_memory/test_brief_hydrate.py \
  src/agent_redis_bridge/redis_io.py \
  tests/test_redis_io.py \
  tests/test_dispatch_authority.py \
  src/agent_redis_bridge/claim_gate.py \
  src/agent_redis_bridge/bridge.py \
  tests/test_claim_gate.py \
  tests/test_bridge_claim_gate.py \
  tests/test_bridge_handle_raw.py \
  tests/test_push_task_event_tee.py
git commit -m "feat(dispatch): hydrate pinned briefs in workers"
```

---

## Stage 1d-vi — Ref-required enforcement, legacy removal, and rollout

**Dispatch boundary:** start only from reviewed-green Stage 1d-v and observed fleet-wide
`legacy-or-ref-v1` plus `brief_hydrate=v1` advertisement and successful ref hydration. Task 7 has
two deployment/code waves: ref-required first, legacy removal only after zero-legacy evidence. Do
not combine them into one restart.

### Task 7: Make rollout prerequisites executable and enable in order

**Files:**
- Modify: `scripts/seat-preflight`
- Modify: `tests/test_seat_preflight.py`
- Modify: `deploy/.env.example`
- Modify: `deploy/README.md`
- Modify: `tests/arb_memory/test_gate_deploy_shape.py`
- Later cleanup modifies: `src/agent_redis_bridge/envelope.py`,
  `src/agent_redis_bridge/dispatch_authority.py`, `src/agent_redis_bridge/bridge.py`,
  `src/agent_redis_bridge/redis_io.py`, and their exact transition tests/recipes.

**Interfaces:**
- Produces: preflight checks for reader, bound per-seat writer, v2 daemon-secret scrub, lane
  posture, local hydration, actual-target machine-user remote proof, target wire capability, and
  sender migration.
- Produces: separate ref-required, claim-gate, and legacy-removal checklists.

- [ ] **Step 1: Write failing preflight/deploy-shape tests**

Require exact deployment keys and checks:

```text
ARB_GATE_LANE_WRITER_ROLE
ARB_GATE_LANE_WRITER_DSN
BRIDGE_WORKTREE_LANE
BRIDGE_EXEMPT_GIT_SSH_COMMAND (one arb-exempt-bot key; IdentitiesOnly=yes)
target checkout origin -> resolved GitHub target repo (no BRIDGE_EXEMPT_GIT_REMOTE_URL)
ARB_MEMORY_LOCAL_DSN / ARB_MEMORY_LOCAL_MCP
BRIDGE_WORKER_VANTAGE
BRIDGE_CLAIM_GATE=0
BRIDGE_TASK_REF_REQUIRED=0
ENV_SCRUB_CAPABILITY=bus-and-gate-daemon-creds-v2
task_wire=legacy-or-ref-v1
brief_hydrate=v1
```

Synthetic gated-seat preflight passes without exempt Git variables; synthetic exempt-seat
preflight derives the target from the worker's actual checkout and fails each
missing/malformed/ambiguous target, missing or mismatched machine-user key/fingerprint,
unprovisioned-ledger target, read-fail, and push-unproven input. A target the machine user cannot
read hard-refuses before registration and no operator credential fallback is invoked. Tests prove
`BRIDGE_EXEMPT_GIT_REMOTE_URL` is neither required nor authoritative: setting it to a different
repository cannot redirect preflight or arm. Claim-gate-on preflight fails if reader,
authenticated role/binding/lane, lane-write reconcile readiness, v2 scrub self-check, or hydration
is absent. Ref-required preflight fails unless every selected target advertises dual parsing and
executed hydration readiness, the caller tripwire/registry evidence proves all senders migrated,
and ref dispatch/hydration has succeeded on each selected target. Parse-only targets must remain
legacy-emitting and must fail a direct ref with `brief_hydration_unavailable`; either violation
blocks preflight. Legacy-removal preflight additionally requires a bounded observation window with
zero authority legacy emission. Assert specific check names/messages.

- [ ] **Step 2: Run tests to verify RED**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/test_seat_preflight.py \
  tests/arb_memory/test_gate_deploy_shape.py -q
```

Expected RED (prediction): current preflight checks Redis/local MCP/workdir/senders only
(`scripts/seat-preflight:331-352`) and deploy docs contain no lane-writer/exempt configuration.

- [ ] **Step 3: Implement preflight and document the exact rollout**

Document per-seat role creation/binding as an operator-owned cluster action. Then:

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

Map delivered prerequisites:

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
| Exempt arm/run/release end to end | Task 7 live canary |
| Full confirmation/verifier machinery | Slices 1e–1h, external prerequisite to fleet-wide flip |

- [ ] **Step 4: Verify repository GREEN**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_claim_resolver.py \
  tests/arb_memory/test_lane_writer.py \
  tests/arb_memory/test_run_grants.py \
  tests/arb_memory/test_gate_deploy_shape.py \
  tests/arb_memory/test_brief_hydrate.py \
  tests/test_lane_writer.py \
  tests/test_exempt_git.py \
  tests/test_bridge.py \
  tests/test_bridge_worktree_lease.py \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_agent_dispatch.py \
  tests/test_ctl_worktree.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_stdio_child_env.py \
  tests/test_agent_sdk_models.py \
  tests/test_agent_sdk_session.py \
  tests/test_bridge_identity.py \
  tests/test_claim_gate.py \
  tests/test_bridge_claim_gate.py \
  tests/test_bridge_handle_raw.py \
  tests/test_seat_preflight.py -q
.venv/bin/python -m pytest \
  tools/faba/tests/test_faba_schema.py \
  tools/faba/tests/test_author_round_guard.py \
  tools/faba/tests/test_dispatch_brief.py -q
(cd tools/go-client && go test ./...)
scripts/check-doc-drift
```

Expected GREEN (prediction): all pass, zero skips. Any `skipped` summary is not completion.

- [ ] **Step 5: Execute and record the canary before any fleet flip**

Run the documented live role/binding, child scrub, heartbeat atomicity/replay, per-target
machine-user remote, real-enqueue outage, dispatch-vantage, ref-required, domain-hash hydration,
single audit entry, gated control, and arm/run/release checks with the DSN exported. Record actual
command outputs, request ids, lease id, artefact id/version/domain hash, resolved repository,
machine-user fingerprint, target capability/generation, database rows, and exact refusal codes.
Include the unprovisioned-target hard-refusal/no-fallback evidence. Do not summarize a predicted
rollout as an observed one.

If any prerequisite is absent, stop with `BRIDGE_CLAIM_GATE=0`; if wire evidence is incomplete,
also keep `BRIDGE_TASK_REF_REQUIRED=0` and retain dual acceptance. Code can merge default-off;
fleet enablement and legacy removal cannot.

- [ ] **Step 6: Commit ref-required/preflight guidance and stop for review**

```bash
git add \
  scripts/seat-preflight \
  tests/test_seat_preflight.py \
  deploy/.env.example \
  deploy/README.md \
  tests/arb_memory/test_gate_deploy_shape.py
git commit -m "docs(claim-gate): gate fleet enablement on exempt lane"
```

- [ ] **Step 7: After observed zero legacy, remove compatibility in a separate reviewed commit**

Delete string acceptance and the authority's legacy emission branch; delete/retire the dedicated
flag; remove `legacy_brief_bytes` from the non-FABA source/interface and recipes so callers pass
only the pre-minted ref+receipt; update advertisement to `ref-only-v2`; keep negative legacy
fixtures and the caller tripwire. Run the entire Task 7 and Final Verification sets plus doc drift
before:

```bash
git add \
  src/agent_redis_bridge/envelope.py \
  src/agent_redis_bridge/dispatch_authority.py \
  src/agent_redis_bridge/bridge.py \
  src/agent_redis_bridge/redis_io.py \
  src/agent_redis_bridge/wiki_refresh.py \
  src/agent_redis_bridge/learn_intake.py \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_agent_dispatch.py \
  tests/test_ctl_worktree.py \
  tools/go-client \
  skills/diagnose/panel.py \
  scripts/arb-memory-seat-e2e \
  scripts/seat-preflight \
  scripts/agent-dispatch \
  scripts/check-doc-drift \
  deploy/.env.example \
  deploy/README.md \
  README.md \
  docs/fragments/dispatch-recipe.md \
  skills/using-agent-bridge/SKILL.md \
  docs/orchestrator-patterns.md
git commit -m "feat(dispatch): remove legacy task wire"
```

The actual external supervisor/cluster changes are deployment actions after review, not files to
smuggle into repository commits.

---

## Final Verification

After all task commits, run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
git diff --check <slice-1d-base>..HEAD
scripts/check-doc-drift
.venv/bin/python -m pytest \
  tests/arb_memory/test_gate_schema.py \
  tests/arb_memory/test_gate_grants.py \
  tests/arb_memory/test_gate_store.py \
  tests/arb_memory/test_claim_resolver.py \
  tests/arb_memory/test_lane_writer.py \
  tests/arb_memory/test_run_grants.py \
  tests/arb_memory/test_gate_deploy_shape.py \
  tests/arb_memory/test_brief_hydrate.py \
  tests/test_claim_gate.py \
  tests/test_claim_gate_deny_proof.py \
  tests/test_claim_resolver.py \
  tests/test_lane_writer.py \
  tests/test_exempt_git.py \
  tests/test_dispatch_authority.py \
  tests/test_redis_io.py \
  tests/test_stdio_child_env.py \
  tests/test_agent_sdk_models.py \
  tests/test_agent_sdk_session.py \
  tests/test_bridge_identity.py \
  tests/test_bridge.py \
  tests/test_bridge_handle_raw.py \
  tests/test_bridge_claim_gate.py \
  tests/test_bridge_worktree_lease.py \
  tests/test_envelope.py \
  tests/test_envelope_claim_fields.py \
  tests/test_agent_dispatch.py \
  tests/test_ctl_worktree.py \
  tests/test_bridge_worktree.py \
  tests/test_bridge_parallelism.py \
  tests/test_envelope_run_id.py \
  tests/test_push_task_event_tee_integration.py \
  tests/test_seat_preflight.py \
  tests/defect_hunts/test_gate_assertions.py -q
.venv/bin/python -m pytest \
  tools/faba/tests/test_faba_schema.py \
  tools/faba/tests/test_author_round_guard.py \
  tools/faba/tests/test_dispatch_brief.py \
  tools/faba/tests/test_bridge_round.py -q
(cd tools/go-client && go test ./...)
```

Expected GREEN (prediction): all pass, zero skips, no doc drift, no diff-check errors.

Then inspect:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
git diff <slice-1d-base>..HEAD -- \
  src/agent_redis_bridge \
  src/arb_memory \
  scripts \
  tools/go-client \
  tools/faba \
  deploy \
  tests
! rg -n 'ARB_MEMORY_DSN.*fallback|ARB_GATE_READER_DSN.*fallback' \
  src/agent_redis_bridge/lane_writer.py src/agent_redis_bridge/bridge.py
! rg -n 'claim_gate\\.check|\\.check\\(' \
  src/agent_redis_bridge/bridge.py src/agent_redis_bridge/claim_gate.py
! rg -n 'rpush\\(.*request|payload.*task.*string' \
  scripts src/agent_redis_bridge tools/go-client skills docs/fragments README.md
rg -n 'legacy|invalid-payload-task-ref' \
  tests/test_envelope.py tests/test_dispatch_authority.py tests/test_agent_dispatch.py
git status --short
```

Confirm no temporary privilege grants, bypasses, body-shaped positive fixtures, mutation
injections, live credential values, unreviewed stage commits, or unowned deployment placeholders
remain. The final legacy grep is positive only in negative refusal/tripwire fixtures.

## Self-Review

| Requirement | Task |
|---|---|
| **R1:** per-seat function authority selected and justified; lane DSN/role scrubbed from every child/session/transcript; v2 capability proved before registration | 1 |
| **R2:** receive-only dual-accept→all callers migrated→hydration-ready advertisement→ref emission only on both capabilities→fleet proof→ref-required→later legacy removal | 4, 5, 6, 7 |
| **R3:** per-lease lock spans mint through reply; heartbeat busy is non-actionable; crash replay idempotent | 2 |
| **R4:** database binds session role to consumer/lane; no table DML; cross-seat DELETE/forgery mutation kills | 1 |
| **R5:** exact `arb_memory.hash.artefact_hash` contract and raw-SHA RED | 6 |
| **R6:** actual-target denial catalog, writable loud blocker, one-machine-user runbook/preflight, and unprovisioned-target hard refusal | 3, 7 |
| **R7:** one credential-scoped `publish_and_enqueue` seam and real Redis zero-enqueue/enqueue-then-raise KILL | 4, 5 |
| NEW-P0: parse capability never authorizes ref emission; parse-only object tasks refuse before prompt/stringification | 4, 5, 6, 7 |
| NEW-P1: FABA driver alone holds publish write material; Bash/Go/ctl/non-FABA require a pre-minted target-bound receipt; engine-child deny proof | 1, 4, 5, 7 |
| NEW-P2: SECURITY DEFINER bodies only in checked-in production `schema.sql`; parameterized Python calls; live tests use the same SQL | 1 |
| NEW-P2: Stage 1d-i size justified as one indivisible authority/containment boundary | Remediation decisions |
| Per-seat writer isolation assertion, reader/writer cross-denials | 1 |
| Closed relation/column/function privilege vocabulary + live parametrized KILL | 1 |
| Consumer server-side lane; arm schema stays closed | 2 |
| Arm success only after filesystem + row + result durable under lock | 2 |
| Row failure compensates/reclaims/tombstones and refuses | 2 |
| Release retires filesystem + row before success | 2 |
| Every partial state reconcileable/non-executable; replay never duplicates | 2 |
| Concrete per-target push-less mechanism on shared-checkout fleet, with no singleton remote | 3 |
| One machine-user SSH identity; no per-repo deploy keys or cross-owner fine-grained PAT | 3 |
| Actual-target fetch-positive/remote-push-denied proof and no operator-credential fallback | 3 |
| Same-UID/direct-URL residual stated honestly | 3, Global Constraints |
| Harness PUBLISH path is FABA-driver-only; never warm/non-FABA direct store write | 1, 4, 5 |
| Dispatch-time target vantage, retarget forces republish | 4 |
| Store outage stops gated and exempt dispatch at the real enqueue | 4 |
| Final envelope carries exact `{artefact_id, version}`, never body; parse-only seats never stringify it | 5, 6, 7 |
| Malformed ref fails exact envelope/CLI seam | 5 |
| Bash/ctl/Go clients agree | 5 |
| Worker hydrates via `ARB_MEMORY_LOCAL_DSN` | 6 |
| Bridge never selects `artefacts`; reader remains three-view SELECT-only | 1, 6 |
| Hydrated domain-separated hash/version matches gated immutable ref; safe stage/cleanup | 6 |
| Resolution record is audit, not credential; `evaluate` is the only gate entry | 6 |
| Close fresh re-resolution is a Slice 2 contract, not implemented | Remainder, 6 |
| Store/DB tests cannot skip-green | Prerequisite, every live task |
| Fleet enablement checklist orders receive-only parse, credential-scoped caller migration, hydration readiness, ref emission, then ref-required | 7 |
| Six separately dispatched/reviewed, independently-green stages | Remediation decisions |
| Probe schema, verifier assignment/family, rerun/F4, Devin refusal explicitly retained | Remainder |
| Slice 2, Slice 3, result-delivery preview excluded | Remainder |

**Files explicitly preserved:** `tests/arb_memory/test_schema.py`,
`tests/arb_memory/test_gate_schema_deny_proof.py`, `opus-5-orch-log.md`, and
`fable-5-orch-log.md`.
