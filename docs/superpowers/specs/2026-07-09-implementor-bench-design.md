# Implementor bench — generation-quality floor for implementor seats (design v2.1, panel rounds 1+2 absorbed)

> **Round 2** (`panel-capsuitebdesign-r2-20260709T010114Z-d86f2b`): agy + cold-Opus approve
> (dispositions verified mechanism-real against bridge.py source); codex P1 — the retained
> fixture ref did not keep the WORKER RESULT commit reachable after worktree removal —
> absorbed in v2.1 (D2 step 4: `refs/implbench/results/<run_id>/<task>` created at
> `head_after` before teardown; V5 prune adversary). pi-GLM wedged (no output within 30s,
> turn aborted) — NAMED ABSENT, re-fired in round 3 per dont-silently-shrink-the-panel.
>
> **Round 3** (`panel-capsuitebdesign-r3-20260709T011412Z-1d67d9`): unanimous approve
> (GLM re-fired and served; cold-Opus P2 — V5 prune adversary needs a mirror/all-refs
> clone — absorbed in-tree). **CERTIFIED at v2.1, zero P0/P1.**

Status: **v2 — round-1 panel findings absorbed** (run
`panel-capsuitebdesign-20260709T002811Z-8fd798`, unanimous needs-changes, max P0
(cold-Opus), no wall breach; all mechanical claims verified against source by GLM). v2
dispositions: **P0/P1-α** (cold-Opus P0 + agy F2 + GLM P1-2 + codex G4/G6-conflict) the
gates premised the bridge as a passive git-evidence emitter, but launch-bound default-on
`orchestrator_commit` rescues non-committers and collapses history — G2 is now
three-valued on the mechanical `committed_by` evidence (the rescue commit carries a
`Committed-by: agent-redis-bridge` trailer, `bridge.py:1401-1404`, and the completion
payload records `committed_by`), G4 gains a specified NOT-DEMONSTRATED verdict for
single-commit/rescued shapes, and the task schema splits baseline vs worker test paths
(D3); **P1-β** (codex + agy + GLM: hidden battery filesystem-reachable — a vacuously-green
V3) batteries are now encrypted at rest, key never seat-reachable, decrypt at scoring time
only (D3 G1, V3); **P1-γ** (agy F1 + codex: orphan-commit GC pruning rots evidence and
races queued dispatches) fixture refs are now mandatory-before-enqueue and retained until
operator prune — never deleted at teardown (D2, V5). P2 absorbs: rung claim narrowed to
"rung 1 + the stated-plan/mechanical subset of rung 2" (GLM P2-1); cluster-UNKNOWN pooling
rule specified (GLM P2-2); G7(a) pinned to the structured status block (GLM P2-3 + agy P2);
G6 pass-count via machine-readable test reports, not stdout parsing (agy P2); frontier cost
marked estimate-until-calibrated with a V5 calibration obligation (codex P2 + GLM P2-5);
object-DB residue statement + fsck accounting (GLM P2-6); injection target repo named
explicitly (cold-Opus P2). This is the §7-deferred oracle from `docs/eval-suite-design.md`: *"For
implementer-role appraisal, the generation-quality oracle is still to be designed"*
(`eval-suite-design.md:120-121`; same deferral at `:76-77` — "implementer/adjunct fit needs
a separate generation-quality oracle, deferred").

## Problem (hinge facts, verified against source 2026-07-09)

Implementor seats are trusted onto the routing ladder on anecdote, and re-trusted after
seat/model changes on nothing more than a ping:

- The routing rulebook (`docs/implementor-routing.md`) sources its default rung from one
  10-task gate + 5 bake-offs run 2026-06-06 (`implementor-routing.md:151-172`), and its own
  re-gating section demands a repeat "whenever a model on the ladder gets a new version …
  or it's been > 6 months" (`implementor-routing.md:294-308`) — but the gate procedure is
  prose in `qwen-worker-seats.md:125-139`, re-executed by hand, never systematized.
- The two live precedents this design systematizes:
  - **model-alias-1 spike + 2-round bake-off** (memory
    `~/.claude/projects/-Users-mark-<workspace>/memory/pi-sdk-model-alias-1-implementor-spike.md:23-44`):
    orchestrator-authored **hidden acceptance battery** the implementors never saw,
    own-tests-first briefs, isolated worktrees, verify-the-commit-not-the-reply. It worked —
    it caught model-alias-1's one real defect (permissive boundary, `refill_rate=0`) and caught that
    cursor-acp's *code* was 27/27 while its *delivery* was 0/3 — but it was a one-off
    hand-run, unrepeatable without re-deriving the method.
  - **cursor-acp implementor usage** (memory
    `cursor-acp-experimental-reviewer-viability.md:57`): "code flawless … but the ACP reply
    channel died on 3/3 dispatches" — proof that a code-only score inverts operational truth;
    delivery and process are first-class dimensions of implementor fitness.
- The bridge already computes a per-dispatch objective completion signal:
  `completion_gate.evaluate()` classifies head movement × tree cleanliness into pass/bounce
  states (`src/agent_redis_bridge/completion_gate.py:25-28,62-85`), `missing_artifacts()`
  checks the `expected_artifacts` contract (`:88-94`), and the gate is only *enforceable*
  in a worktree — shared-cwd dispatches are `shared_cwd_unchecked` (`:97-101`). The bridge
  captures `head_before`/`dirty_before` before every turn (`bridge.py:1073-1074`) and
  applies the gate after it (`bridge.py:1104-1110`). **Reuse this; do not duplicate it.**
- Hard isolation exists and is the mandated substrate: `--worktree` runs the task on a
  fresh engine **whose cwd IS the worktree**, so the base checkout cannot be modified by
  construction (`skills/using-agent-bridge/SKILL.md:109-122`; `bridge.py:1047-1049`).
  Bridge worktrees are anchored at the seat's `AGENT_WORKDIR`
  (`bridge.py:986-1004`: `<workdir>/.claude/worktrees/<name>`, created with
  `git -C <workdir> worktree add --detach <base_ref>` — any committish is accepted).
- `agent-dispatch` already carries the contract flags the bench needs:
  `--worktree/--worktree-base/--worktree-cleanup`, `--expected-artifact`, `--allowed-path`,
  `--run-id` (`scripts/agent-dispatch:59`).

The gap: **no repeatable, objective-gated capability probe exists for implementor seats at
seat/model-change events.** The `/learn` gate rejected `arb-bench` (verdict record: ARB
Memory artefact `learn-arb-bench-role-scoped-red-team-benchmark-5ad6a61f` v4, summarized at
`docs/learn-candidates.md:129-142`) — but what it foreclosed was the *reviewer red-team
bench consumed for trust assignment* (the REJECTed v1 corpus renamed). The floor-capability
kernel was explicitly named legitimate and already-approved in the Instrument-1 shape. This
design is that kernel, generation-side, wall included.

## What transfers from §0 — and what does not (the epistemics, engaged honestly)

`eval-suite-design.md` §0 (`:11-18`) proves every internal **catch-corpus** blind to the
never-caught set: a record of catches contains only what some seat could see, so it cannot
license decorrelation or seat-drop claims. That proof concerns reviewer corpora, where the
ground truth of each fixture *is itself panel-legibility-bounded*.

**Objective gates change the per-task epistemics.** A generation task's verdict is "does
the hidden battery pass in the worktree / did HEAD advance / is the diff in scope" —
machine-checked facts external to any seat's (or the author's) perception of the *output*.
A seat cannot argue with a red battery, and no matcher/normalizer layer sits between
finding and oracle. Per-task ground truth here is strictly stronger than reviewer-fixture
ground truth.

