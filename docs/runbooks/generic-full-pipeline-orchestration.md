# Generic full-pipeline orchestration runbook

**Purpose:** run a small but real design → spec → plan → implementation succession arc
through ARB while treating both the product and the orchestration infrastructure as systems
under test.

**Calibration:** two independent `mini_kv` runs (Grok Build and Codex orchestrators,
2026-07-11). A mini_kv-class run with full re-panel rigor took **76 minutes** including
live harness debugging. Operational budget: **2 hours** on a healthy-ish local cell.

This runbook is harness-neutral. Engine-specific authentication and dispatch syntax remain
in `skills/using-agent-bridge/SKILL.md`; host-specific credentials and seat IDs do not
belong here.

---

## 1. What the dogfood is actually testing

The deliverable is deliberately small. The primary system under test is the bridge's
evidence chain:

```text
identity → routing → isolation → execution → artifact → verification → completion
```

The recurring systemic failure is **vacuous green**: a signal can be technically true
while the reality it is meant to prove is false.

| Weak signal | Reality it fails to prove |
|---|---|
| Registry entry exists | One healthy consumer is actually listening |
| Heartbeat key exists | The intended process owns the identity and can execute a turn |
| Output file is non-empty | The reply is successful (`ok:true`), not an error payload |
| Task says `completed` | Required artifact/commit/tests exist |
| Worktree was requested | The worker actually stayed inside it |
| PID existed after launch | The process survives the launching session |
| Reviewer says tests pass | Tests ran and their count/output match |

**Rule:** never let a control-plane signal certify itself. Couple it to an independent
data-plane observation.

---

## 2. Entry criteria

Before starting the clock:

1. Pick a bounded product implementable in one focused dispatch but complex enough to
   exercise persistence/state, failure semantics, CLI/API behavior, and tests.
2. Create a fresh repository. Never reuse the prior dogfood's completed artifacts.
3. Read `AGENTS.md`, `skills/using-agent-bridge/SKILL.md`, and this runbook.
4. Choose one honest orchestrator identity. Add it explicitly to every seat policy; never
   borrow another harness's sender ID.
5. Mint unique seat identities and assert **one live process per agent ID**.
6. Use a real supervisor for durable seats: launchd/systemd with restart policy. A
   persistent foreground exec session is acceptable for an interactive dogfood. `nohup`
   launched from a managed terminal/session is not a supervisor and is not canonical.
7. Completion enforcement is mandatory for durable daemons. The bridge ignores the old
   `AGENT_ENFORCE_COMPLETION=0` ambient override and accepts `--no-enforce-completion` only
   with an explicit diagnostic mode (`--self-test`, `--once`, or `--dry-run`).
8. Start with a clean base checkout and bridge-created worktrees for every writer and
   every independent reviewer.

### Seat preflight: prove consumption, uniqueness, and authority

For each seat, perform all three checks:

1. **Uniqueness:** inspect the process table/supervisor and prove exactly one daemon owns
   the full agent ID.
2. **Consumption:** dispatch a unique nonce and require a matching reply; registry and
   heartbeat alone do not pass preflight.
3. **Authority:** for a write-capable seat, perform a disposable in-worktree write +
   commit probe under the actual orchestrator sender policy.

Record seat ID, PID/supervisor unit, registered timestamp, code SHA, model/harness, nonce,
and probe result in the run manifest.

---

## 3. Run manifest and timing

Create one durable manifest containing:

- repo root and clean base SHA;
- orchestrator ID;
- seat roster and preflight evidence;
- stage artifact paths;
- one `run_id` per panel round;
- task IDs, output/error paths, and assigned worktrees;
- start/end timestamps per stage;
- freeze commit per stage;
- deviations, infrastructure incidents, and recovery actions.

Expected mini_kv-class budget:

| Phase | Working envelope |
|---|---:|
| Seat preflight + seed | 10–20 min |
| Design including re-panels | 15–30 min |
| Spec including re-panels | 15–35 min |
| Plan including re-panels | 10–25 min |
| Implementation + final panel | 15–35 min |
| Total | target ~75–90 min; budget 2 h |

Rigor is not the dominant cost. Authentication, dead seats, duplicate identities,
misleading completion signals, and artifact integration are.

---

## 4. Stage machine

Run this state machine for **design**, **spec**, **plan**, then **implementation**:

```text
AUTHOR → PANEL → VERIFY FINDINGS → FOLD P0/P1 → RE-PANEL
                                      ↑             |
                                      └── until 0 P0/P1
```

### Author

- Author from the frozen parent only; do not leak later artifacts or the previous
  dogfood repository.
- The author writes and commits the initial artifact in an isolated worktree.
- The warm orchestrator verifies the returned SHA, artifact path, and dirty state.

### Independent panel

- Use at least three decorrelated seats when available.
- Mint a new shared `run_id` for every round.
- Reviewers read the artifact and frozen parent, not each other's reports.
- Give every reviewer its own worktree and a distinct report path.
- Require evidence-first findings and a canonical vote fence.

### Verify findings before folding

