# Multi-Agent Pipeline Operating Manual

A reusable, project-portable workflow for shipping features through a **plan → codex implementation → agy-print review** loop coordinated from Claude Code. The pipeline takes a half-described idea and produces a fully-reviewed merge-ready branch with bounded human attention.

This manual is engine-mechanic-agnostic — the same shape works in any Laravel / TypeScript / Python codebase. Project-specific tokens (paths, agent IDs, env files) are called out as `<<placeholders>>` so this document drops into a new repo with a find-and-replace.

---

## When to use this pipeline

Reach for it when a feature has all of:

- **Non-trivial scope** — 1+ day of work, multiple files touched, observable customer behaviour changing
- **Crisp acceptance criteria** — you can describe what "done" looks like in a few sentences
- **Independent reviewability** — the change can be evaluated against the spec without deep prior context

Don't reach for it when:

- The task is a one-line fix, hotfix, or trivial cleanup (just do it)
- The scope is genuinely open-ended exploration ("what could we do about X?") — that's a brainstorming session, not a dispatch
- The work spans cross-system migrations or coordinated deploys where a single agent can't see all the moving pieces
- Time-to-resolution matters more than reviewability (production incident — skip the loop, fix directly)
- **The load-bearing facts are an EXTERNAL system's behaviour, and you haven't characterized it yet** — see the next section. **Characterize first; then run the pipeline over the results.**

The pipeline trades real-time speed for verified correctness. If you're optimising for human-attention-per-unit-of-shipped-code, it wins. If you're optimising for wall-clock latency, it doesn't.

---

## Characterize before designing, when the object is an external system's behaviour

**The precondition this pipeline silently assumes: the facts are in the repo.** Design → panel →
spec → panel → plan → panel → impl works because reviewers can *read* the code the design describes
and check it. That assumption fails completely when the design object is an **external system's
behaviour** — a CLI, a binary, a protocol peer, a vendor API. There, **source tells you what you
SEND and nothing about what it DOES.**

**A panel of LLMs cannot produce a behavioural fact at any price.** It is an excellent detector of
*unsupported claims* — tier violations, bad citations, contradictions — and a **terrible oracle of
behaviour**. Asked "what does X do?", the best a reviewer can honestly return is *"this claim is
unsupported"*, which is **a pointer to a probe, not an answer**. Run the pipeline anyway and it
degenerates into: **author asserts → panel says unsupported → a six-minute probe settles it** — the
expensive instrument used to discover which cheap instrument to run, one fact at a time, per round.

### The rule

> **If a load-bearing fact is obtainable only by executing the thing, execute it — BEFORE any model
> authors or reviews. Build the characterization matrix from the protocol/config surface alone (it
> needs zero design input), publish it as the authoritative evidence artifact, and only then author
> against a closed table.** The panel's job becomes *"does this design follow from this evidence?"* —
> reasoning over ground truth, which is what panels are genuinely good at.

### The incident this came from (2026-07-17, kimi-bridge-dev seat arc)

Standing up one adjunct seat on an existing engine took **6 design versions and 4 panel rounds**,
every one of which died on a behavioural claim about the kimi CLI:

| Version | Central claim | Reality (one probe) |
|---|---|---|
| v1, v2 | guessed at mode behaviour | blocked, unanimous, twice |
| v3 | `auto` gates unsafe ops ⇒ reject them | **`auto` writes with ZERO permission asks** ⇒ D5 void |
| v4 | `plan` + inherited responder is safe | true — but the **deny loop never terminates** on some prompts |
| v5, v6 | `default` is "empirically inert" (a 6-week-old docstring) | **`default` WRITES** — `Write` executes with no ask |

**Every one of those is a single cell in a matrix that was derivable from the FIRST handshake.** The
opening `configOptions` probe returned all four modes. From that moment this was computable with
**zero design input**:

```
4 modes × {read, create-in-cwd, modify-tracked, write-out-of-cwd, shell}
        × {cooperative, adversarial}
        × {responder: cancelled, allow, reject}
```

≈60 cells · 30–100s each · one unattended script · **~90 minutes**. It would have answered **every
question that blocked every version**, before a word was written. Instead: ~20 seat-reviews of a
1000-line document across four rounds, and **not one of them settled a behavioural question** —
each round ended with a probe.

### How to run a characterization suite (the rules are load-bearing; each was learned the hard way)

1. **Termination is a first-class result.** Assert `stopReason == end_turn` before reading any
   negative as a pass. A client-side timeout produces a **green-looking** "nothing happened" that is
   **INDETERMINATE** — the arc produced three such rows from a 180s ceiling and nearly believed them.
2. **Adversarially self-test the detector first.** Simulate the effect ⇒ it must go RED; reset ⇒
   byte-identical. A "blocked" result from an untested detector is vacuous
   (`docs/superpowers/` · the deny-proof rule).
3. **Disposable target.** Write cells go against a throwaway `git worktree add --detach` checkout —
   the property under test is often *target-inside-cwd*, and **some cells will really write**.
4. **Catalogue every protocol shape verbatim.** In the kimi case, option **IDs turned out not to be
   stable across ask shapes — only `kind` was** — so any responder keying on IDs is wrong by
   construction. **Any enumeration you produce is a LOWER BOUND, never a closed set.**
5. **Re-run known-answered cells as the suite's own control.** A disagreement means the harness is
   wrong or the world moved. Both outrank the design.
6. **State the residue the matrix cannot close** — rare silent divergences (accept-but-not-apply),
   multi-turn state (warm-session leak needs a *two-dispatch* cell), and **prompt-dependent families**
   (behaviour is a function of the prompt, so the space is unbounded in principle — sample it, never
   claim closure).

### The trap that hid this for four rounds

After the first probe overturned v3, the orchestrator adopted *"probe the nominated weakest claim
before each panel."* It went **4-for-4** — and *felt like the process working*, which is exactly what
stopped anyone asking whether the loop was shaped wrong. **A local improvement that keeps paying off
is the best camouflage a structural error can have.** When a mitigation is succeeding, ask what it is
mitigating *for*.

Full record: `docs/superpowers/reviews/2026-07-17-kimi-spike-findings.md` (evidence),
`docs/superpowers/probes/2026-07-17-kimi-spike/` (harnesses), ARB Memory artefact
`characterize-before-designing-external-behaviour`.

---

## Workflow selection — A, B or C

This manual ships with **three workflow shapes**. Pick one per project (or per phase within a project) based on risk tolerance and expected context accumulation.

**At project kickoff, Claude must confirm with the user which workflow to use before dispatching the first task.** When this manual is first loaded into a new project's `CLAUDE.md` reference set, the agent's first move is to ask: "Workflow A (lightweight), B (rigorous parallel + cold-opus final), or C (bounded-context rounds — FABA subagents + author offload) for this project?" Default to A unless the user says otherwise or the project's risk profile clearly demands B or C. A and B remain fully available; C does not replace them.

| Dimension | A: Lightweight | B: Rigorous |
|---|---|---|
| Branching | one feature branch off main; tasks commit on it | feature branch + **task branches off feature** |
| Per-task review | agy-print reviews codex's commit (serial) | codex self-review **and** agy-print review run **in parallel**; re-review loop after remediation |
| Pre-merge review | none (per-task agy-print suffices) | **triple review**: codex full-feature + agy-print full-feature + **cold-opus** full-feature (fresh context, judges only against spec) |
| Wall-clock per task | baseline | ~1.3× baseline |
| Risk caught | most issues at task boundary | adds "implemented exactly what the reviewer expected" tunnel-vision catch via the cold-opus context-fresh pass |
| Use when | new product surfaces; reversible changes; greenfield features; most CRUD; UI work | data migrations; behaviour-preservation under rewrite; regulated paths; irreversible single-shot ops (re-encryption, schema, backfills); high-stakes prod cutovers |

Mixed projects can run A for most phases and B only for high-stakes ones. Example pattern: a legacy-to-Laravel migration where Phases 1 (read-only reports) and 2 (CRUD UI) are A, and Phases 3 (background worker cutover that writes real customer data), 4 (correspondence — touches real customer emails), and 5 (production cutover/decom) are B. Declare the choice per phase in a project-local `docs/phase-workflow.md` (template: [`./phase-workflow.template.md`](./phase-workflow.template.md)) so a fresh Claude session reading the project knows what cadence to expect.

**Where C fits in that table:** C is orthogonal to the A/B risk axis — it changes *where content lives*, not how much review runs. Its review depth is configurable per round (a C round can carry an A-shaped single reviewer or a B-shaped multi-seat panel). Choose C when the work item is long enough that warm-session context accumulation becomes the dominant cost or quality risk (multi-stage design→spec→plan→build arcs, repeated remediation rounds, panel-scale reviews); stay on A/B for short arcs where a warm session never grows past ~80k context — C's per-round process overhead isn't paid back there.

## The light path (co-signed Mark, 2026-07-20) — record-adjudicated fixes skip the panels