**What transfers anyway — three things, and they bound the claim:**

1. **Corpus-selection blindness (the §0 analog).** Only tasks with author-legible,
   objectively-gateable success criteria can enter the corpus. The implementor value that
   justifies the top rung — ambiguous refactors, architectural judgment, "the design is
   under-specified" (`implementor-routing.md:78-96`) — is excluded *by construction*:
   there is no hidden battery for a task whose correct shape is the thing being discovered.
   The never-benchable task class is the generation-side never-caught set. Consequence:
   this bench can qualify a seat for **rung 1 plus the stated-plan/mechanical subset of
   rung 2** (GLM P2-1: `implementor-routing.md:32-39` is rung 1's criteria; rung 2's
   "spec leaves room" tasks are partly in the never-benchable set, so the headline must not
   claim all of rung 2) and can *disqualify* at the floor; it cannot rank rung-3 fitness
   and must never claim to.
2. **P1 — correlated instances inflate confidence** (`docs/measurement-principles.md:7-26`,
   instances 5 at `:43-65`): N tasks of one mechanism re-skinned are ~1 sample. The corpus
   is cluster-keyed by mechanism and reported per cluster, pooled conservatively (a cluster
   passes only if **all** members pass — the caught-side mirror at
   `measurement-principles.md:52-57`).
