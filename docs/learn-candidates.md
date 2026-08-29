# /learn candidate tracker — external technique intake

Working list of externally-sourced candidates for the `/learn` gate. Each row records the
orchestrator's initial triage (with reasoning), the pre-registered prediction (written
BEFORE the eval, never included in the proposal text — anchoring the seats voids the test),
and the gate's verdict. Tick off or exclude with reasoning; this file is the audit trail
that makes gate-calibration claims checkable.

Referent rule: every candidate repo is VERIFIED to exist (gh, date, stars) before proposal —
suggestions arrive from training-knowledge sessions and are claims until checked.

## Category 1 — Orchestration / harness patterns

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| pi-agent-teams: **stall detection** for parallel agents (`tmustier/pi-agent-teams`) | ✅ 2026-07 active | **SELECTED** | Reality-grounded: three live seat wedges on 2026-07-07 alone (ghost daemon made the Sonnet seat deaf; a `bfs` sweep hung a review turn 25 min; slow-silent GLM turns). arb-watch *displays* status; nothing *detects* stalls. The one orchestration gap demonstrated by our own ops, not imagined. |
| pi-agent-teams: file-reservation | ✅ | excluded | Worktree isolation already solves file conflicts structurally — reservation is the configurational version of a problem we solved by construction. |
| microsoft/autogen termination/handoff conditions | ✅ ⭐59k | excluded | Framework patterns; ARB panels have fixed rounds + orchestrator arbitration. Marginal distillate. |
| openai/openai-agents-python handoff primitive | ✅ ⭐27k | excluded | Bridge dispatch is already an explicit handoff primitive with envelopes and completion gates. |

**Pre-registered prediction (stall detection):** genuine coin-flip leaning approve — the gap
is real and demonstrated same-day, but the trio may judge it an ops-polish item below build
capacity, or point at arb-watch as the existing surface to extend rather than a new mechanism.

- [x] Proposed → `learn-seat-stall-detection-for-dispatched-turn-fbf3c4b3`
- [x] Gate verdict: **needs-mark** — codex approve (detect-only posture right, false alarms
      cheap); agy flags a real design gap (continuous turn heartbeats mean a naive
      no-event timeout won't fire for wedged TOOLS — the signal must be tool-event gap, not
      heartbeat gap); GLM: strongest evidence in the batch, worth building as the
      unattended-run notifier with a generous timeout. AWAITING MARK: resolve approve
      (build detect-only, tool-event-gap signal) or reject.

## Category 2 — Verification / eval / LLM-judge (highest-value category: the panel IS the moat)

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| **Judge-calibration methodology** for ARB panels (distilled from promptfoo ⭐23k / deepeval ⭐16k judge patterns + published LLM-as-judge bias findings: position bias, self-preference, verbosity bias) | ✅ both active 2026-07 | **SELECTED** | Directly maps to an observed fact: our seats have measurably different severity temperaments (pi seats soft-label; Sonnet vs Opus split on identical pages). No mechanism measures panel-seat bias or scores reviewers on misses. The review-stage bake-off idea exists but is unbuilt. Reality-grounded in the system's actual moat. |
| OpenAI evals / Ragas as frameworks | ✅ | excluded | Frameworks, not techniques — ARB's panel machinery already exists; the distillable part is the calibration methodology, captured in the selected row. |

**Pre-registered prediction (judge calibration):** most-likely-approve (or needs-mark on
scope) — this is the reality-grounded one, analogous to the backfill approval: grounded in
our own observed seat-temperament differences rather than external plausibility.

- [x] Proposed → `learn-panel-judge-calibration-measure-seat-bia-606a6002`
- [x] Gate verdict: **needs-mark** — the hedge branch: goal endorsed (panels ARE an
      LLM-judge system with documented bias classes), but the mechanism doesn't transpose:
      promptfoo/deepeval calibrate a STABLE single judge while ARB's fleet drifts across
      model versions, so a periodic replay calibration is stale-on-arrival. Seats propose a
      redirect: extend the EXISTING live calibration loop, lightweight and model-versioned.
      RESOLVED (Mark, 2026-07-07): **approve AS REDIRECTED** — extend the existing live
      calibration loop, model-version-aware and lightweight; explicitly NOT a replay corpus
      or a new subsystem. Promoted; build brief emitted; queued for a future pipeline run.