A ceremony floor for the ONE structurally-safe case, adopted after a four-seat design panel
unanimously refuted a classifier-gated light tier (run
`panel-light-tier-design-20260720T161509Z-2f17be`: a branch-controlled harness cannot verify
its own branch — pyproject/conftest own the pytest run that judges them; deny-lists name
entrypoints while integrity lives in their dependencies). Per the enforcement-sizing
doctrine, the adopted path is sized to the accident actually possible:

- **Eligibility (nothing else qualifies):** the change remediates finding(s) adjudicated
  **P2 or P3 in a PUBLISHED FABA record**. The record — already panel-grade adjudication of
  the defect and its fix direction — is the review; a fresh panel would re-review it.
- **Path:** worktree fix-dispatch with run-id (unchanged from standard) → **mandatory
  orchestrator diff-read** (the one gate no branch can influence; not skippable) →
  independent orchestrator test run → merge. No design panel, no imp panel, no audited
  close per change.
- **Never light:** protected/instruction files (the CLAUDE.md set), FABA/bridge/memory
  integrity surfaces, tier machinery itself, anything remediating a P0/P1, anything not
  grounded in a published record. When in doubt: standard. **Escalation is one-way** —
  anyone may escalate an eligible change to standard, no reason required; nothing escalates
  down.
- **Ledger:** the merge commit cites the record id + finding ids (trailer
  `Light-Path: <record-artefact-id> <finding-ids>`). Landings are swept into ONE **batch
  fold** triggered by whichever comes first: weekly, 5 accumulated light landings, or
  before any standard-tier fold (a standard fold never jumps unswept light landings — ADR
  lag is bounded by the next standard landing, not the calendar). The sweep fold is a
  normal author round (gates, spot-diff).

Precedent lineage: exercised twice owner-present as the "D7 route" (F29/F24 bundle
`0aec5cc9`; r6 hygiene cluster `756962ed`) before formalization.

## The rival-instrument probe (co-signed Mark, 2026-07-29) — REQUIRED, all workflows

After each remediation lands and after each deployment-readiness claim, the lead runs a
**rival-instrument probe** BEFORE the next reading round is dispatched, and the round brief
cites the probe results as prior art:

- **After a remediation:** execution — constructed hostile inputs against every check the
  remediation touched. The finding class in remediation tails is predictably
  constructed-input-catchable (Captured Loop doctrine `art-a03c92a4cbf72de0` rule 2); the probe
  makes the next panel round a confirmation, not a discovery, or replaces it entirely.
- **After a deployment-readiness claim:** live probes against **every claimed target — n-of-n.
  A claim's scope may not exceed its evidence's scope**: one seat's proof supports "this seat is
  ready," never "the fleet is ready"
  (`docs/defect-classes/claim-scope-exceeds-evidence-scope.md`, first occurrence E26).

Execution is thereby a scheduled *discovery* instrument, not the lead's verification-only tool.
Origin: the wave-1 evidence bundle and Captured Loop self-review, `art-1d78d15acf8816c3`
(Slice 1d: r2–r3 findings were execution-class, found by reading at panel prices; the 13-seat
live sweep found in 20 minutes what 16 panels could not). Drafted in that artefact's amendment
section; co-signed at REQUIRED strength by owner review 2026-07-29 per the doctrine-strength
co-sign rail.

## Workflow C: bounded-context rounds (FABA subagents + author offload)

**Adopted 2026-07-19 (owner-directed).** C runs the same stage sequence as A/B but nothing that *produces or adjudicates content* ever runs in the orchestrator's context. The warm session becomes a cockpit holding only: work item ID, stage, open-findings ledger (IDs + severities), artefact/version pointers, and the commit gate. The invariant that makes it checkable: **if the cockpit's context ever contains an artefact body or a verdict body, something leaked.**

Reference artefacts (ARB Memory): the FABA ADR `art-81438f2f5a5c4955` (v13+ — the decision record, contract, and measured economics) and the plain-language explainer `art-96faf77da9149e80`. Machinery: `tools/faba/` on the ARB clone (subagent form: `.claude/agents/faba-round.md`, `run_probe_round.py` driver, SubagentStop content gate, harness-side publish + receipt).

### Stage shape (one stage, end to end)

1. **Author round (subagent, not a bridge seat).** The cockpit spawns a fresh author subagent with parent-minted ids and a brief built from POINTERS: the approved prior-stage artefact id + the latest decision-record id. The tool-bounded author cannot read Memory; it reads only the inputs the DRIVER stages in its workspace (`author-input.json` and, when applicable, `prior-record.md`). For a revision, the driver fetches the current artefact body from the store at arm time and stages it as `prior-record.md` (or uses a validated `--prior-record-file` override); for a fresh draft, `--prior-record-file` can stage a decision record. `--prior-artefact-id` remains a provenance pointer in the brief and input metadata — the prior-stage artefact body is not materialised. The author writes the draft artefact to its round workspace and returns ONLY `{artefact_id, version, one-line change summary}`. The body never transits the Task result — under contract v2 the harness validates the workspace content, publishes to Memory itself, and verifies its own receipt; the agent cannot satisfy the gate by prose.
2. **Review round (FABA subagent).** The cockpit spawns the FABA round agent (model: fable — evidence-backed, see below) with the subject artefact id + round N + prior record id. FABA dispatches the panel via the bridge (bridge seats are for *reviewers*; author and synthesiser are native subagents), verifies hinge claims with filesystem + exec, writes the round's decision record; harness publishes + receipts; cockpit gets `{record_id, status, recommendation}`.
3. **Cockpit adjudicates pointers only:** carry/close findings by id, surface forks to the owner, gate the merge. Remediation = a fresh author round against the new record. Fresh instance per round is the author-non-quorum guarantee — author ≠ verifier by construction, no extra machinery.

### Why fresh authoring context is a quality gain, not just a token saving

An author booting from *what was certified* (approved artefact + decision record) writes against the certified state, not against 200k of conversational residue including its own rejected drafts. This is the same succession discipline the decision records enforce for verification, applied to authoring. Measured on the synthesis side (2026-07-19, same-inputs A/B): the fresh Fable synthesiser was ~27% cheaper than the warm-style Sonnet pass AND recovered three findings the other pass silently dropped.

### Model tiers per seat