3. **P2 — the wall stops the writer, not the reader** (`measurement-principles.md:89-101`):
   any per-seat-per-gate grid is reader-convertible into a ranking. The reporter refuses to
   *write* one; the residual convertibility is stated in the artifact, not claimed walled.

So: Instrument-1-class epistemics (floor + pipeline validation), with **stronger per-task
ground truth** and **weaker corpus representativeness at the high end**. The honest claim
is exactly: "seat S clears / does not clear the bounded-work floor, per mechanism cluster,
per gate." Nothing more.

## Constraints

1. **Report-only (hard wall).** Nothing downstream consumes results to change
   trust/quorum/routing automatically. Routing stays the caller's job
   (`implementor-routing.md:8-9`); results are evidence for a human.
2. **No ranked leaderboard, no composite seat score, no trust verdicts** in v1 output —
   same structural wall as Instrument 1 (`eval-suite-design.md:57-69`), enforced by the
   reporter (mechanical field refusal), justified below.
3. **Hard-isolated worktrees for every task** (`SKILL.md:109-122`). Enforced completion
   needs a worktree (`completion_gate.py:97-101`); soft prose-`cd` isolation is the known
   footgun.
4. **Bench the deployed seat, one seat per run.** Seat = model × role-profile × tools,
   bound at launch (memory `bridge-seat-role-bound-at-launch`); a bench-dedicated seat
   would measure a different thing than the fleet runs.
5. **Hermetic tests for everything that doesn't need a live engine.** CI has no engines;
   binary/live probes stay outside CI (same rule as CDX-1 V6).
6. **Never trust seat self-report** — identity confabulation is documented for model-alias-1 and both
   qwen seats (`qwen-worker-seats.md:181-184`); model attribution comes from seat config +
   engine/CLI version + billing delta, never from prose.
7. **Infra failure ≠ seat failure.** A gate whose evidence collection itself fails yields
   UNKNOWN, never FAIL (evidence-store no-silent-drop, one level up).
8. **run_id discipline**: every bench dispatch labeled (`implbench-<seat>-<ts>`), per
   dispatch-run-id-discipline.

## Design

### D1 — Task corpus: mechanism-clustered synthetic fixtures, pinned by construction

**Distinctness rule (P1 applied to tasks):** every task declares a `cluster` key naming the
*mechanism family* it exercises. Two tasks share a cluster iff a seat that can do one can
do the other for the same underlying reason. The v1 corpus is **N = 10 tasks across 7
clusters** (1–2 tasks each), drawn from the families the live precedents proved
discriminative:

| Cluster | Exemplar shape | Precedent |
|---|---|---|
| C1 greenfield-algorithmic | token-bucket / parser with boundary probes | model-alias-1 bake-off r1 (caught the real `refill_rate=0` miss) |
| C2 hard-spec-sweep | cron next-fire w/ DOM/DOW-OR + leap years | model-alias-1 bake-off r1 hard battery |
| C3 refactor-preserve-quirks | legacy module with named quirks that must NOT be "fixed" | bake-off r2 (trailing-space key, half-up rounding) |
| C4 concurrent-mechanism | job queue / lock discipline | bake-off r2 |
| C5 seeded-bug-hunt | ≥1 latent bug among seeded ones | bake-off r2 (`events_for` time-order) |
| C6 spec-trap / prohibition | awkward-but-explicit spec + "do NOT X" rails | qwen gate task 10 (`qwen-worker-seats.md:106-110`) |
| C7 multi-file-mechanical | stated plan across several files, no design room | brain-repo bake-offs / Composer rung |

**Honest floor size:** effective n = **7 clusters**, pooled — small n, wide implicit CI, so
the output vocabulary is per-cluster PASS/FAIL/UNKNOWN, never a rate. **Cluster pooling
rule (GLM P2-2, ambiguity closed):** any member FAIL ⇒ cluster FAIL; else any member
UNKNOWN ⇒ cluster UNKNOWN (re-runnable to resolution); else PASS. G4's NOT-DEMONSTRATED
does not enter cluster pooling at all — it is reported per-task in its own column. Precedent: the 10-task
qwen gate was decision-grade for a deliberately narrow promotion
(`qwen-worker-seats.md:68-79`); this corpus is the same size with the correlation structure
made explicit instead of implicit.

