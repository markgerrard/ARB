# ARB eval-suite design — panel review synthesis

> Status: **review record** (not committed). Panel review of `docs/eval-suite-design.md` (draft).
> Reviewers: codex (GPT-5.5), agy-print (Gemini), cold-opus (Claude Opus subagent) — independent, unique out-of-repo paths.
> Date: 2026-06-16. Individual reports: `/tmp/eval-review-codex.md`, `/tmp/eval-review-agy.md`, `/tmp/eval-review-coldopus.md`.

## Verdicts — unanimous REJECT

| Reviewer | Verdict | Counts |
|---|---|---|
| codex | REJECT | 1 P0, 4 P1, 1 P2 |
| agy-print | REJECT | 1 P0, 4 P1 |
| cold-opus | REJECT | 1 P0, 6 P1, 3 P2 |

Review was a **measurement-validity** review: would the design produce numbers that mean what they appear to, or
confident-looking artifacts that are confounded? It is not a prose/code review.

## The finding that matters most (all three circle it; cold-opus names it cleanly)

**P0 — the seeded-defect corpus is *circular* for measuring decorrelation, and anti-correlated with the very thing it
is meant to measure.** You can only seed a defect you already know exists, know the class of, and can locate
(`location: "src/api/upload.rs:42"`). That is exactly the set of **legible, catchable** defects. But the panel's whole
justification — and cold-Opus's specifically — is catching the **subtle / emergent / unknown** defects nobody wrote
down. Those are *by construction absent* from the corpus. So a deep seat whose value is on unknowns shows **low marginal
contribution on the seeded set → flagged "redundant → drop."**

The kicker: **the eval designed to settle the cold-Opus-voting-seat question would systematically frame cold-Opus to
lose — for a measurement artifact, not a real signal.** Precisely the "looks rigorous, is confounded" failure the brief
warned against, aimed at the exact decision it was built to inform.

## Deduped findings + severity adjudication (orchestrator)

### P0 — invalidates a core measurement claim