## Category 3 — Memory / retrieval (re-tests the Hermes candidate-B rejection)

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| **mem0 decay/consolidation mechanism** (`mem0ai/mem0` ⭐60k) | ✅ active 2026-07 | **SELECTED** | Deliberate re-test: the gate rejected the Hermes lifecycle-curator as premature (no read telemetry, YAGNI at 166 artefacts). Feeding a concrete, popular implementation of the same idea checks whether the gate's rejection was reasoning (should hold) or novelty-aversion (would flip). |
| Letta/MemGPT self-editing memory | ✅ ⭐23k | excluded | Self-editing memory is the autonomous-writer pattern the gate already rejected (post-run reflection) — re-proposing a rejected idea's sibling without new evidence wastes an eval. |
| Zep | unverified | excluded | Not verified this pass; redundant category coverage via mem0. |

**Pre-registered prediction (mem0 decay):** reject as redundant-with-ARB-Memory + premature
(consistency with the candidate-B rejection) — UNLESS the seats find mem0's concrete decay
mechanism answers the telemetry gap, in which case a needs-mark reopening B is legitimate
discrimination, not drift.

- [x] Proposed → `learn-memory-decay-consolidation-as-implemente-97c646ec`
- [x] Gate verdict: **rejected** — sharper than the original B rejection: decay is
      REDUNDANT for the hint layer (artefact_index retirement already soft-deletes,
      "verified this session") and HARMFUL for artefacts (durable evidence); consolidation
      would destroy the verbatim-provenance property that is the store's reason for
      existing. Consistency with B held, with better grounds.

## Category 4 — Prompt-injection / security (the drift detector: re-tests candidate-C logic)

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| **garak's scanning approach** (`NVIDIA/garak` ⭐8k, the LLM vulnerability scanner) | ✅ active 2026-07 | **SELECTED** | Deliberate consistency probe: the gate rejected injection-*screening* on threat-model-applicability grounds (mistakes-not-malice) while its dissent produced the structural fix we shipped (withhold, don't scan). Feeding a concrete scanner tests whether that reasoning holds across sessions. An approval here would contradict the gate's own week-old logic — drift, worth catching. |
| Azure/PyRIT | ⚠️ stale (pushed 2026-03, ⭐77 — likely mirror) | excluded | Failed the referent-freshness check; garak covers the category. |
| promptmap | unverified | excluded | Not verified this pass. |

**Pre-registered prediction (garak):** reject on threat-model-applicability grounds — the
consistency test. Approval = drift alarm; rejection on complacent "we're internal" grounds =
worth a second look; rejection engaging the threat model = the gate is stable across sessions.

- [x] Proposed → `learn-adversarial-llm-vulnerability-scanning-o-90045b2a`
- [x] Gate verdict: **rejected — on exactly the sound grounds**: "mis-scoped to ARB's
      threat model; garak probes MODEL-level adversarial robustness for an ADVERSARY input
      source; ARB's documented model is mistakes-not-malice and the only external-text
      ingress was just structurally hardened" + probe-content contamination risk. NOT the
      complacent version. Cross-session consistency with candidate C: CONFIRMED, no drift.

## Scorecard (fill as verdicts land)

| Category | Prediction | Verdict | Match? |
|---|---|---|---|
| Stall detection | coin-flip lean approve | needs-mark (design gap named) | ✳️ coin-flip resolved to escalation |
| Judge calibration | approve (hedge: needs-mark on scope) | needs-mark (mechanism doesn't transpose; redirect proposed) | ✳️ hedge branch hit |
| mem0 decay | reject (redundant/premature) | rejected (redundant + doctrinally harmful) | ✅ |
| garak scanning | reject (threat-model) | rejected on threat-model grounds | ✅ no drift |

**Batch result (2026-07-07, final):** 2 sound rejections matching firm predictions (incl.
the garak drift-detector passing on exact threat-model grounds), 2 needs-mark escalations
resolved by Mark as approve-with-the-gate's-corrections (stall detection detect-only with
tool-event-gap signal; judge calibration redirected into the existing live loop). Both
promoted; build briefs queued. The escalations carried design improvements the gate itself
produced — the highest-value eval behavior. Gate lifetime: 9 evals, 1 direct approval
(reality-grounded backfill), 2 Mark-resolved approvals of gate-corrected mechanisms, 6
rejections; zero drift across sessions.