**Fixtures are synthetic mini-repos, not host-repo history.** Each task ships as a file
tree under `bench/implbench/fixtures/<task>/tree/` plus a `task.yaml` (brief text, cluster,
allowed-path globs, expected artifacts, prohibitions, tdd flag, worker test command,
battery id). Fixture trees are self-contained (own tests, own README-level context), small
(≤ ~30 files), and dependency-light (stdlib-only where possible) so they stay runnable for
years. **Solvability does not rot as <workspace> moves because tasks never reference the host
repo at all** — the pinning problem is dissolved, not solved.

**Corpus version = content.** `corpus_version` is the hash of (task specs + fixture trees +
batteries). Because fixtures are materialized as deterministic git objects (D2), each
task's base SHA is a pure function of its tree — the fixture SHA doubles as a per-task
fingerprint.

**Leakage stance (decided, not waved at):** tasks are novel-authored synthetic mechanisms,
*not* harvested from this repo's history — harvested tasks would be in-distribution for
seats that work here daily, correlated with each other (P1), and rot with the repo. Three
residuals, handled distinctly: (a) **battery leakage** — the hidden battery never enters
any tree, ref, or prompt the worker can see (structural, CI-checked, D3); (b) **training-set
in-distribution bias** — accepted and *in scope*: a floor check on bounded-brief execution
is measuring competence at exactly the kind of work models train on; we are not measuring
novelty resistance; (c) **cross-run memorization** — irrelevant for repeated floor checks
of one model (pass staying pass is the point), but corroding for *model-version*
comparisons on a stale corpus, so corpus refresh (new task variants, same clusters) is
part of the re-gate cadence, mirroring "use a different codebase target each time"
(`implementor-routing.md:307-308`).

### D2 — Execution: orphan-commit fixture injection + `--worktree` hard isolation

The mandated substrate (`--worktree`) anchors worktrees at the seat's `AGENT_WORKDIR` and
accepts any committish as base (`bridge.py:986-1004`). The bench exploits this instead of
fighting it:

1. **Materialize:** the harness writes the task's fixture tree into the *seat repo's object
   database only* — `git hash-object -w` / `mktree` / `commit-tree` with pinned
   author/committer/date ⇒ a **deterministic orphan commit SHA** per task. The target repo
   is the one the seat's `AGENT_WORKDIR` names (resolved from the seat's launch config —
   for bridge-dev seats that is the <workspace> or AgentRedisBridge clone per
   `bridge-clone-topology`; cold-Opus P2). No checkout, no branch. **Ref lifecycle (round-1
   P1-γ — was "may hold", now mandatory):** the harness MUST create
   `refs/implbench/runs/<run_id>/<task>` pointing at the fixture commit BEFORE the dispatch
   is enqueued, and the ref is RETAINED after the run — teardown removes the worktree only.
   Rationale: an unreferenced orphan races `git gc` while a dispatch waits in the seat's
   queue (verified by the panel: `reflog expire + gc --prune=now` deletes it), and deleting
   the ref at teardown rots the NDJSON evidence — V-obligation re-run checkouts
   (`git worktree add <sha>` years later) require the objects to stay reachable. Pruning
   old run refs is an explicit operator act (`implbench prune --before <date>`), never
   implicit. Side effect: fixture commits are reachable, so `git fsck` reports no dangling
   noise (agy P2).
2. **Dispatch:** one task = one dispatch to the seat under test:
   `agent-dispatch --engine <engine> --target-id <seat> --worktree implbench-<task>-<nonce>
   --worktree-base <fixture-sha> --worktree-cleanup keep --expected-artifact …
   --allowed-path … --run-id implbench-<seat>-<ts> <brief>`. The worker's cwd IS a worktree
   containing *only* the fixture tree, detached at the fixture SHA. The base checkout is
   untouchable by construction; parallel tasks cannot collide.
3. **Brief shape:** own-tests-first TDD brief (where flagged), explicit expected artifacts,
   explicit prohibitions — the hidden-battery method's brief shape, verbatim from the
   bake-off precedent.
4. **Collect:** post-reply, the harness runs all gates **in the kept worktree** (evidence =
   git + filesystem + task-event stream, never reply prose), writes NDJSON evidence, then —
   **BEFORE `git worktree remove` (round-2 codex P1: the fixture ref alone leaves the
   WORKER RESULT commit unreachable after worktree removal; a later `gc --prune=now` rots
   the exact evidence G1/G3/G4 re-runs need)** — creates
   `refs/implbench/results/<run_id>/<task>` at `head_after`. Only then is the worktree
   removed. Both refs (fixture base + result) are retained under the same
   operator-prune-only lifecycle, so NDJSON SHAs stay checkout-able for battery reruns,
   diff validation, and G4 parent walks indefinitely.