- **P0-1. Circular / construct-invalid decorrelation.** (cold-opus P0-1; codex P1 "synthetic recall overclaimed"; codex
  P0 "v0 single scenario"; agy #5.) Seeded defects are legible-by-construction; decorrelation / marginal-contribution on
  them is anti-correlated with the real value (catching unknowns) and mis-ranks the exact seat the design wants to judge.
  Evidence: design §3 (corpus = "known defects deliberately present"), §5 ("marginal contribution … *empirically answers*
  the cold-Opus-voting-seat question"), §10 (v0 = single synthetic scenario).
  **Fix:** v0 emits **no** decorrelation / seat-drop verdict; reframe seeded recall as a **floor capability check**
  (does the seat catch legible classes at all), explicitly *not* a panel-value measure. Defer decorrelation to a
  **harvested real-defect** corpus (§9 Q1) validated against a **held-out set of unseeded real defects**. Add an explicit
  construct-validity statement: "recall on this corpus does not generalize to detection of unseeded defects."

- **P0-2. Recall is gameable to 100%.** (agy P0.) Design §5 "treat unmatched findings as 'review for new seed' rather
  than auto-FP" → a seat flags every line / emits noise, incurs zero penalty, scores perfect recall.
  **Fix:** track unmatched findings as a distinct "unverified findings" metric; classify into new-seed vs true-FP via
  consensus filter / manual step; penalize true FPs. Recall must have a precision counterweight.

### P1 — significant confounds, fixable

- **P1-1. Marginal contribution is a panel property, not a seat property — and measures the wrong function for a voting
  seat.** (all three: codex P1, agy #3, cold-opus P1-1.) "Defects only it caught" awards two seats both catching a hard
  defect zero credit; it changes when any other seat is added/removed; and a confirmer/adjudication seat (cold-Opus's
  actual role) has high decision value yet ~0 unique catch. Jaccard is confounded by recall and panel size.
  **Fix:** report marginal contribution only as **panel-conditional** (state the panel); add an independent-agreement /
  confirmation metric for voting seats; use **leave-one-seat-out delta panel-recall/precision by class & severity** with
  CIs; keep Jaccard descriptive only, never a drop/keep decision rule.

- **P1-2. The matcher confounds report-format with detection ability — biased along the exact model×harness axis being
  appraised.** (codex P1, agy #2, cold-opus P1-2.) Exact `file:line + class` matching rewards seats fluent in the
  structured evidence-first contract (codex/claude) and scores prose-narrating seats (agy/gemini, kimi/minimax via
  pi-rpc) as having *missed* a defect they correctly described. A matcher miss is scored identically to a detection miss.
  **Fix:** off-quorum **format-normalization** pass before matching, or match on **class + file-proximity window** not
  exact line; report **matcher-miss vs detection-miss as separate columns**; build a **gold matcher-validation set** with
  precision/recall + error bars and propagate matcher uncertainty into seat metrics (codex). Accelerate the LLM-judge
  matcher to v1 (agy).

- **P1-3. v0 has no scenario-level error bar; single scenario + repeats=3 cannot produce a generalizable decorrelation
  estimate.** (codex P0, agy #5, cold-opus P1-3.) Design §6 variance = "score spread at **fixed scenario**" — captures
  model stochasticity only, not scenario-sampling error, which is the term needed to claim a seat is decorrelated *in
  general*.
  **Fix:** v0 multi-scenario (a small bank) even with a fixed panel; report decorrelation with a scenario-level interval;
  single-scenario v0 validates the *pipeline* only and emits no decorrelation verdict.

- **P1-4. Model and harness are confounded in the matrix.** (cold-opus P1-4.) Design §4 varies one list
  (`reviewer_model: [...]`) with harness pinned per seat → "opus/claude beat gemini/agy" is uninterpretable as
  model-vs-harness. Same-base-model seats (opus reviewer + cold-opus seat + warm-opus orchestrator + possibly an
  opus matcher) correlate for weight-identity reasons the decorrelation oracle misreads as panel structure.
  **Fix:** make the matrix **factorial** (`model` × `harness` independent, feasible cells enumerated); record base-model
  identity per seat; exclude or separately flag same-base-model pairs from decorrelation claims.

- **P1-5. Cross-role appraisal extrapolates review-detection recall to roles the corpus cannot observe.** (cold-opus
  P1-5.) Corpus measures *reviewing* (findings-vs-seeds); implementer fit depends on generation quality, steerability,
  latency/narration overhead — none produce a finding to match. Using per-class detection coverage to pick implementers
  is out-of-construct.
  **Fix:** scope the leaderboard to **reviewer/conformance roles** the corpus exercises; add a distinct generation-quality
  oracle for implementer/adjunct fit, or mark the leaderboard reviewer-role-only.

- **P1-6. "OTel spans = eval evidence, one effort not two" inverts the right dependency.** (cold-opus P1-6; codex P2,
  agy P2.) Spans are best-effort: sampling, batching, attribute size/count limits, truncation are normal and silent. A
  dropped/truncated `findings[]` payload becomes a **false "missed defect"** corrupting recall, marginal contribution,
  and cost-per-true-finding with no error surfaced.
  **Fix:** make `events.ndjson` (complete, append-only, versioned) the **authoritative oracle input**; spans are a
  derived observability projection, *not* the source. If spans feed oracles, assert no-sampling/no-truncation for findings
  and reconcile span count against the NDJSON record per run. Store eval-specific provenance the span layer won't carry:
  fixture SHA, scenario version, seed manifest, seat prompts, matcher version, adjudication records (codex P2).

### P2 — improvements

- **P2-1. cost-per-true-finding inherits matcher/FP labeling noise without the advisory caveat.** (cold-opus P2-1.) "True
  finding" uses the same matcher + unseeded-real-defect ambiguity that makes FP-rate advisory; a seat catching a real but
  unseeded defect is penalized. **Fix:** propagate the FP advisory caveat; report with the same uncertainty band, or only
  on clean-reviewed fixtures.
- **P2-2. Calibration oracle treats one author's seeded severity as ground truth.** (cold-opus P2-2.) Severity is
  context-dependent; a reasonable disagreement is scored "miscalibrated." **Fix:** use a severity **band**/tolerance, or
  multi-rater consensus, and report disagreement-with-consensus, not distance-from-one-author.
- **P2-3. repeats=3 yields non-discriminating CIs on binary catch/miss.** (agy #4, cold-opus P2-3.) Proportions land in
  {0, .33, .67, 1}; the interval is wider than most real seat differences. **Fix:** N≥10, or hierarchical pooling across
  defects/scenarios rather than per-cell 3-rep intervals.

## What the design papers over (unstated assumptions)

1. That a defect you can seed is representative of the defects a panel exists to catch (P0-1).
2. That marginal contribution is a seat property, not a panel-configuration property (P1-1).
3. That report-format-normalized findings and detection ability are separable in the mechanical matcher — they aren't,
   as specced (P1-2).
4. That model and harness effects are separable from a one-dimensional model sweep (P1-4).
5. That observability delivery is good enough for measurement (P1-6).

## Orchestrator read + the fork

The design is **salvageable but needs a reframe, not line-patches.** cold-opus's clean restatement is the honest v0:
*"validate the dispatch→findings→matcher→oracle pipeline end-to-end and report a floor-capability check; emit no
decorrelation or seat-drop verdict."* Decorrelation / appraisal claims defer until a **harvested-real-defect +
held-out-unseeded** corpus, multi-scenario, **factorial** matrix, with a **confirmer metric** for voting seats and
**NDJSON-authoritative** evidence.

**Foundational fork (user's call):** P0-1 is deep enough to question whether seeded-defect detection is the right
primitive for the *decorrelation* goal at all. It can do floor-capability and reviewer-role appraisal honestly — but the
decorrelation question (the one that started this) may need a fundamentally different instrument (e.g. measuring
agreement/independence on *real* review streams, not seeded ones).

### Options

- **(a) Revise the design** per findings: re-scope v0 to pipeline + floor-capability check; defer decorrelation to the
  harvested corpus; fix matcher/metric/evidence layers; factorial matrix; N≥10.
- **(b) Rethink the foundation first:** is seeded-detection the right primitive for decorrelation, or do we need a
  different instrument (real-stream agreement/independence)?
- **(c) Walk specific findings** before deciding.

## Meta-note

The panel found that the design would produce confident numbers that mislead — and the deepest finding is that the
seeded corpus is anti-correlated with the quantity of interest for exactly the seat (cold-Opus) the eval was built to
adjudicate. Same pattern as the autonomous-skill review and the oracle-mechanism catch: the instrument's blind spot was
invisible from inside the design and surfaced only under adversarial, independent review.