P0/P1 labels are candidates. Reality-check the hinge claim against code, artifact text,
or executable behavior. Reject polished false findings; do not remediate them into the
system.

### Fold and re-panel

- The warm orchestrator folds confirmed P0/P1 findings.
- Write a synthesis mapping finding → verification → change location.
- Re-panel the folded artifact with a new run ID. Reviewers verify folds **and hunt new
  defects introduced by the fold**.
- Freeze only when every panel member has zero surviving P0/P1. Self-verification never
  freezes an artifact.
- Any downstream work started before the parent freezes is non-binding and must be rerun.

### Implementation-specific gate

A worker reply is a claim. Completion requires all of:

1. expected production and test artifacts exist inside the assigned worktree;
2. allowed-path/isolation check passes;
3. worktree has a commit descendant of the dispatched base;
4. independent orchestrator test run passes with exact count/output;
5. implementation panel returns zero P0/P1.

If the worker returns `completed` with a dirty uncommitted tree, the task failed its
contract even if the code works. Preserve and integrate recoverable work, but record the
completion-gate defect.

---

## 5. Completion watcher contract

Use one silent watcher per run. It prints nothing until every seat reaches a validated
terminal result.

For every manifest row, success requires:

```text
reply parses as JSON
AND reply.ok == true
AND Redis task state is terminal-success
AND in_reply_to/task_id matches
AND required artifact/commit predicate passes (for write tasks)
```

An `ok:false` reply is terminal **failure**, never success. A non-empty output file alone
is insufficient. `queued` plus non-empty output is an inconsistency that must fail loud.

The final banner must include counts:

```text
expected=N terminal=N ok=N failed=0 artifacts=N commits=N
```

The watcher exits non-zero on timeout, parse failure, identity mismatch, terminal failure,
missing artifact, missing commit, or contradictory state. Two consecutive green samples
remain useful for flush races, but cannot repair a weak predicate.

Do not use `pgrep` discovery, inbox length, or registry existence as completion.

---

## 6. Failure handling

### Request remains queued

Do not infer from the registry. Verify the seat process is alive, unique, connected, and
consuming. Restart under the supervisor if necessary; the queued envelope should then
drain.

### Sender rejected unexpectedly

Check for duplicate agent IDs first. A stale daemon with an older sender policy may have
consumed the request. Kill/quarantine the duplicate, prove uniqueness, and re-dispatch
with a new task ID and run ID.

### Worker escapes its worktree

Stop concurrent base writers, preserve evidence, compare base/worktree diffs and commits,
and treat the run's isolation claim as failed. Two observations promote this from anomaly
to reproducible defect. Do not normalize the escaped write as harmless because its content
was correct.

### Daemon dies after launch

Do not promote `nohup` folklore. Use a real supervisor for durable operation or a
harness-owned persistent foreground session for the dogfood. Prove survival with a nonce
after the launching shell/session has ended.

### Watcher announces contradictory completion

Treat the banner as a failed verifier. Inspect the raw reply and Redis state, fix the
predicate, mint a new task/run ID where needed, and preserve the false banner as evidence.

---

## 7. Closeout

The run is complete only when:

- design, spec, plan, and implementation each have a frozen commit;
- every freeze followed an independent zero-P0/P1 panel;
- final tests were run independently by the orchestrator;
- task/result/commit/artifact counts reconcile;
- infrastructure defects and product residuals are separated;
- elapsed time and per-stage timing are recorded;
- a concise outcome is stored in git and ARB Memory.

Classify every incident by the invariant it violated:

| Class | Examples |
|---|---|
| Identity uniqueness | duplicate agent IDs, stale consumer |
| Liveness | registry without consumption, reaped process |
| Completion polarity | non-empty `ok:false` output treated as done |
| Artifact truth | completed without commit, missing report |
| Isolation | base write from assigned worktree task |
| Verification invocation | completion gate configured off, test written but not run |

This classification matters more than six disconnected bug tickets: it reveals whether
the signal layer has a systemic vacuous-green failure mode.

---

## 8. Re-validation ritual

Run this dogfood quarterly and after material changes to dispatch, registry, supervision,
worktree isolation, completion gates, watchers, or audit plumbing.

Production arcs optimize for the deliverable and route around infrastructure friction.
The scheduled low-stakes dogfood is the standing appointment where the infrastructure is
the deliverable and the orchestrator is required to distrust it.

Rotate the warm orchestrator harness when possible. Fresh harnesses carry different
assumptions and expose ambient conveniences that incumbent operators have learned to
route around.

Success is not “the toy shipped.” Success is: the machine either proved every link in its
evidence chain or failed itself loudly enough to fix.

---

## 9. Provenance

- Historical Grok Build first-ARB orchestration session record:
  `an archived build-orchestrator note (not included in this repository)`
- Codex run outcome:
  `/Users/<user>/test/arb-codex-dogfood/reviews/dogfood-outcome.md`
- Codex run elapsed time: 19:02:30–20:18:52 Europe/London = 76m22s.
- Shared empirical rule: mini_kv-class full rigor ≈ 75 minutes; budget 2 hours.