5. **Concurrency/cost:** one seat per invocation (hard); tasks queue on that seat's inbox at
   its own `max_parallel`. Default 2-concurrent, per-task timeout 900s.

Scope note: this requires harness filesystem access to the seat's workdir repo — **v1 is
same-host seats only**; forge-host seats need either a remote-ref push variant or a local
harness run, deferred and named.

### D3 — Objective gates (all eight are v1; each is cheap)

| Gate | Question | Evidence (never prose) |
|---|---|---|
| **G0 delivered** | did the dispatch return `ok` within timeout? | reply envelope status |
| **G1 battery** | does the **hidden acceptance battery** pass? | harness-run in worktree |
| **G2 delivery** | did the WORKER deliver a clean commit — or did the orchestrator rescue it? | three-valued, from `committed_by` + the rescue trailer, below |
| **G3 scope** | changed paths ⊆ task allowlist? | `git diff --name-only <fixture-sha>..HEAD` + porcelain, vs `allowed-path` globs |
| **G4 tdd** (flagged tasks) | red *observed before* green — where the evidence exists? | commit-graph evidence with a specified NOT-DEMONSTRATED verdict, below |
| **G5 prohibition** (flagged tasks) | forbidden pattern absent? | mechanical pattern/AST check on the diff — the deny-proof analog |
| **G6 no-test-weakening** (fixtures with baseline tests) | baseline tests unmodified; pass-count non-decreasing? | diff over `baseline_test_paths` + rerun with a **machine-readable report** (fixtures declare `--junitxml`-class output in `task.yaml`; the harness parses the report file, never test-runner stdout — agy P2) |
| **G7 no-fabrication** | do the reply's structured claims match git? | mechanical cross-checks, below |

**G1 — hidden until scoring, structurally (round-1 P1-β: "not in the fixture tree" is NOT
"unreachable" — a same-host worker can read any path by traversal, so plaintext batteries
anywhere on the host make V3 a vacuously-green guard).** Batteries are stored **encrypted
at rest** (`bench/implbench/batteries/<task>.enc`, AES via `openssl enc` or `age`); the
plaintext never exists on disk outside scoring. The key lives ONLY in the harness
operator's environment (`IMPLBENCH_BATTERY_KEY`, sourced at scoring time — never in the
repo, never in any seat's env file, never in the dispatch env). Scoring happens AFTER the
turn ends: the harness decrypts to a private temp dir outside every seat workdir, runs the
battery against the kept worktree with prefixed `PYTHONPATH` and an asserted
`module.__file__` under the worktree (the worktree-editable-install-shadowing scar), and
deletes the plaintext. **Honest residual, stated:** a worker can read the ciphertext
(useless without the key), and a worker running DURING scoring on the same host could in
principle race the temp dir — mitigated by scoring after turn completion and a 0700 temp
dir; a same-host root-equivalent seat is out of threat model (ARB's threat is mistakes,
not malice). Rationale for hidden-not-provided: visible acceptance tests invite
teaching-to-the-test and erase the TDD signal; the bake-offs proved the hidden method
discriminates (it caught the one real model-alias-1 defect that its own 24-function suite did not).

**G2 — delivery, three-valued (round-1 P0/P1-α):** the bridge's `orchestrator_commit` is
launch-bound and default-on (`auto_commit`/`enforce_completion`, `bridge.py:290,314,2502`)
and constraint 4 forbids switching it off for the bench — so a worker that writes correct
code but never commits gets its work committed BY THE BRIDGE. Pretending that is worker
delivery would rescue exactly the cursor-acp failure class this bench exists to catch. G2
therefore reads the mechanical evidence and reports three values:
**DELIVERED** (`committed_by: "agent"` in the completion payload; head commits carry no
bridge trailer), **RESCUED** (`committed_by: "orchestrator"`; the rescue commit carries the
`Committed-by: agent-redis-bridge` trailer, `bridge.py:1401-1404` — verifiable from git
alone even if the envelope is lost), or **NOT-DELIVERED** (bounced/failed states). The
harness cross-checks payload vs trailer and records `evidence_conflict` UNKNOWN if they
disagree. RESCUED is not a pass and not a fail of *code* — it is the delivery-discipline
signal, reported as its own column; expected artifacts and cleanliness still come from
`completion_gate.evaluate`/`missing_artifacts` (reused, not duplicated).