## Category 5 — Internal feature requests (not externally sourced; same gate, same discipline)

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| **arb-bench: role-scoped red-team benchmark harness for seats** (Mark, 2026-07-09) | n/a — internal request, no repo referent | **SELECTED** | Reality-grounded in same-day ops: cursor CLI updated + grok-4.5 seat stood up 2026-07-08/09, both re-trusted on nothing more than a ping dispatch. Trust ladder rests on anecdote (cursor viability = 1 review; model-alias-1 = 1 commit). No repeatable capability probe exists for seat/model change events. |

**Pre-registered prediction (arb-bench):** needs-mark — the goal is reality-grounded (the
gate historically respects that), but the proposal sits next to the judge-calibration
ruling's "NOT a replay corpus or a new subsystem" boundary, and the staleness argument that
redirected that candidate (fleet drifts continuously; a bench suite must be re-runnable at
every change or is stale-on-arrival) applies with similar force here. Expect at least one
seat to propose a redirect: fold arb-bench into the existing calibration/eval-suite thread
(eval-p3 fixture corpus) rather than a new subsystem, and/or scope-slice to the reviewer
red-team bench first. Outside chance of clean WORTH-BUILDING if seats read the planted-
ground-truth distinction as clearly outside the no-replay-corpus ruling; REJECT unlikely
(the gap is demonstrated by our own ops, the gate's strongest historical approve signal).

- [x] Proposed → `learn-arb-bench-role-scoped-red-team-benchmark-5ad6a61f`
- [x] Gate verdict: **rejected** (round 3; rounds 1–2 were eval-errors from two /learn
      brief under-specifications — stance mapping, then severity vocabulary — both fixed
      red-green, `5045542` + `7b48bc5`). Full three-way split on substance: codex
      WORTH-BUILDING (as constrained extension + evidence-for-human-decisions); agy
      NEEDS-MARK (redirect to Instrument 1, floor-capability only, effective-N); **GLM
      REJECT P1, grounds verified against the docs verbatim**: `eval-suite-design.md` v3
      is a *decision record* — v1 (seeded corpus) and v2 (disagreement corpus) were
      already panel-REJECTED and §0 *proves* every internal catch-corpus blind to the
      never-caught set by construction; the proposal's "reviewer red-team bench for trust
      assignment" IS the foreclosed v1 core renamed; the escaped-defect journal is "a
      journal, not a harness" (fixture use inverts it); the legitimate kernel — a
      floor-capability check on seat/model swap — is ALREADY approved as Instrument 1 §3
      "Build (now)" and needs no /learn promotion. Strict precedence: one substantive
      REJECT rejects.

**Scorecard entry:** prediction needs-mark, "REJECT unlikely" → verdict **rejected**: ❌
headline miss. The redirect I predicted (fold into the eval-suite thread) was real but I
had its polarity backwards — I guessed the reviewer red-team bench was the *first slice*;
it was the *foreclosed core*, and the buildable remainder needed no gate at all. The miss
is instructive: I pattern-matched "reality-grounded ⇒ gate approves" without reading
`eval-suite-design.md` §0 first — the gate had, in effect, already run this eval in June.

## Category 6 — Build-workflow verification (2026-07-20 batch)

| Candidate | Verified | Triage | Reasoning |
|---|---|---|---|
| **Gate-first auto-validation** — validator-authored executable acceptance gate (`gate.py`) written BEFORE the build dispatch; red-baseline proof; builder loops on verbatim FAIL lines; triage at N; one gate self-repair (`disler/fusion-harness` `/auto-validate` loop) | ✅ 2026-07-20 (created 2026-07-16, pushed 2026-07-20, 49★) | **SELECTED** | Repo + 8-hit ARB Memory sweep found no prior art for the ordering (gate-authored-first, red-baseline, machine-FAIL-driven loop); adjacent philosophy only ("completion gate is truth", re-panel-to-zero, Pattern F). Mechanizes the seam where vacuous-done claims slip through: acceptance criteria live in brief prose the builder itself interprets and self-reports. Outside eval-suite v3 §0 foreclosure (per-task workflow verification, no corpus, no seat measurement). |

**Pre-registered prediction (gate-first auto-validation):** NEEDS-MARK — expect at least one
seat to argue the legitimate kernel (executable pre-registered done-criteria) is achievable
today via brief discipline + the existing completion gate without new loop machinery, and to
propose a redirect: fold the validator gate contract into the worker-brief preamble /
completion-gate contract rather than build loop tooling. REJECT is possible on
already-available grounds (the arb-bench precedent: "the buildable remainder needs no gate").
Clean WORTH-BUILDING if seats weigh grader/builder separation + the red-baseline requirement
as genuinely absent from current practice (they are — that's the scan result). Panel note:
run fired from the pier-session Mac with grok substituting for the unavailable glm seat
(no pi-sdk GLM seat on this host); composition deviation recorded here, not hidden.

- [x] Proposed → `learn-gate-first-auto-validation-validator-aut-b9d67335`
- [x] Gate verdict: **WORTH-BUILDING, unanimous** (run
      `learn-gate-first-20260720T175933Z-ea69d5`, audit-closed `emitted`, 2026-07-20;
      first live confirmation of seat-side `--audit-panel` auto-votes). Build conditions
      from findings: **sandbox generated gates** — no secrets, no network, read-only repo
      (codex P1); **per-check red baseline** with delta/invariant classification, an
      aggregate red can hide vacuous checks (codex P1); digest-enforced gate immutability
      (codex P2); behavior-invariant tasks (refactors) need structural checks or a
      red-baseline opt-out in the validator contract (agy P2); opt-in sized to blast
      radius, green gate stays evidence-not-verdict, ship the kernel (role + red baseline
      + orchestrator-run gate) before auto-triage automation (grok P2). Ops note: codex
      round 1 role-played the arb-learn CLI and asked which panel to use instead of
      evaluating — no vote emitted (fail-soft worked); re-fired with an
      already-composed-panel preamble. **PROMOTED 2026-07-20** (v2 eval-approved with
      out-of-band evaluation record, v3 promoted via `arb-learn promote` after the
      arb-prod SSH path was restored — key received sealed via ARB Secrets from
      claude-bridge-dev; approval-rate warning 4/10 noted). Build brief:
      `docs/superpowers/briefs/learn/learn-gate-first-auto-validation-validator-aut-b9d67335-build-brief.md`.
      **BUILT 2026-07-20** (codex-sol, worktree `gate-first-kernel`): kernel =
      `roles/validator.md` + `scripts/run-gate` + `src/agent_redis_bridge/gate_runner.py`
      + `tests/test_gate_runner.py` + Pattern G in `orchestrator-patterns.md`. Review
      loop ran the gate-first pattern on itself: impl (912dd5f) → panel
      `panel-gate-first-impl-…` blocked on 2 hinge-verified P1 (dirt-detector blind to
      gitignored + `.git/hooks` code-exec; `RLIMIT_NPROC=128` per-UID spawn failure on
      macOS) → correction (1adebf1) → refix panel `panel-gate-first-refix-…` both P1
      closed-by-execution, converged P2 on other `.git` internals → correction 2
      (2e00131) closes it (full gitdir fingerprint). Final: 20/20 tests, all findings
      verified closed by orchestrator execution. Worktree at 2e00131, mergeable to dev.

**Scorecard entry:** prediction NEEDS-MARK (redirect into brief/completion-gate) →
verdict **WORTH-BUILDING unanimous**: ❌ headline miss. Grok addressed the predicted
redirect explicitly and rejected it — folding into brief prose loses exactly the two
load-bearing properties (lineage separation, red baseline). I over-weighted the
arb-bench "already-available remainder" precedent; the seats read the ordering itself
as the absent mechanism, which matches the scan result I had in hand.