- **FABA (synthesis/verification): Fable** — evidence-backed on both economics (64.9k peak vs 101.3k; ~371k vs ~505k eq) and coverage (ADR changelog v13b).
- **Author rounds: the owner-set per-stage choice stands** (inline-cold / Opus / Fable, asked ONCE up front — see the skill's per-stage authoring rotation). The 2026-07-03 authoring bake-off calibration is NOT superseded: the panel is the verification backstop, so don't default to the strongest author; reach for a Fable author only on blank-page, no-tools-hard stages.
- **Cockpit: Sonnet-class.** It holds pointers and runs process; capability there is wasted spend.

Fable plan credits get touched at exactly two bounded, countable points per stage (FABA round, and the author round only if Fable-authored).

### Load-bearing rails (each learned the hard way)

- **Return-channel discipline is where C lives or dies.** A subagent Task result that contains the document has defeated the workflow. Pointer-only returns; the harness publishes. The SubagentStop gate + harness receipt make this structural rather than remembered.
- **One tool call, one timeout ceiling.** A round agent's blocking wait on dispatched seats must fit inside a single tool-call timeout — a batched multi-dispatch `wait` that outlives the ceiling orphans the dispatchers even though the seats survive (proven 2026-07-19; recovery = reconcile-then-resume from durable `task:<id>:result` keys, zero re-dispatch, votes append-only).
- **Probe each new agent definition's tool bound.** The faba-round definition was probed (ToolSearch unavailable in-subagent; account connectors unreachable) but that result is per-definition: a new author definition re-runs the probe before first production use. Assume additive until proven otherwise — that assumption has already failed once on the SDK-harness path.
- **Panel composition and workflow selection stay owner decisions** (CLAUDE.md constitution layer); the audited run-id/roster/vote/close discipline is unchanged from A/B.

### Adoption status

Both halves are live-proven. The FABA review-round half at panel scale (three rounds, audited close, crash recovery exercised, 2026-07-19). The **author-round arming landed the same day**: pointer `kind: "author"` switches the stop gate to the light authored-artefact check (`validate_authored_artefact` — title + change-summary + stub floor), `run_author_round.py` drives the round, and `publish_artefact_and_gate` publishes harness-side with its own receipt. Live probe `art-faba-au-b7b8cdd8`: content gate passed first-stop, receipt verified, and the author's entire reply was the 284-char `FABA_EXIT` pointer line while the 78-line artefact travelled only via workspace → harness → Memory. The `faba-author` agent def carries no model pin on purpose — the driver's `--child-model` is the per-stage author-tier lever.

## Isolated Implementor Bench operator loop

The Open Interpreter versus Pi bakeoff is a report-only, controller-owned workflow. Tasks 1–13
are hermetic implementation and verification work; Task 14 is the separate live readiness gate.
Do not run `pilot`, `run`, or any provider-backed calibration from an implementation checkout.
The manifest and evidence root must be controller-owned absolute paths; credentials stay in the
operator's private seat configuration and never appear in argv, manifests, logs, or this manual.

From a clean checkout, the read-only validation and hermetic regression commands are:

```sh
scripts/implbench validate
env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q
PYTHONPATH=bench env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q bench/implbench/tests
scripts/check-doc-recipes
scripts/check-doc-drift
scripts/check-seed-canon
git diff --check
```

`check-seed-canon` verifies that every `docs/agent-memory-seeds/` topic cites the **current**
version of its ARB Memory canonical. Seeds are downstream copies installed into machine-wide
global stores, so a stale pointer is read unbidden by every interactive CLI on the host, in every
repo, until someone notices by hand — which is how `arb-seat-roster` spent ~24h citing
`arb-seat-scorecard` v1 while the store was at v3. Without `ARB_MEMORY_DSN` it checks citation
shape only and still catches intra-file version conflicts; pass `--require-store` where store
access is guaranteed, and `--strict` to reject floor pins (`v13+`) and unversioned citations.

A correct corpus is not the same as a correct store — the fan-out is where drift actually
happens. On a host that runs orchestrator CLIs, also run:

```sh
scripts/check-seed-canon --check-stores
```

This checks the live stores (`~/.codex/auto-memory/global`, `~/.pi/agent/memory/global`, and
`$CLAUDE_HARNESS_MEMORY_DIR` when set). Verbatim stores are byte-compared against the corpus and
divergence fails — record a genuine fork with `--allow-divergent <topic>` rather than letting it
sit. Adapted stores (pi restructures its copies on purpose) are pointer-checked only. Absent or
unconfigured stores are reported, never silently skipped. It reads host state, so it is **not**
part of the clean-checkout list above — CI has no `~/.codex`.

For an already sealed evidence package, use its absolute root with the read-only commands:

```sh
scripts/implbench validate --evidence <absolute-sealed-evidence-root>
scripts/implbench report --evidence <absolute-sealed-evidence-root>
```

`validate` must exit non-zero for a missing, changed, unsealed (when a closed package is
required), non-canonical, or secret-bearing package. `report` is read-only and must reject an
unsealed or malformed package; its output is pair-analysis-v1 only, with no rank, composite
score, leaderboard, trust, quorum, or promotion claim. A `FAIL` is a measured contract failure;
`UNKNOWN` is infrastructure or provenance that is not safe to score; `NOT_SCORED` is an
intentional empty/not-delivered path. Stop the run and retain append-only evidence for any
unexpected result—do not retry by rewriting a model outcome or weakening a validator.

The mutating phase commands are intentionally controller-bound and serialized. They require an
absolute manifest and refuse `--concurrency` values other than `1`:

```sh
scripts/implbench preflight --manifest <absolute-controller-owned-manifest>
scripts/implbench calibrate --manifest <absolute-controller-owned-manifest> --seat <seat-id>
scripts/implbench pilot --manifest <absolute-controller-owned-manifest>
scripts/implbench run --manifest <absolute-controller-owned-manifest>
```

On a hermetic checkout these commands should fail closed with “production … runtime is not
bound”; that is a setup signal, not permission to substitute a fake or call a live provider.
`prune` likewise requires an explicit evidence root and only deletes eligible ordinary refs:

```sh
scripts/implbench prune --before <YYYY-MM-DD> --evidence-root <absolute-evidence-root>
```

If packaging or CI needs a path outside the task-owned files, record an Escalation with the
frozen requirement, conflicting evidence, failed command, and why the scope cannot remain
closed; do not broaden a worker change silently.

---

## Workflow A: lightweight single-reviewer (stage shape, one feature, end to end)

```
Idea
 │
 ▼  brainstorming skill (Claude + user) — one question at a time
SPEC: docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md   (committed to main)
 │
 ▼  writing-plans skill (Claude) — bite-sized TDD tasks, exact code in every step
PLAN: docs/superpowers/plans/YYYY-MM-DD-<topic>-plan.md      (committed to main)
 │
 ▼  pre-create worktree on feat/<topic> branched from main
 │  (CRITICAL — see § "Worktree-before-dispatch" below)
 │
 ▼  brief-to-file for codex dispatch
BRIEF: docs/superpowers/reviews/YYYY-MM-DD-codex-<topic>-impl-brief.md   (committed to main BEFORE worktree creation)
 │
 ▼  loop: for each task N in [1..N]
 │     ├── dispatch codex: "Execute Task N from <plan path>"
 │     ├── codex commits in the worktree, replies with SHA + test counts + deviations
 │     ├── write agy-print review brief (project-specific concerns + codex's deviations)
 │     ├── dispatch agy-print for independent review against spec + plan + commit diff
 │     ├── agy-print writes report INSIDE worktree (so it ships with the branch)
 │     └── triage: APPROVE → next task; REQUEST CHANGES → focused fix dispatch
 │
 ▼  final full-suite regression from worktree
 │
 ▼  git merge --no-ff feat/<topic> (preserves branch boundary + review trail)
 ▼  delete worktree + local branch (remote branch usually never existed — codex worked locally)
```

The output: a merge commit on main that ships:
- The implementation
- The plan and spec (audit trail)
- The agy-print review reports per task (independent verification record)
- Tests covering the new behaviour

If a future contributor wants to understand "why was it done this way?", `git log` on the merge commit surfaces the spec, the plan, and the four agy-print reports — all the rationale lives in the repo, not in a planning tool you'll churn through.

---

## Workflow B: rigorous parallel-review + cold-opus final

For high-stakes work where the lowest-possible regression risk justifies more wall-clock time per task. Adds two structural changes on top of Workflow A: task-level branching (so each task has its own scope to abandon or remediate without polluting the feature branch) and parallel reviewers per task (codex self-review + agy-print independent review) with explicit re-review after remediation. Plus a feature-level triple review (codex + agy-print + cold-opus) before the feature merges to main.

> **Who authors each stage is a choice (per-stage authoring rotation).** The initial draft of the
> design, spec, and plan can be authored **inline** (warm orchestrator), by an **Opus subagent**, or by a
> **Fable subagent** — asked **once, up front** (one question batch for all three stages) so an autonomous
> run stays hands-off. The chosen author writes the **initial draft only**; the warm orchestrator owns
> panel dispatch, synthesis, and all remediation. This generalises the same author-non-quorum discipline
> the P0/P1 re-panel below rests on: whichever model lineage authored a stage, that lineage's reviewer goes
> **non-certifying** on that stage's panel. Since all three authoring options are Anthropic lineage,
> **cold-Opus reviews but does not certify** a design/spec/plan panel; the certify quorum is
> **codex (GPT anchor) + pi-GLM + agy-print**. A Fable authoring dispatch applies
> [`./prompting-claude-fable-5.md`](./prompting-claude-fable-5.md). Full mechanism: the `using-agent-bridge`
> skill § "Per-stage authoring rotation for design / spec / plan", and ARB Memory artefact
> `art-49c566cc076f374a`.

> **Plan-stage pre-flight (REQUIRED for plans with fake-based tests): run
> `scripts/plan-fixture-smoke <plan.md> --task=<N>` at each dispatch boundary, before
> dispatching task N.** Static plan panels reliably catch embedded-test CONTRADICTIONS
> but are structurally blind to two claim sub-species whose falsification requires
> execution — (A) a fixture that cannot satisfy a predicate the plan's tests assert
> against it (GROK-1 pooled-engine lifecycle; ENG-1 `is_healthy()` on process-less
> fakes), and (B) a red-phase that was never red: a test the plan claims fails
> pre-edit but that passes, i.e. an inert pin that would ship as coverage covering
> nothing (ENG-1 Task 3). Three specimens, all surfaced as implementor BLOCKEDs at a
> dispatch round-trip each; the smoke converts that accidental last-resort
> verification into a deliberate pennies-cost gate. Plans embed `python fixture-smoke`
> blocks (optionally `task=N`-tagged) that exercise the strongest predicate form each
> fake must satisfy, plus `red_claim(<test-source>, expect_fail=[...])` calls for every
> pre-edit-failure claim. The panel keeps owning coherence; the smoke owns runtime
> semantics of tests and fixtures. Exemplar: the ENG-1 plan's "Pre-flight fixture
> smoke" section (its deny-proofs re-introduce both historic bugs and go red).
> The runner also exposes `TREE` (via `--tree=<worktree>`) so plans assert
> ORCHESTRATOR world-claims — provisioning, dependency presence, absolute report
> paths — before dispatch (built from the 2026-07-11 node_modules near-miss, i.e.
> from a specimen the pipeline had paid for only once).

> **Seat-standup pre-flight:** before bootstrapping a new or changed launchd/systemd seat,
> run `scripts/seat-preflight <plist-or-env-path>`; add `--strict` to make warnings fail.
> It checks the env-file and Redis requirements, local MCP DSN derivation, cross-store
> policy, workdir, executable/working-directory paths (plist mode), and trusted senders.
> This is the seat-provisioning member of the pre-flight family: falsify the seat-world
> claims before an engine start can queue work against a broken seat.

> **Implementor-BLOCKED triage (calibrated 4-for-4, 2026-07-11):** under the
> transcription regime, a BLOCKED is a *precondition-falsification detector* — from
> inside the worktree, a false plan-claim and a false world-claim are the same event:
> an assumption the brief treats as ambient did not hold. Do NOT debug the seat.
> Diff the assumed world against the actual one, in base-rate order: (1) the plan's
> claims (fixture semantics, red-phase expectations, anchors — 3 of 4 specimens),
> (2) the orchestrator's provisioning (worktree deps, resolvable paths — 1 of 4).
> Tripwire corollary: every BLOCKED so far produced its fix commit somewhere OTHER
> than the worktree. The day a BLOCKED's fix lands in the worktree is the day this
> calibration broke — treat that as its own incident.

> **Arc-closure constitution sweep (adopted 2026-07-11, Mark co-signed; rail 2 of
> CLAUDE.md § Constitution-layer discipline):** every arc close / handoff includes a
> standing section listing the arc's constitution-layer touches on TWO tracks:
> (a) decisions with NO authority trail — true crossings; (b) decisions riding
> STANDING RAILS (workflow-includes-merge, fold-and-proceed, precedent) — because
> drift-by-accumulation is how constitutions actually erode: not violation but
> accretion, each rail individually authorized, the sum never reviewed. Grep set:
> merges to dev; roster/certify strings in panel briefs; writes to CLAUDE.md,
> AGENTS.md, this manual, skills/; memory_store calls; severity adjudications in
> commit messages. Each touch names its authority (explicit ask / standing rail /
> none). Companion rail: REQUIRED/MUST-strength doctrine needs Mark's co-sign
> before it binds future runs.
>
> **Merge-rail status (Mark, 2026-07-11, on the sweep's first formal output):** the
> `workflow-includes-merge` rail is KEPT by explicit choice — within an arc Mark
> ordered, merges to dev proceed on unanimous final review without a fresh per-merge
> nod. The sweep had flagged it as drift-by-accumulation risk after carrying two
> merges (`805736b`, `fca9a50`) unreviewed; it is now chosen, not accreted. Future
> sweeps count accumulation from this affirmation.

> **Incident records timestamp the world (REQUIRED; Mark co-signed 2026-07-11, the
> concrete GOV-1 yield):** every drift record, BLOCKED postmortem, and incident
> artefact carries a `world_at:` field — the commit SHA(s) of the tree(s) the incident
> occurred against (plan/dev SHA + worktree SHA where distinct) — and, when the faulty
> text was later amended, either the verbatim pre-image or its git ref (the fix
> commit's parent). WHY: the stores are current-state stores; remediation OVERWRITES
> the evidence (spoliation-by-repair). GOV-1's single replay divergence was a sound
> judge reasoning from a post-fix file with no signal it post-dated the incident —
> succession, audit sweeps, and incident review all need point-in-time reconstruction
> to be a lookup, not archaeology a successor may not know to attempt. Git already
> holds the pre-images; this rule mandates the POINTER.

### Stage shape (one feature, end to end)

```
Idea
 │
 ▼  brainstorming skill (Claude + user)
SPEC: docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md         (committed to main)
 │
 ▼  writing-plans skill (Claude)
PLAN: docs/superpowers/plans/YYYY-MM-DD-<topic>-plan.md           (committed to main)
 │
 ▼  pre-create feature worktree on feat/<topic> branched from main
 │
 ▼  brief-to-file for codex dispatch
BRIEF: docs/superpowers/reviews/YYYY-MM-DD-codex-<topic>-impl-brief.md  (committed to main)
 │
 ▼  loop: for each task N in [1..N]
 │     ├── pre-create task worktree on feat/<topic>/task-<N> branched from feat/<topic>
 │     ├── dispatch codex: "Execute Task N from <plan path>"  → commits in task worktree
 │     ├── dispatch reviewers in PARALLEL against the task commit:
 │     │     ├── codex self-review brief: "Did you produce what the plan said? Surface deviations."
 │     │     └── agy-print review brief: "Independent — does this match the spec?"
 │     ├── BOTH APPROVE → merge task worktree's feat/<topic>/task-<N> into feat/<topic>; loop next task
 │     ├── EITHER REQUEST CHANGES → focused remediation dispatch to codex
 │     │     └── re-review the changed scope (not necessarily the whole task)
 │     └── max remediation rounds: 3. After 3, escalate to human triage — usually a planning gap, not an impl bug.
 │
 ▼  full-suite regression from feat/<topic> worktree (all tasks merged in)
 │
 ▼  full-feature review BEFORE merging feat/<topic> → main:
 │     ├── dispatch codex full-feature review brief (impl perspective — every task lands as planned?)
 │     ├── dispatch agy-print full-feature review brief (independent perspective — spec coverage?)
 │     └── dispatch COLD-OPUS full-feature review brief (fresh-context Claude session,
 │         NO conversation history of this phase — reads only the spec + git diff main…feat/<topic>)
 │     ALL THREE APPROVE → git merge --no-ff feat/<topic> main
 │     ANY REQUEST CHANGES → remediation cycle on the rejected scope, then re-review only that scope
 │
 ▼  delete worktrees + branches; push main
```

### Why the cold-opus final review

Codex saw every task as it was implemented. Agy-print reviewed each task as it was committed. Both are *in-the-loop* — their judgment is anchored to "implemented exactly what the previous reviewer/I expected." That tunnel-vision is a real review failure mode and it's invisible from inside the loop.

A fresh Claude Code (Opus) session with zero conversation memory of this phase, reading only the spec and the merged diff, can only score the work against the spec. It catches the gap between "what the plan iteratively converged toward" and "what the spec actually asked for" — a gap that opens up subtly when each task review only sees its own slice.

### What gets committed where (Workflow B)

| Surface | `main` (before worktree) | `feat/<topic>` (feature branch) | `feat/<topic>/task-<N>` (task branch) |
|---|:--:|:--:|:--:|
| Spec | ✓ | – | – |
| Plan | ✓ | – | – |
| Codex impl brief (shared across tasks) | ✓ | – | – |
| Agy-print review briefs (one per task) | ✓ | – | – |
| Cold-opus full-feature review brief | ✓ | – | – |
| Codex implementation commits per task | – | – | ✓ |
| Codex self-review reports per task | – | – | ✓ (committed by codex in the task worktree) |
| Agy-print review reports per task | – | – | ✓ |
| Codex / agy-print / cold-opus full-feature review reports | – | ✓ (committed against feat/<topic> after all tasks merged) | – |

Task branches merge into the feature branch with `--no-ff` so the per-task review trail (codex impl + parallel review reports) stays visible in `git log --graph` on the feature branch. Feature branch merges into main with `--no-ff` so the full-feature review trail stays visible on main.

### Per-task loop (Workflow B)

After the previous task's task-branch has been merged into `feat/<topic>`:

1. `git worktree add -b feat/<topic>/task-<N> .claude/worktrees/<topic>-task-<N> feat/<topic>`
2. Dispatch codex with the worktree path + task-N reference: `"Execute Task N from <plan path>"`
3. Codex commits in the task worktree, replies with SHA + test counts + deviations
4. **Dispatch BOTH reviewers in parallel** (use `Bash(run_in_background=true)` for both):
   - Codex self-review with brief: did the implementation match the plan? Surface any deviations.
   - Agy-print review with brief: independent check against spec + plan.
5. When both notification fires resolve, read both verdicts:
   - **Both APPROVE / APPROVE WITH NOTES** → `git -C <feature-worktree> merge --no-ff feat/<topic>/task-<N>`; remove task worktree; loop next task
   - **Either REQUEST CHANGES** → focused remediation dispatch to codex referencing the rejecting reviewer's exact prescription
6. After remediation: re-review the changed scope (not necessarily the whole task — bound the work to what changed)
7. Bound the remediation loop at **3 rounds**. If the third round still rejects, escalate to human — that's almost always a planning gap (the spec or plan didn't decompose this task cleanly), not an implementation problem to keep grinding on.

### Pre-merge feature review (Workflow B)

After all tasks have been merged into `feat/<topic>`, before the feature merges into main:

1. Run full regression from the feature worktree
2. Write three full-feature review briefs (all committed to main):
   - `docs/superpowers/reviews/YYYY-MM-DD-codex-<topic>-feature-review-brief.md`
   - `docs/superpowers/reviews/YYYY-MM-DD-agy-<topic>-feature-review-brief.md`
   - `docs/superpowers/reviews/YYYY-MM-DD-coldopus-<topic>-feature-review-brief.md`
3. Each brief includes: spec path, plan path, `git log feat/<topic>` summary, full `git diff main…feat/<topic>` reference, the acceptance criteria from the spec to score against, output format (verdict + findings + spec-coverage table)
4. Dispatch all three reviewers (they're independent; can be parallel):
   - Codex: same bridge engine as the per-task self-reviews
   - Agy-print: same bridge engine as per-task reviews
   - Cold-opus: see "Cold-opus dispatch mechanism" below
5. All three commit their reports inside the feature worktree (so they ship with the merge)
6. Triage:
   - All APPROVE → `git merge --no-ff feat/<topic>` to main
   - Any REQUEST CHANGES → focused remediation on the rejected scope, then re-review only the rejected scope (don't re-run all three on unchanged code)

### P0/P1 remediation → mandatory re-panel (dispatched, concurrent)

Any remediation of a **P0 or P1** finding must go **back through the decorrelated panel** before the
finding is treated as closed. Orchestrator self-verification of the fix (reading the diff, confirming
the commit, running the suite) is necessary but **not sufficient** to close a P0/P1 — it is
author-adjacent verification and shares the framing that produced the fix in the first place. The
remediation is new code that no decorrelated lens has seen; it must be paneled like any other
artifact, not waved through because it's "just a fix."

The re-panel is **dispatched, not self-performed**: the orchestrator dispatches the remediated
artifact back to the panel (the same reviewers who found the issue, or the full panel) and does its
own review **concurrently while the panel runs** — it does not block on hand-reviewing the fix, and
it does not substitute its own read for the panel's verdict.

The re-panel must verify that the **defect is gone**, not that a change was made — confirm the
resolved condition actually holds (e.g. "the cap is now honest," "the raw exception can no longer
reach the caller that crashes"), not merely that a diff exists at the right file/line. A remediation
review that confirms the edit rather than the fix is the assert-vs-telemetry trap from
`patterns/e2e-close-conditions`: "a signal you only log is a signal you will miss," applied to review
instead of to test assertions — confirming a defect line changed is telemetry; confirming the defect
condition no longer reproduces is the gate.

(P2 and below may close on orchestrator-verified fix without a mandatory re-panel — that remains a
judgment call, scaled to how consequential the finding is.)

**Why a fixer reviewing its own fix isn't enough:** `art-fce1767c8a9e6c62` (design-panelling pattern)
names the mechanism this protects — independent seats catch what one slipped on *because* they walk
different reasoning paths; a reviewer re-checking the artifact it just produced walks the same path
it was already on, so the error-cancellation the panel exists for doesn't fire. A remediation is the
one moment in the pipeline where the fixer and the "reviewer" are most likely to be the same framing
twice — which is exactly when the decorrelated pass matters most, not least.

**Motivating instance (2026-07-01, project-j-ingest bounded-graph-ingest feature):** a Stage-5 gate
review found a P1/P2 pair (an invalid cutoff crashing the daemon; a filtered-delta page-size gap that
left the feature's own safety cap practically unreachable). Both were remediated and the orchestrator
verified the fix by reading the diff and running the suite — which would have closed both findings.
Instead they were re-dispatched to the panel: two of three seats confirmed the fixes cleanly, but the
third caught a **new, real issue the fix itself introduced** (a rationale comment that had gone stale
because the remediation changed the underlying mechanism it was describing) — a defect that
orchestrator self-verification had already missed once. The re-panel is what caught it.

### Cold-opus dispatch mechanism

**The bridge isn't involved.** The bridge exists for external CLIs (codex, agy) that Claude Code can't invoke directly — they need an out-of-band coordination mechanism (Redis). Opus is the orchestrator itself, so a cold-opus reviewer is dispatched via Claude Code's native `Agent` tool. No bridge engine, no manual session-switching, no separate worktree.

Canonical invocation:

```
Agent(
    subagent_type="general-purpose",
    description="Cold-opus feature review",
    prompt="""You have NO context on this phase. Read only:
      1. docs/superpowers/specs/<topic>.md  (the spec; the authoritative contract)
      2. git diff main…feat/<topic>           (what actually shipped)
    Score against the spec only. Output verdict (APPROVE / APPROVE WITH NOTES /
    REQUEST CHANGES) + findings by severity + spec-coverage table. Commit the
    report to docs/superpowers/reviews/<YYYY-MM-DD>-coldopus-<topic>-feature-review-report.md
    inside the feat/<topic> worktree."""
)
```

The cold-opus property comes from **sub-agent context isolation**: a sub-agent doesn't see the parent's conversation history, only what the orchestrator passes in the prompt. An Opus sub-agent invoked with a brief that contains only the spec + the diff is genuinely operating without memory of how the implementation got built — that's the "fresh context, judges only against spec" rigor without any infrastructure cost.

**The only failure mode is brief contamination.** If the orchestrator's brief leaks implementation context (rationale, deviations log, "we ended up doing X because Y", per-task review reports, the planning conversation), the sub-agent sees those hints and isn't fully cold. The cold-opus brief must contain literally only:

- Path to the spec
- The diff (or instruction to compute it)
- Output format requirements (verdict + findings + spec-coverage table)
- Where to commit the report

**Nothing about the plan, per-task reviews, deviations, or the conversation that produced the implementation.** The brief is the only contamination vector, so brief-cleanliness is the only discipline gate.

**Higher-rigor escape hatch (rarely needed):** open a fresh Claude Code session in a separate worktree with truly empty conversation history (`ls ~/.claude/conversation/` shows only the current session's file). This guards against the edge case where the sub-agent's system-prompt context somehow leaks through harness-level details. In practice the brief-cleanliness discipline is sufficient and this fallback is overkill.

### Workflow B failure shapes

In addition to the Workflow A failures (see § "Common failure shapes" below), B adds:

| Symptom | Cause | Fix |
|---|---|---|
| Task branches merge out of order; feature branch history confusing | Tasks dispatched out of plan order, or remediation overlaps with next-task dispatch | Strict serial: don't dispatch task N+1 until task N is merged. The parallel-reviewer step is internal to a task, not across tasks. |
| Cold-opus sub-agent isn't actually cold — its review reads like it knew the implementation history | Brief contamination — orchestrator's prompt leaked implementation context (rationale, deviations log, per-task reviews, "we ended up doing X") | Audit the cold-opus brief: should contain ONLY the spec path, the diff, output format requirements, and where to commit. Strip anything else. The sub-agent's context isolation is harness-enforced; the only way to leak in implementation hints is via the brief itself |
| Re-review loop never terminates | Remediation rounds exceed planning quality | Hard-stop at 3 rounds. Escalate to human — re-brainstorm the task instead of grinding |
| All three feature reviewers APPROVE with zero findings | Briefs aren't asking the right questions | If this happens twice in a row, tighten the "specific things to check hard" sections in the next feature's briefs. A perfect triple-APPROVE is more often a brief-quality signal than an implementation-quality signal |
| Cold-opus and agy-print disagree on whether to merge | One reviewer caught something the other missed — the dual-perspective design working as intended | The lower verdict wins (REQUEST CHANGES dominates APPROVE). Remediate; the disagreement itself is data — note it in the phase summary for next time |

### Workflow B adoption deltas (vs A)

Beyond the Workflow A adoption checklist:

- [ ] Confirm with user at project kickoff: A or B (or "mixed — B for these phases, A for the rest")
- [ ] If B: confirm the cold-opus dispatch mechanism is operational (option 1, 2, or 3 from above)
- [ ] Add a `phase-workflow.md` to project docs declaring which phases use which workflow, so a fresh Claude session reading the project knows what cadence to expect

---

## The agent bridge (one-time setup)

The pipeline depends on this bridge — a Redis-mediated request/reply protocol that turns a local `codex` or `agy` CLI into a peer addressable from Claude Code via shell scripts. Install + protocol detail in [`../src/agent_redis_bridge/README.md`](../src/agent_redis_bridge/README.md); envelope/protocol shape in [`../SPEC.md`](../SPEC.md). Available engines:

| Engine                    | Wraps                  | Surface         | Status                                   |
|---------------------------|------------------------|-----------------|------------------------------------------|
| `--engine codex`          | OpenAI Codex CLI       | full ACP        | Primary implementer                      |
| `--engine devin-acp`      | `devin acp`            | full ACP        | Implementer + adjunct reviewer (promoted 2026-07-18: panel-approved adapter `a1c2743`, rostered per ARB Memory `art-81ef40a78683d2e9` v2) |
| `--engine agy-print`      | `agy --print`          | one-shot stdin/stdout | Primary reviewer (commit `fea6172`, fix `687c1e5`) |

> **ℹ️ gemini-acp is dead — hard-deprecated, not "retiring"**
>
> Google killed the `gemini-cli`. As of 2026-07-03 `agent-dispatch` / `agent-bridge-ping` **reject `--engine gemini-acp` outright**, regardless of whether `gemini-cli` happens to still be installed locally — there is no fallback path to it anymore. The bridge's reviewer engine is `agy-print`, which wraps `agy --print <task> --print-timeout <s> --add-dir <cwd>` (Google's [Antigravity](https://antigravity.google) CLI) and returns the agy stdout as the turn result.
>
> Known trade-offs of `agy-print` (one-shot, not streaming):
> - **No streaming token deltas** — the report arrives whole at end-of-turn
> - **No mid-turn `steer`** — `engine.steer()` raises `EngineError`
> - **No per-tool events** — only `turn_started` / `turn_completed` / `turn_timeout`
> - **No model selection** — agy 1.0.0 rejects `--model` at the top level, so the engine uses whatever default agy is configured for
>
> These limitations don't matter for review-style dispatch (one prompt in, one report out). For streaming-implementer roles, use codex instead.
>
> Use `agy-print` for all reviewer dispatches. Where more review perspectives are wanted, add pi-GLM rather than reaching for gemini-acp — canonical quorum is codex-contributor + cold-Opus + agy-print + pi-GLM.
>
> The bridge will not get a `--engine claude` for cold-opus — Claude Code itself doesn't speak ACP and Anthropic [closed that as not planned](https://github.com/anthropics/claude-code/issues/6686). Cold-opus dispatches as a native sub-agent (see § "Cold-opus dispatch mechanism") rather than over the bridge. The carve-out is `--engine agent-sdk` with `--target-id asdk-<project>-<ws>-<model>`: that is the Claude-over-bridge path for non-Claude-Code orchestrators such as pi, which have no native subagent tool.

### Quick setup checklist (per machine, per project)

1. **Clone the bridge:** `git clone https://github.com/markgerrard/ARB ~/AgentRedisBridge`
2. **Install:** follow `README.md` — uv venv + script install
3. **Provision a Redis/Valkey bus** (managed is fine; the bridge supports TLS + ACL auth). Localhost works for single-machine setups; managed bus enables cross-host dispatch
4. **Per-project env file:** `~/AgentRedisBridge/envs/<project>-<workspace>.env` with at minimum:
   ```bash
   AGENT_PROJECT=<short-project-name>
   AGENT_WORKSPACE=<dev|staging|prod>
   AGENT_REDIS_HOST=<bus-host>
   AGENT_REDIS_PORT=<port>
   AGENT_REDIS_TLS=1            # if managed
   AGENT_REDIS_USER=default     # if ACL enabled
   AGENT_REDIS_PASSWORD=<secret>
   AGENT_TRUSTED_SENDERS=claude-<project>-<workspace>=trusted,human-<your-id>=human
   ```
5. **Launchd plists (macOS)** at `~/Library/LaunchAgents/com.<user>.codex-bridge.<project>-<workspace>.plist` and `com.<user>.agy-bridge.<project>-<workspace>.plist` — auto-start on login. Mirror the patterns in the bridge repo's `install/` directory
6. **Verify both bridges are alive:**
   ```bash
   AGENT_ENV_FILE=~/AgentRedisBridge/envs/<project>-<workspace>.env \
   AGENT_PROJECT=<project> \
   ~/AgentRedisBridge/scripts/agent-bridge-ping --engine codex <workspace>
   # Expect: heartbeat=alive ttl=<positive seconds>
   ```
7. **Add a project-level Claude memory entry** documenting the env file path, agent IDs, and bus host. Pattern: `reference_agent_redis_bridge.md` in your `~/.claude/projects/.../memory/`

### Recovering a down bridge

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<user>.codex-bridge.<project>-<workspace>.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<user>.agy-bridge.<project>-<workspace>.plist
# Wait ~5s, then re-ping until heartbeat=alive
```

If the plists are missing entirely, re-install from `~/AgentRedisBridge/install/`.

---

## Worktree-before-dispatch (the rule that prevents the most common footgun)

The bridge daemon's `AGENT_WORKDIR` typically points at the same git checkout the parent Claude session is in. **If codex/agy commit work on a feature branch and the parent Claude session is operating on `main` in the same checkout simultaneously, Claude's commits interleave into the feature branch's history** — because `git` doesn't know which writer "owns" which branch. The bug surfaces hours later when the feature-branch log shows mysterious docs commits mixed into the implementation.

### The recipe

Pre-create a worktree BEFORE the first dispatch:

```bash
git worktree add -b feat/<topic> .claude/worktrees/<topic> main
```

Then pass the worktree's absolute path inside the dispatch task body:

```bash
agent-dispatch --run-id "impl-$(date -u +%Y%m%dT%H%M%SZ)" ... "Worktree: /absolute/path/to/.claude/worktrees/<topic>. Read brief at <brief-path> and execute it."
```

The dispatched agent reads the path from prose and `cd`s in as its first action. From that point, all the agent's commits land on `feat/<topic>` cleanly; Claude's parallel work on main doesn't race them.

### What gets committed where

| Surface | Lives on `main` (committed BEFORE worktree creation) | Lives on `feat/<topic>` (committed by the agents in the worktree) |
|---|---|---|
| Spec (`docs/superpowers/specs/...`) | ✓ | – |
| Plan (`docs/superpowers/plans/...`) | ✓ | – |
| Codex implementation brief | ✓ (so it's visible in the worktree history) | – |
| Agy-print review briefs (one per task) | ✓ | – |
| Codex implementation commits | – | ✓ |
| Agy-print review reports | – | ✓ (committed by agy-print inside the worktree so they ship with the merge) |
| Cleanup commits to backlog | – | ✓ (typically in the last codex task) |

**Critical detail:** commit the spec, plan, AND impl brief to main BEFORE running `git worktree add`. The worktree pins to a specific commit; if you commit the brief AFTER worktree creation, the brief file won't be visible in the worktree's filesystem. You can still dispatch using absolute paths (which is what makes "brief on main, worktree on commit-N-with-no-brief" work) but readability suffers and merging can get fiddly.

### After merge

```bash
git checkout main
git merge --no-ff feat/<topic>
git push origin main
git worktree remove .claude/worktrees/<topic>
git branch -d feat/<topic>
# Remote branch usually never existed (codex worked locally) — the
# "remote ref does not exist" error from git push --delete is benign
```

`--no-ff` preserves the branch boundary so the dual-review trail (per-task codex commits + agy-print report commits) stays visible in `git log --graph`. A fast-forward would collapse it.

---

## The brief-to-file pattern

For any dispatch beyond one sentence, **write the task body to a markdown file first** and pass `"Read the brief at <path> and execute it"` as the dispatch task.

Why this beats passing a long task string:

- Avoids shell-quoting pitfalls (bash double-quotes don't interpret `\n` — the literal escape lands in the task body)
- Creates a durable artifact for human review
- Lets the brief evolve in git if you need to re-dispatch with corrections
- The agent's reply is shorter, so the harness's completion notification doesn't bloat context

If you do pass a task string inline and want to be sure the bytes are right, `agent-dispatch
--dry-run-envelope "<task>"` prints the exact JSON envelope it *would* LPUSH (without sending),
so you can see a literal `\n` or a wrong recipient/payload before the round-trip rather than
diagnosing a mangled task body from the agent's reply afterward.

### Brief file conventions

| Brief type | Path |
|---|---|
| Codex implementation (shared brief for all N tasks) | `docs/superpowers/reviews/YYYY-MM-DD-codex-<topic>-impl-brief.md` |
| Agy-print review (one per task) | `docs/superpowers/reviews/YYYY-MM-DD-agy-<topic>-task<N>-review-brief.md` |
| Agy-print report (written BY agy-print, INTO the worktree) | `docs/superpowers/reviews/YYYY-MM-DD-agy-<topic>-task<N>-review-report.md` |

### Codex implementation brief — what to include

- **Worktree path** (CRITICAL — first line of the dispatch instructions inside the brief; "you MUST cd here, not the shared checkout")
- **Branch name** (so the agent confirms `git branch --show-current` before any work)
- **Authoritative source documents** (spec, plan, `CLAUDE.md` / project conventions file)
- **Per-task dispatch model** — explain that subsequent dispatches will say `"Execute Task N from <plan path>"` and what the agent should do for each task (read task body, follow steps in order, run tests at TDD checkpoints, pint + commit, reply with SHA + test results + deviations)
- **Project-specific conventions the agent MUST follow** (TDD shape, formatting rules, generator commands, "no migration rollback" guards, etc.)
- **Known unknowns** the agent must resolve themselves (e.g., placeholders in the plan that depend on undocumented codebase state)
- **First task to execute**

### Agy-print review brief — what to include

- **Worktree path** + commit SHA being reviewed
- **What this commit does** (1-2 sentence summary)
- **Codex's reported deviations** (from the implementation reply) — agy-print's job is to validate each one
- **Specific things to check hard** — numbered checklist of acceptance criteria from the spec (verbatim where possible)
- **What NOT to flag** (style nits, deviations the implementer already disclosed and that you've already triaged as acceptable)
- **Output format** — verdict (APPROVE | APPROVE WITH NOTES | REQUEST CHANGES), summary, findings by severity, spec-coverage table. Audit stances are the canonical `abstain|approve|block|needs-changes|timed-out`; map prose verdicts per `docs/fragments/vote-fence.md` before emitting.
- **Report file path** and commit-message instruction so agy-print commits the report inside the worktree

The brief is the contract. A thin brief produces a thin review; a precise brief produces a precise review.

---

## Dispatch recipe (the canonical invocation)

Canonical generic recipe: `docs/fragments/dispatch-recipe.md`; this inline form keeps
pipeline-specific worktree and per-task reply-contract annotations.

```bash
FROM_AGENT_ID=claude-<project>-<workspace> \
BRANCH=main \
AGENT_ENV_FILE=~/AgentRedisBridge/envs/<project>-<workspace>.env \
~/AgentRedisBridge/scripts/agent-dispatch \
  --engine codex \
  --target-id codex-<project>-<workspace> \
  --timeout 5400 \
  --run-id "impl-task-N-$(date -u +%Y%m%dT%H%M%SZ)" \
  "Worktree: /absolute/path/to/worktree. Read brief at /absolute/path/to/brief.md and execute Task N from <plan path>. Reply with commit SHA + test pass count + any deviations." \
  > /tmp/<task>.out 2> /tmp/<task>.err
```

**Run via `Bash(run_in_background=true)`** — the dispatcher's BLPOP loop is the wait mechanism; the harness's task-completion notification fires when the agent replies. Don't wrap in `(... &) sleep N && cat`. Don't poll.

For the reviewer dispatch, use `agy-print` (gemini-acp is dead — the CLI rejects `--engine gemini-acp` outright, so it is not a fallback option regardless of what's installed locally):

```bash
--engine agy-print
--target-id agy-<project>-<workspace>
--timeout 3600                                     # review is usually faster than implementation
--run-id "$RID"
```

Agy-print's reduced surface (no streaming, no steer, no per-tool events) is only visible inside the bridge engine — from the orchestrator's perspective the dispatch + reply shape is the same as any other reviewer engine.

### Why each override matters

| Override | Why |
|---|---|
| `FROM_AGENT_ID` | Bridge's `--sender-policy` only trusts specific agent IDs; helper script's default may be wrong |
| `BRANCH` | Empty branch → `envelope-invalid invalid-branch`. Hardcode |
| `AGENT_ENV_FILE` | Helper's default may point at a legacy path |
| `--target-id` | Helper default may use a legacy project name |
| `--timeout` | Default 1800 (30 min) is too short for substantial work; bump to 5400 for impl, 3600 for review |

**Preflight instead of remembering all five:** `agent-dispatch --check` validates the
*resolved* config — env file readable, bus host/port resolved, branch non-empty,
`FROM_AGENT_ID` in the target's `AGENT_TRUSTED_SENDERS`, and the target registered with a
live heartbeat — and refuses with a specific message *before* the LPUSH, instead of
surfacing as `sender-rejected` / `envelope-invalid invalid-branch` / silent-wrong-bus
after the round-trip. Run it once before a batch of dispatches (or whenever a dispatch
mysteriously times out) to catch a forgotten override at the source. (The trusted-sender
check reads the env-file policy — authoritative when the target shares that env file, which
is the single-host case; advisory cross-host until the daemon publishes its policy to the
registry.)

See `~/AgentRedisBridge/docs/orchestrator-patterns.md` and the `using-agent-bridge` skill for protocol-level detail.

---

## The per-task loop

After Task N codex commit, before Task N+1 dispatch:

1. **Read the codex reply** from `/tmp/<task>.out` — capture commit SHA, test counts, deviations
2. **Verify the commit** lives in the worktree: `git -C <worktree-path> show --stat <SHA>` to confirm files match the plan
3. **Write the agy-print review brief** — include codex's deviations as explicit things for agy-print to validate
4. **Commit the agy-print brief to main** (audit trail)
5. **Dispatch agy-print** with the brief path + worktree path
6. **Read agy-print's verdict** when notification fires
7. **Triage:**
   - **APPROVE** → next task dispatch
   - **APPROVE WITH NOTES** → read notes; if minor (style, observations), proceed; if substantive, fold into the next task's brief
   - **REQUEST CHANGES** → dispatch a focused fix to codex with agy-print's exact diagnosis; **do not skip this step**, even if the fix is small

For REQUEST CHANGES, the focused-fix dispatch task body should:
- Reference the agy-print report commit SHA
- Quote agy-print's exact prescription (file:line + recipe)
- Ask codex to also add the missing test coverage that allowed the defect to slip through

After the fix, decide whether to re-dispatch agy-print for second-round review:
- **Re-dispatch** if the fix involved engineering judgment (new pattern, architectural change)
- **Skip re-dispatch** if the fix is a mechanical mirror of agy-print's exact prescription AND a new test asserts the fix
- Either way: verify locally before merging

---

## When dual-review proves its worth

Pattern observed across multiple feature dispatches: the first 2-3 tasks tend to come back **APPROVE with notes only** (positive observations, no findings). The final task often surfaces a **REQUEST CHANGES** with a real blocker. This is the dual-review investment paying off — the small remaining gap (a missed controller, a stale cache key, an unwired surface) lands precisely at the boundary between "what the plan said" and "the full operational reach of the system".

If 4 reviews in a row all APPROVE with zero findings, that's a signal the briefs aren't asking the right questions — not that the implementation is perfect. Tighten the "specific things to check hard" list for the next feature.

Conversely, if every review surfaces a blocker, the implementer (codex) is being asked to make architectural decisions that should have been made during brainstorming. Push more decisions back to the brainstorming stage.

The healthy distribution:
- 2-3 reviews APPROVE with notes (validation that the planned architecture lands cleanly)
- 1 review REQUEST CHANGES on the final-task boundary (catches the inevitable unwired edge)
- 0 reviews REQUEST CHANGES on Task 1-2 (would indicate the spec didn't decompose cleanly)

---

## Common failure shapes and fixes

<!-- fragment:failure-shapes begin -->
| Error / Symptom | Likely cause | Fix |
|---|---|---|
| `[bridge-error] sender-rejected ...` | `FROM_AGENT_ID` is not in the target bridge's trusted-sender list | Set `FROM_AGENT_ID` to a value the bridge trusts, or have the operator add your ID |
| `envelope-invalid invalid-branch` | `BRANCH` is empty or `git branch --show-current` returned `""` | Hardcode `BRANCH=dev` or the intended branch in the dispatch invocation |
| `bridge busy with task <uuid>` | All engine-pool slots on the target are occupied | Wait, cancel, or check whether `BRIDGE_MAX_PARALLEL` is set lower than needed |
| Bridge starts but rejects every dispatch | The target bridge may have no sender policies configured | Set `AGENT_TRUSTED_SENDERS` in the env file or pass `--sender-policy` on the CLI |
| Dispatch exits immediately: "pass --run-id ID ... or --adhoc" | agent-dispatch hard-refuses un-labelled dispatches (since 2026-07-01) | Mint a run-id (dispatch-dev auto-defaults one) or pass --adhoc for a throwaway |
| `agent-dispatch` exits 124 | Timeout reached before a matching reply landed | Increase `--timeout` if the task is still running, or inspect bridge logs for a crash |
| Commit body shows literal `\n` characters | Caller composed the body with Bash double quotes | Use `$'...'`, a heredoc, or the brief-to-file pattern |
| `LLEN inbox` reads 0 while a task is running | Normal BLPOP behavior; the bridge consumes atomically | Use task status/result keys or bridge logs, not inbox length |
| `NOPERM No permissions to access a key` on a foreign inbox `LLEN`/`KEYS`/`SCAN` | You are on the **self-hosted bus** (per-identity ACLs); browse + foreign read-back are denied by design | Not a bug. Use the recipient's reply as the consumption signal, `GET`/`TTL` on a known `:status` for presence. See `docs/self-hosted-bus.md` |
| Panel `refused_reconcile` naming a seat whose vote you never saw fail | On the self-hosted bus, a missing **audit-emitter** grant NOPERMs the emit in the seat daemon log, not your cockpit | Grep that seat's daemon log for NOPERM at vote time; check its `ARB_MEMORY_REDIS_URL` user; recover via new run-id + `supersedes:` (`docs/self-hosted-bus.md`) |
| Bridge log shows `[reply-sent]` but dispatcher does not exit | Caller inbox may be polluted with stale `kind=notify` envelopes | Pull bridge code to a dispatcher that drops notifies and set `BRIDGE_NOTIFY_INBOX=0` |
| Dispatch to a Claude seat fails as unknown engine | A raw model id (e.g. `claude-opus-4-...`) was passed as `--engine` — engines are harness names, not model ids | Use `--engine agent-sdk` with `--target-id asdk-<project>-<workspace>-<model>` |
| `Could not connect to Redis ...: Can't assign requested address` mid-run, after the task-id printed | Ephemeral-port exhaustion. Each `agent-dispatch` spawns a fresh `redis-cli` per BLPOP poll; a wide fan-out held open for tens of minutes exhausts local ports | Stagger to 2–3 concurrent dispatchers. **The reply is lost irrecoverably** — the task ran, but its result key is gone before you can read it, so never fan out a benchmark un-staggered |
| Reply gate returns `dirty_uncommitted` listing files the task never touched | The orchestrator edited the seat's workdir while the dispatch was in flight; the gate diffs against the state at task START | Never edit a seat's workdir mid-dispatch. Note this is **silent for tasks that start after the edit** — they baseline the dirt and report `no_changes_clean`, so one contaminating edit fails only the runs already in flight |
| Later dispatches in a queued fan-out exit 124 while earlier ones succeed | Seats are `--max-parallel 1`; queued dispatchers spend their client `--timeout` waiting their turn, not working | Set `--timeout` to at least `queue_depth × turn_timeout`; keep `--turn-timeout` at the review ceiling |
| Seat dies at startup with `ValueError: invalid sender policy: <id>:trusted` | `--sender-policy` pairs are separated by `=`, not `:` | Pass `<id>=trusted`; valid values are `trusted\|human\|reject` (`Bridge.parse_sender_policies`) |
| `ModuleNotFoundError: No module named 'redis'` from `dispatch_authority`, and the seat log shows NOTHING | `agent-dispatch` resolved a system python without the venv, so it died before enqueueing — the seat looks deaf but never received anything | Put `$PWD/.venv/bin` on `PATH` for the dispatch. Note the asymmetry: `arb-memory-harness-publish` needs `ARB_MEMORY_REDIS_URL` **sourced**, the dispatch step needs it **unset** (`env -u`) |
| `arb-memory-harness-publish` → `invalid brief: missing ## Assumptions section`, or `items[N] must be an object` | The brief has no assumptions block, or its items are strings. `scripts/review-brief` does not emit the section at all | Add `## Assumptions` with a JSON fence whose `items` are objects: `{"statement","status":"assumed"\|"demonstrated","vantage"}`; `demonstrated` also needs `artefact_id` + positive int `version` matching the target's vantage (`tools/faba/faba_schema.py::validate_dispatch_brief`) |
| Verdict close returns `refused_reconcile` with `expected exactly 1 dispatch manifest, found 2; run un-auditable` | The roster manifest was emitted twice under one run-id (e.g. re-emitted after seats were replaced). Two rosters means no single answer to "who was on this panel", so no verdict can be proven complete | Emit the manifest **last**, once seat ids are final. To recover: mint a NEW run-id, emit exactly one manifest, re-emit every seat's vote from its **verbatim** fence, close with `supersedes: <refused-run-id>`. The refused run stays in Postgres as the scar — intended |
<!-- fragment:failure-shapes end -->

Pipeline-specific failures still apply around the shared bridge failures: a brief committed after `git worktree add` may not be visible in the worktree; no worktree can interleave commits into the wrong branch; old bridge builds before `9da7761` can throttle prose dispatches on managed buses; and a `killed` foreground wait should be checked with `ctl status` / `ctl result` before redispatching.

**`killed` vs a genuinely wedged daemon — don't conflate the two.** A `killed` notification is not
itself evidence the bridge is stuck; it's evidence the orchestrator's *own* wait ended early. Check
the bridge's journal for the task-id: if there's a `[turn-start]` with no matching `[turn-end]` for a
long time (minutes, not seconds) on a still-running child process, that's a genuine wedge — restart
the daemon (§ Recovering a down bridge). If the journal shows normal `[turn-tool]` progress or a
`[turn-end] ... ok`, the work finished or is progressing fine — use `ctl status`/`ctl result` to
retrieve it, don't fire a duplicate dispatch. Redispatching reflexively on every `killed` status
stacks duplicate requests in the same inbox and wastes the time you were trying to save.

---

## Adoption checklist (new project)

When dropping this pipeline into a new project:

- [ ] **At kickoff, Claude asks: "Workflow A (lightweight) or Workflow B (rigorous) for this project?"** See § "Workflow selection — A or B". For mixed (per-phase), record which phases use which in `docs/phase-workflow.md`.
- [ ] Bridge installed + env file at `~/AgentRedisBridge/envs/<project>-<workspace>.env`
- [ ] Launchd plists created for both codex and agy bridges
- [ ] Bridges register alive on `agent-bridge-ping`
- [ ] Project `CLAUDE.md` includes formatting / TDD / generator commands the implementer must follow
- [ ] `docs/superpowers/{specs,plans,reviews}/` directories exist
- [ ] `.claude/worktrees/` is in `.gitignore` (worktrees should NOT be committed)
- [ ] Project memory has an entry mapping bridge env file paths, agent IDs, bus host
- [ ] **If using Workflow B**, confirm orchestrator is Claude Code with the `Agent` tool available (cold-opus dispatches as a native sub-agent — no bridge engine needed). Brief-cleanliness discipline understood: cold-opus brief contains ONLY spec path + diff + output format + report path. See § "Cold-opus dispatch mechanism"
- [ ] First feature dispatched end-to-end (brainstorm → spec → plan → codex → agy-print → merge, plus full-feature review if Workflow B) to validate the loop before relying on it for higher-stakes work

---

## Proposed — pending owner co-sign: changed-test mutation gate

Before merging a candidate that changes test files, the orchestrator runs the gate from a clean
checkout. This subsection is proposed and binds only after Mark's co-sign.

```bash
BASE=dev
SUMMARY_DIR="${SCRATCH:-/tmp/mutation-gate}"
mkdir -p "$SUMMARY_DIR"
expected="$(git show "${BASE}:scripts/changed_test_mutation_gate.py.sha256" | awk 'NF {pin=$NF} END {print pin}')"
actual="$(shasum -a 256 scripts/changed_test_mutation_gate.py | awk '{print $1}')"
[ -n "$expected" ] && [ "$expected" = "$actual" ] || exit 3
.venv/bin/python scripts/changed_test_mutation_gate.py --base "$BASE" --json "$SUMMARY_DIR/mutation-gate-summary.json"
```

The gate-machinery landing exception verifies the candidate's reviewed digest because the base
cannot yet contain its pin. Allowlist entries are owner-landed in
`scripts/mutation_gate_exemptions.json` and read from the base ref, never from a candidate.
Export `ARB_MEMORY_DSN` when gating `tests/arb_memory/` pairs; the gate does not provision it.
Workers changing tests may run the same gate with `--base <branch point>`; a CoW worktree may
advisory-refuse on import resolution, while the merge-time run remains authoritative.

If a `FAIL[tree]` or hard kill leaves residue, enumerate it and recover before any merge:

```bash
git -C <repo> status --porcelain
git -C <repo> checkout -- <paths>
git -C <repo> clean -n -- <paths>
git -C <repo> clean -f -- <paths>
git -C <repo> status --porcelain
```

The final status must print nothing. Never commit, stash, or merge from that state; rerun the
gate after confirming clean.

## See also

- [`../src/agent_redis_bridge/README.md`](../src/agent_redis_bridge/README.md) — installation, env-file shape, systemd units
- [`../skills/README.md`](../skills/README.md) — Claude Code skill setup, and which instruction file each layer reads
- [`../SPEC.md`](../SPEC.md) — protocol envelope format and Redis key naming (historical; see banner)
- [`./orchestrator-patterns.md`](./orchestrator-patterns.md) — bridge-internal orchestration quick reference plus patterns for parallel dispatch (A), zero-poll monitoring (B), dual-review (C), recurring-gotcha briefing (D), and cross-host orchestration (E)
- [`./bridge-parallelism.md`](./bridge-parallelism.md) — engine-pool design and `--max-parallel` flag
- [`./claude-peer-coordination.md`](./claude-peer-coordination.md) — Claude↔Claude peer coordination without an engine (relevant if cold-opus dispatches via the bridge's peer-coordination path)
- Claude Code skill: `using-agent-bridge` — operational dispatch recipe (covers protocol mechanics; this manual covers the *workflow* using those mechanics), incl. § "Per-stage authoring rotation for design / spec / plan" (who authors each stage + the author-non-quorum quorum-swap)
- [`./prompting-claude-fable-5.md`](./prompting-claude-fable-5.md) — Anthropic's Fable prompting guide (apply when dispatching a Fable authoring subagent; in-repo so it doesn't depend on ARB Memory)
- Claude Code skills: `superpowers:brainstorming`, `superpowers:writing-plans` — the stages BEFORE codex dispatch
- Claude Code skill: `superpowers:subagent-driven-development` — alternative execution model (in-session subagents instead of cross-process bridge dispatch); use when you don't need cross-host or want a tighter Claude-controlled loop