**G4 — TDD from evidence, not self-report; verdict specified for every commit shape
(round-1 P1-α cont.: parentage is uninformative for the common single-commit worker, and a
rescue collapses history).** The brief mandates: first commit after base = failing test(s)
only; implementation commits follow. Evidence semantics:
- **Multi-commit worker chain** (≥2 worker-authored commits): walk the first-parent chain
  from `<fixture-sha>`; commit #1 must touch only paths under the task's **worker test
  paths** (see below); the harness checks out commit #1 in a scratch worktree and runs the
  worker's declared test command — it must FAIL; at `head_after` it must PASS →
  PASS/FAIL as designed.
- **Single worker commit, or G2=RESCUED** (history collapsed): G4 =
  **NOT-DEMONSTRATED** — a named non-PASS distinct from FAIL: the discipline was neither
  observed nor refuted. Reported as its own value; never pooled as a failure.
- Honest bound, stated in the artifact: G4 certifies the *discipline shape* where the
  evidence exists, not test quality — G1 owns quality.
**Test-path schema (fixes the G4/G6 conflict, codex P1):** `task.yaml` declares
`baseline_test_paths` (immutable — G6 guards them) and `worker_test_paths` (additive —
where G4's red test belongs, including shared helpers like `conftest.py` explicitly
listed). G4's commit-#1 check runs against `worker_test_paths`; G6 runs against
`baseline_test_paths`. The two gates cannot contradict on the same file by construction.

**G7 — fabrication, mechanical subset only in v1:** (a) the dispatch runs
`--expect-structured` and G7(a) checks the STRUCTURED status block only (`status:complete`
asserted while G2 is NOT-DELIVERED or G1 is red ⇒ `fabricated-completeness` flag, memory
`fabricated-completeness-is-the-default`) — no prose parsing, fully mechanical (GLM P2-3 +
agy P2; a keyword match over free prose is gameable and false-positive-prone); (b) reply
cites a commit SHA absent from the worktree ⇒ flag. Prose-level claim auditing (e.g. "all
24 tests pass" vs actual counts) is v2 — it needs parsing judgment and would dilute the
"objective" claim of v1.

**UNKNOWN discipline (constraint 7):** any gate whose evidence collection errors (bus blip,
git failure, battery harness crash) records UNKNOWN with the error attached; the task is
re-runnable. A seat is never failed on infrastructure.

### D4 — Scoring and report shape: the wall applies, for generation-specific reasons

**Decision: the reviewer-side wall shape applies to generation benches** — no ranking, no
composite score, no trust/quorum verdict; unordered per-cluster × per-gate
PASS/FAIL/UNKNOWN for **one seat**; report informs a human. Justification, engaging the
arb-bench verdict rather than citing it as scripture:

1. **The objective gates genuinely change what a comparison would mean** — a per-task
   pass/fail is real ground truth, so a cross-seat table would not be *confabulated* the
   way a reviewer catch-ranking is. The wall here is not because the numbers are fake.
2. **It is because the corpus cannot rank what the ranking would be used for.** A
   leaderboard over bounded legible tasks laundered "good at rungs 1–2" into "better
   implementor" would mis-route exactly the rung-3 work where mis-routing is most
   expensive (§ epistemics, point 1). The live data already shows the inversion concretely:
   cursor-acp scored 27/27 on code and 0/3 on delivery (memory
   `pi-sdk-model-alias-1-implementor-spike.md:30-32`) — a code-quality leaderboard would have ranked
   a bridge-unusable seat at the top. Per-gate reporting *preserves* that structure;
   a composite *destroys* it. The composite is not just forbidden, it is lossy.
3. **P2**: the per-gate grid is still reader-convertible into a ranking; the artifact
   carries the construct-validity disclaimer stating this residual as
   inherent-and-accepted, not walled (`measurement-principles.md:96-101`).
4. **Precedent**: the arb-bench rejection (`learn-candidates.md:129-142`) foreclosed
   bench-for-trust-assignment; the approved shape is evidence-for-human-decisions. The
   human decision this feeds is a named, existing one: the routing-ladder placement and
   re-gate calls that `implementor-routing.md` already assigns to the caller.

**Mechanical enforcement (Instrument-1 parity, `eval-suite-design.md:65-69`):** the
reporter emits a fixed schema with no rank/score/trust fields and **refuses** them; output
namespace is bench-only. A reporter unit test pins the refusal.

### D5 — Storage and trigger

- **Evidence (authoritative):** append-only NDJSON per run under
  `bench/implbench/results/<run_id>.ndjson` — one record per (task, gate) with raw
  evidence pointers (SHAs, battery output, diffstat). Same authority rule as Instrument 1
  (NDJSON authoritative; anything else is a derived projection).
- **Summary artifact:** one ARB Memory artefact per run via `memory_store`, keyed
  `seat_id + engine + model_declared + model_verified_via + harness_version (bridge git
  SHA) + corpus_version + run_id`, body = the per-cluster × per-gate table + disclaimers.
  Model attribution per constraint 6 (config + engine version + billing delta; never
  self-report).
- **Trigger: manual CLI, deliberately.** `scripts/implbench run --seat <agent-id>
  [--task <id> …]` on the events the routing doc already names
  (`implementor-routing.md:296-303`): new seat stood up, model version change, harness
  engine change, >6 months. No scheduler, no hook, no auto-anything in v1 — a bench that
  runs itself is one step from a bench that is consumed automatically.
- Companion subcommands: `implbench validate` (adversarial self-check, V2 below),
  `implbench report <run_id>` (render from NDJSON).

### D6 — Cost (bench one seat at a time, by design)

Anchors from executed runs: 10-task qwen gate = **$0.049** total, 2m32s wall at
3-concurrent (`qwen-worker-seats.md:122-124`); model-alias-1 = free tier; bounded bake-off turns ran
seconds (qcn) to ~2 min (model-alias-1 hard family). Estimate for v1 (10 tasks, 2-concurrent,
900s/task ceiling): **cheap OpenRouter seats ≤ $1 and ~10–20 min; frontier API seats
ESTIMATE-UNTIL-CALIBRATED ~$2–20 (codex P2 + GLM P2-5: one cheap-model anchor does not
support a frontier band — V5 adds a measured frontier calibration run, and no artifact
carries a cost claim before it exists); codex/asdk plan-billed seats cost queue-time, not
dollars.** Worst-case wall clock is bounded at ~75 min by the timeout ceiling. One-seat-per-invocation keeps the bench from
ever monopolizing the fleet (fleet-restart discipline: other orchestrators share the bus),
and makes each artefact's key unambiguous.

## Rejected alternatives

- **Harvest tasks from <workspace> history** — in-distribution for resident seats, cluster-
  correlated (P1), rots with the repo, and success criteria would be reconstructed post-hoc.
- **Pin tasks to host-repo base SHAs** — dependency/env rot makes old SHAs unsolvable for
  reasons unrelated to the seat; the orphan-fixture mechanism keeps hard pinning without
  binding to host-repo content at all.
- **Standalone fixture clones outside the seat's workdir** — loses `--worktree` hard
  isolation (worktrees anchor at `AGENT_WORKDIR`, `bridge.py:986-1004`) and falls back to
  soft prose-`cd`, the documented footgun (`SKILL.md:111`).
- **Bench-dedicated seats** — measures a seat the fleet doesn't run (role/tools bound at
  launch); the artefact key would silently mean something else.
- **Ranked leaderboard / composite score** — D4; also the cursor 27/27-vs-0/3 inversion is
  an existence proof that the composite lies about operational fitness.
- **LLM-judged code-quality scoring** — reintroduces a legibility-bounded rater as the
  oracle, the exact §0 disease, one layer down. Style/clumsiness escalation remains a
  human routing trigger (`implementor-routing.md:131-133`, triggers 5–6), not a gate.
- **Reusing Instrument 1's defect corpus** — it measures *reviewing* (find the seeded
  defect); generation is a different act with different evidence; only the wall shape and
  the NDJSON/authority conventions carry over.
- **Auto-consumption into routing/trust** — hard wall; forecloses the arb-bench failure
  mode by construction, not restraint.

## Verification obligations

- **V1 — hermetic harness suite (CI, no engines):** deterministic fixture materialization
  (same tree ⇒ same orphan SHA, twice); each gate evaluator (G0–G7) exercised against
  crafted worktrees/commit graphs — G4's matrix includes impl-before-test,
  test-only-first-but-green-first, merge-commit, **single-commit (→ NOT-DEMONSTRATED),
  and orchestrator-rescued (trailer present → G2=RESCUED, G4=NOT-DEMONSTRATED)** shapes
  (round-1 P1-α); G2 payload-vs-trailer conflict ⇒ `evidence_conflict` UNKNOWN; G3 on
  allowlist edge globs; G6 parses a crafted JUnit report, never stdout; cluster pooling
  rule (FAIL > UNKNOWN > PASS precedence) pinned; UNKNOWN paths (evidence collection made
  to fail ⇒ UNKNOWN, not FAIL).
- **V2 — adversarial harness validation (`implbench validate`, hermetic):** scripted fake
  implementors drive the *full* pipeline: the **null implementor** (replies "done", commits
  nothing) must fail G1/G2 and flag G7; the **fabricator** (claims a SHA that doesn't
  exist) trips G7; the **scope-escaper** trips G3; the **test-weakener** (edits baseline
  assertions) trips G6; the **rail-breaker** (does the prohibited X) trips G5; the
  **discipline-skipper** (impl first, tests after) trips G4. For each gate: with the gate
  evaluator deleted/stubbed, its adversary must go GREEN and the meta-test must go RED —
  deny-proofs need adversarial verification; a guard that stays green when stubbed is the
  worst cheap-fake (vacuously-green-guard-fail-loud).
- **V3 — battery secrecy, structural (rewritten per round-1 P1-β):** CI asserts (a) no
  battery PLAINTEXT exists anywhere in the repo or fixtures (only `.enc` files; a grep for
  known battery-function signatures over the whole tree comes back empty), (b) no battery
  path appears in any fixture commit tree, (c) decrypt-roundtrip works and the battery
  runner asserts `module.__file__` resolves under the worktree (shadowing scar), and (d)
  the adversarial read: a simulated worker `cat` of the battery path yields ciphertext,
  and no battery plaintext exists on disk while a (stubbed) turn is active. Deleting the
  encryption (plaintext battery committed) must turn (a) red.
- **V4 — reporter wall:** unit test pins that the reporter schema contains no
  rank/composite/trust field and that injecting one raises; artifact text contains the P2
  residual-convertibility disclaimer.
- **V5 — live gate (required; live-verification-catches-cli-glue, two instances on
  record):** one full run against a known-good seat (`codex-bridge-dev`; 27/27 both
  bake-off rounds) expecting all-clusters PASS, and one against the model-alias-1 seat expecting the
  harness to *reproduce the known historical result shape* — C1's boundary battery red on
  the permissive-boundary task if the temperament persists, everything else green. The
  harness rediscovering a truth we already know from hand-run evidence is the calibration
  standard. **Plus (round-1 additions):** the fixture ref must resolve DURING the run
  (checked between enqueue and collect); post-run `git fsck --unreachable` accounting shows
  only intended residue (worktree list byte-identical; `refs/implbench/runs/<run_id>/*` AND
  `refs/implbench/results/<run_id>/*` present by design — the retained-evidence contract);
  **the prune adversary (round-2 codex P1): after worktree removal, run
  `git reflog expire --expire-unreachable=now --all && git gc --prune=now` in a MIRROR (all-refs — a default clone drops refs/implbench/*, breaking the adversary loud; r3 cold-Opus P2)
  clone of the seat repo, then `git worktree add` BOTH the fixture SHA and the result
  `head_after` SHA — both must succeed; delete the results-ref creation ⇒ this test goes
  red**; and ONE measured frontier-seat calibration run prices the D6 estimate before any
  artifact carries a cost claim.
- **V6 — inertness:** a fleet seat that never receives an implbench dispatch has zero new
  behavior; the bench is client-side only (dispatch + harness), no bridge code changes in
  v1 (the completion gate is consumed, not modified).

## Open forks (round 1 resolved #1; the rest stand for Mark)

1. ~~Orphan vs visible ref~~ — **RESOLVED by round-1 P1-γ:** a dedicated
   `refs/implbench/runs/*` namespace is mandatory (GC-safety + evidence reachability); it
   is visible to `git for-each-ref` but outside every normal workflow's refspec, which the
   panel judged the right trade.
2. **Remote-host seats** (forge): v1 excludes; is a push-a-ref variant worth specifying now?
3. **G7 prose-claim auditing** (v2): worth the judgment-layer cost, or does the mechanical
   subset suffice indefinitely?
4. **Corpus refresh cadence**: variant-rotation at every model-version comparison, or only
   at the 6-month re-gate?

## Non-goals (v1)

- Rung-3 (judgment/architectural) fitness appraisal — outside every objective-gated corpus
  by construction; stated in the artifact disclaimer.
- Cross-seat rankings, composite scores, trust/quorum/routing consumption — walled.
- Reviewer-role appraisal — Instrument 1's job (`eval-suite-design.md:53-79`).
- Replacing the real-codebase re-gates (project-f-style) — this bench is the *standing*
  floor; the periodic real-repo gate remains the texture check, per
  `implementor-routing.md:294-308`.
- Multi-seat batch invocation; scheduling/automation; bridge-side code changes; style or
  code-quality judgment beyond the objective gates.
