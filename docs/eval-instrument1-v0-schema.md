# Instrument 1 — v0.2 schema (seeded floor-capability suite)

> Status: **schema draft, revised post-panel** (not committed; not built). Implements eval-design v3 §3.
> v0.1 was unanimously FIX_BEFORE_BUILD — `docs/eval-instrument1-v0-schema-review.md`. The panel found
> **nothing foundational** (floor-capability is well-posed); every finding was measurement hygiene. v0.2
> folds them in. **Scope (viability, not reliability):** *can* a seat catch the legible defect classes —
> is its detection demonstrably above its own noise floor? + does the pipeline run? A capability floor
> (can-it-catch), **not** a graded reliability score (how-often). **Emits no decorrelation /
> marginal-contribution / seat-keep/drop / ranking verdict — ever.**

## What changed v0.1 → v0.2 (the panel fixes)

- **§0.5 Power budget (new) — A/D/E unified.** repeats≠instances (P0-A), N=10 underpowered (P1-E), and
  gold-set gate math self-defeating (P1-D) were *one* root cause: thresholds without the power to resolve
  them. v0.2 sets a power target **once** and derives instances, repeats, CI level, and gold-set size from
  it, mutually consistent — not three independent numbers.
- **PASS gates on joint (recall, precision)** — `true_fp` now *gates*, not describes (P0-B).
- **Gold set stratified per seat** + per-seat normalizer fidelity (P0-C; principle:
  `measurement-principles.md` P1 — *aggregate validators hide correlated error*).
- **Construct = viability (can-catch-above-noise), not reliability-at-θ** (P1-F + review of v0.2). v0.1's
  "catch at all" was mislabeled; a first pass renamed it to "reliably catches at θ≥0.7" — but that
  silently swapped a *capability* question for a *graded reliability* one (and §0 scope still claimed
  capability), and a graded bar is exactly the reader-convertible gradation principle P2 / P1-G works to
  minimize. Resolved to **viability**: PASS = detection demonstrably above the seat's own spurious-match
  floor. Near-binary, honest to v3 scope, and harder to convert into a value ranking.
- **Headline grid = PASS/FAIL/UNKNOWN only**; raw rates/CIs → detail file; disclaimer carries the
  convertibility caveat (P1-G; principle P2 — *the wall stops the writer, not the reader*).
- **Gold-set adjudication protocol** specified (blind, multi-rater, off-quorum, IRR reported) (P1-H).
- **Normalizer** does fidelity not cardinality; segmentation + no-op handling (P0-C, P1-I).
- **P2s:** function-boundary oracle named; match-basis precedence; CI level stated; `legible` reworded.

## §0.5 Power budget (set the target first, derive everything else)

**The instrument's statistical parameters are derived from one power target, not chosen independently.**
Set these, then compute the rest; do not patch one without re-deriving the others.

**Construct (viability, not reliability — P1-F):** "seat *can* catch class C — its detection rate is
**demonstrably above its own spurious-match (noise) floor**." A capability floor (can-it-catch), not a
graded reliability target (how-often). This folds the precision gate (P0-B) in naturally: a flag-everything
seat's detection rate ≈ its noise rate, so it is *not* demonstrably above noise → FAIL, no free PASS. A
graded "≥70% reliable" bar is deliberately **out of scope** — it reintroduces the convertible gradation the
wall (P1-G / principle P2) exists to minimize.

**Noise floor `ν_s` (per seat):** the seat's spurious in-class match rate, estimated from **control loci**
— so "above noise" is measured against the seat's *own* false-match behaviour, not a global constant.

**A control locus is a plausible-but-clean instance of the target class (decision D3).** It is a location
where the class *could* appear and a reviewer *might* flag it, but where there is genuinely no defect — so a
flag there is a false positive (e.g. a `secrets-in-logs` control locus is a logging call that logs a
**request ID, not a token**). This makes `ν_s` measure "how often does this seat cry wolf on defect-*adjacent*
clean code" — the FP rate that matters. **Rejected (degenerate):** clean lines unrelated to the class
(blank/comment/import) make `ν_s` artificially ≈0, so a trigger-happy seat looks precise and every seed-catch
looks viable — the noise floor measuring the wrong thing. Control loci must be **distinct in the F6 sense**
(N near-identical clean logging calls are not N independent loci — they must span how clean instances of the
class actually appear, or `ν_s`'s CI is a lie) and **numerous enough that `control_loci ≥ T`**.
**Implementation:** the seat does ONE review pass; its findings are matched against seeds (→ detected) AND
control loci (→ false positive / noise) — noise is the seat's own review crying wolf on clean instances, not
a separate "probe" dispatch. **First-run note:** one seed + few control loci lacks power to clear above-noise
→ verdict UNKNOWN, **which is a PASS** (the machinery ran and honestly reported insufficient data) — distinct
from UNKNOWN-by-construction. The run output must say so.

**Targets (stated once, used everywhere):**
- viability test — PASS when `caught_rate` lower-CI(α) **>** `ν_s` upper-CI(α) (demonstrably above own
  noise); FAIL when `caught_rate` upper-CI ≤ `ν_s` lower-CI; else UNKNOWN.
- `V` — the *viable* effect size the suite must be powered to confirm: a seat whose true catch-rate exceeds
  noise by ≥ `V` should resolve PASS. Default `V = 0.4` (scenario-configurable). This replaces the old θ; it
  sets *power*, not a reliability bar.
- `alpha` (α) — CI level for all Wilson intervals. Default 0.05 (95%). **Stated, not implicit (P2).**
- `I_min` — minimum *distinct instances* per class for a class-level claim (construct validity, P0-A).
  Default 5. Fewer → claims are relabeled instance-level, not class-level.
- `R_min` — minimum *repeats per instance* for stochastic stability. Default 3.

**Derived (computed by `arb-eval plan`, NOT hand-estimated — the numbers below are the tool's actual
output at the default target, recorded here so doc and code can't drift):**
- **Trials per (seat,class)** `T` = smallest trial count at which a seat whose true catch-rate exceeds its
  noise floor by ≥ `V` resolves PASS (its `caught_rate` lower-CI clears `ν_s` upper-CI) at α — i.e. the two
  Wilson intervals separate. For `V=0.4`, `ν_s≈0.1`, α=0.05, **T = 19** (control loci scale with trials, so
  `ν_s`'s CI shrinks alongside). *(A first hand-estimate said "≈30"; the tool computes 19 — the doc now
  carries the computed value. The power calc mirrors the oracle's exact `caught.lo > noise.hi` test; an
  earlier point-`ν` version returned a spurious T=3, caught by running it.)*
- **Decomposition** `T = instances × repeats`, with `instances ≥ I_min` and `repeats ≥ R_min`. Computed:
  **5 instances × 4 repeats = 20 (≥ 19)** (both clear their minima: I_min=5, R_min=3). *Instances* give
  class-generalization; *repeats* give stochastic stability — both required, neither substitutes for the other.
- **Gold-set size**, **per-seat-stratified** (P0-C): per seat, `G_s` = size whose per-seat matcher recall
  Wilson-95% lower-CI clears the matcher gate `g` (default g=0.85, assuming true matcher recall ~0.95).
  Computed **G_s = 45 pairs/seat** → 3 seats = **~135 gold pairs** (vs the v0.1 global 30 that could never
  clear the gate). Gate is applied **per seat**: a seat whose matcher lower-CI < g has *its* floor numbers
  suppressed, not the whole report.
- **Required control loci** `≥ T` per class: the noise floor `ν_s` is estimated over control loci (loci
  with no seeded defect of that class), and the budget assumes `L ≥ T`. Build too few and the grid comes
  back all-UNKNOWN — which reads as "weak seats" but means "too few control loci." `plan` prints this
  requirement and `run` validates `control_loci ≥ T` (build-review P1-3 — the L-trap).
- **Expected UNKNOWN fraction** is computed and **printed up front** (P1-E) so a majority-UNKNOWN grid
  reads as "instrument power," not "seat behaviour."
- **Unachievable targets RAISE, never return the cap** (build-review P0): if an effect size is not
  resolvable within the trial cap, `power.compute` raises `Unachievable` and `plan` prints
  "UNACHIEVABLE — raise V or ν"; it never presents the cap (2000) as a converged budget. (Returning the
  cap as a real number was the exact shape of the original T=3 bug, re-found by the build panel.)

## 1. Scenario schema (`fixtures/<name>/scenario.yaml`)

```yaml
id: floor-001
description: "Upload handler review — legible posture + correctness seeds"
subject:
  repo: fixtures/webapp
  base: <sha>
  head: <sha>
  spec: fixtures/webapp/spec.md          # required only if any seed is a conformance-class defect
seeded_defects:                           # >= I_min distinct instances PER class for a class-level claim
  - id: D1
    class: secrets-in-logs                # closed taxonomy (§2)
    location: { file: "src/auth/session.py", line: 88, symbol: "log_request" }
    description: "bearer token written to request log"
    consensus_severity: P1                # calibration only (§4); never gates floor
    legible: true                         # asserted by author; spot-audited (NOT lint-proven — P2)
  # ... >= 5 more secrets-in-logs instances for a class claim; fewer => instance-level claim only
panel:                                    # v0 = FIXED panel (matrix is v1)
  - { seat: codex,     model: "gpt-5.5", harness: codex }
  - { seat: agy,       model: "gemini",  harness: agy-print }
  - { seat: cold-opus, model: "opus",    harness: claude-subagent }
power: { V: 0.4, alpha: 0.05, I_min: 5, R_min: 3, matcher_gate: 0.85 }   # V = viable effect size ABOVE noise (a power target, not a reliability bar)
# instances/repeats/gold-size are DERIVED from power (§0.5); not set here independently.
```

**Load invariants:** classes ∈ taxonomy; `location.file` exists at head; `legible: true` for all v0 seeds;
seats registered; **instances-per-class ≥ I_min for any class-level PASS** (else that class is reported
instance-level, explicitly labelled).

## 2. Defect-class taxonomy (closed; shared with the posture oracle)

`cors`, `tls-transport`, `secrets-in-logs`, `auth-on-endpoint`, `authorization-scoping`, `input-trust`,
`pii-logging-retention`, `egress`, `correctness`, `perf`, `logic`, `test-gap`. No free-text classes
(load error) — keeps eval suite and posture oracle co-evolving.

## 3. Finding + matcher contract (P0-C, P1-I — fidelity, not cardinality; per-seat validated)

Raw finding captured verbatim to NDJSON. A **segmentation step** first splits a seat's response into atomic
findings (prose seats bundle several in one reply; "no issues" → zero findings — both handled, P1-I). Then
an **off-quorum, non-Claude normalizer** maps each atomic finding to:

```yaml
normalized_finding:
  seat: codex
  instance_id: D1
  repeat: 3
  class: secrets-in-logs | unknown   # MAY emit `unknown` — low confidence routes to matcher-ambiguous,
                                      # NOT a forced (possibly wrong) label (P0-C)
  location: { file, line?, symbol? } # partial allowed
  severity: P1                       # seat's claim (calibration, §4)
  statement: "..."
  raw_ref: <ndjson-offset>
  confidence: 0.0–1.0                # normalizer's class/location confidence
```

The normalizer **restructures, does not invent**: integrity is a **fidelity** check (each normalized
finding traces to a raw span; no raw finding silently dropped; no normalized finding without a raw source),
**not** count-in==count-out (which fails on bundling/no-op — P1-I).

**Match rule:** `class` equal (excluding `unknown`) **AND** location overlap — same `symbol`, OR same
`file` within ±`W` lines (default 10), OR same enclosing function (boundary via **tree-sitter** where a
grammar exists, else ctags; language coverage stated per fixture — P2). **Basis precedence** symbol >
function > window, recorded *and* ranked as match-strength (P2). Outcome per (seat, instance, repeat):
`detected` / `detection-miss` / `matcher-ambiguous` (includes `class: unknown` and low-confidence) —
separate columns, never folded into recall.

**Gold matcher-validation set — per-seat stratified (P0-C, P1-H):** ~`G_s` hand-adjudicated
(raw-finding → normalize → match → seed, correct-verdict) tuples **per seat** (~120 total at defaults),
exercising the **full raw→normalize→match pipeline** (not match-only). Yields **per-seat** matcher
precision/recall + confusion matrix, propagated as a **per-seat** error band onto that seat's floor
numbers. **Adjudication protocol (P1-H):** blind, ≥2 raters **disjoint from the panel/quorum** (not
Claude-family), **inter-rater agreement reported**; disagreements resolved by a third disjoint rater. A
seat whose matcher recall lower-CI < `matcher_gate` → its floor numbers suppressed (per-seat, not global).

## 4. Oracles & metrics (floor only)

- **Floor-detection** — per (seat, class): `caught_rate` = fraction of (instance × repeat) trials with a
  matched detection, over `T` trials. Wilson CI at α. **Viability test = demonstrably above the seat's own
  noise floor `ν_s` (the precision gate, P0-B, folded in — not a separate θ):**
  - PASS if `caught_rate` lower-CI **>** `ν_s` upper-CI (catches real seeds more than it spuriously matches);
  - FAIL if `caught_rate` upper-CI **≤** `ν_s` lower-CI (demonstrably **below** its own noise — a real
    failure mode, e.g. a broken matcher or anti-correlated seat);
  - else UNKNOWN. A seat **at** its noise floor resolves UNKNOWN, **not** FAIL — correct conservative
    behaviour (equal rates → overlapping CIs, can't separate). So the power budget powers *PASS-detection
    of a viable (≥ ν+V) seat*; it does **not** promise to resolve a noise seat to FAIL (build-review #4).
    A flag-everything seat has `caught_rate ≈ ν_s` → not above noise → no free PASS (the precision gate is
    *intrinsic*, not a bolted-on `fp_max`).
- **`ν_s` / `true_fp_rate`** — `ν_s` (the noise floor) is the seat's spurious in-class match rate over
  **control loci** (no seeded defect of that class); `true_fp_rate` over flagged-but-unmatched findings is
  classified (off-quorum consensus + blind manual spot-check, same disjoint raters) into
  `new-seed-candidate` vs `true-fp` and reported as a cross-check on `ν_s`. The viability gate runs off
  `ν_s`; `true_fp_rate` corroborates it.
- **Calibration** (descriptive, never gates) — seat severity vs `consensus_severity` as a band confusion
  matrix.
- **Cost** (descriptive) — tokens/seconds/dispatches per trial; `cost-per-detected-instance` carries the
  fp advisory caveat.

## 5. Report shape — minimized convertible surface + named residual (P1-G, principle P2)

**Headline grid: PASS / FAIL / UNKNOWN only.** No raw rates, no CIs, no aggregate column, no total, no
meaningful sort (rows alphabetical by seat). Raw `caught_rate` + CIs + matcher error bands live in a
**separate detail file**, not the headline.

**Mechanical guard (holds for its literal claim):** reporter writes only to `floor/`; a hard-coded
field-name denylist (`marginal_contribution`, `decorrelation`, `lone_correct`, `seat_value`, `keep`,
`drop`, `rank`, `score`, `redundant`) raises on emit; floor and any future decorrelation output are
physically separate dirs.

**Named residual (the honest part — P1-G):** the disclaimer on **every** report states:
> *Floor viability on legible seeded defects — can this seat catch the class above its own noise. Does NOT
> measure reviewer value, reliability (how-often), decorrelation, or seat-keep/drop (ill-posed internally —
> eval-design v3 §0). **A per-seat-per-class grid is inherently
> reader-convertible into a ranking this suite never emitted; that residual is accepted, not walled
> (measurement-principles.md P2). Do not sort these cells and read seat value into them.** A FAIL means
> "missed legible seeds in this class," never a drop signal.*

This caveat is load-bearing: "accepted residual" must not decay into "ignored residual." It ships in the
report template, not just this doc.

## 6. Evidence — NDJSON authoritative (v2 P1-6)

`floor/runs/<run-id>/events.ndjson`, append-only, versioned. Events: `scenario_loaded`, `dispatch_start`,
`dispatch_end`, `finding_emitted` (verbatim), `segmented`, `normalized` (with fidelity-trace + confidence),
`matcher_decision` (basis + strength + gold provenance), `oracle_result`, `run_end`. Oracles read NDJSON,
not spans. Per-run provenance: fixture SHA, scenario+power params, seat prompts, normalizer model+version,
matcher `W` + boundary-oracle, gold-set version + IRR, RNG seeds. OTel spans (if added) are a derived
observability projection, never oracle input.

## 7. Resolved vs still-open

**Resolved by v0.2:** A (instance bank ≥ I_min), B (joint recall∧precision gate), C (per-seat stratified
gold set + fidelity normalizer + `unknown` routing), D (gold size derived to clear the gate), E (power
budget + printed UNKNOWN fraction), F (viability construct — can-catch-above-noise, not reliability-at-θ), G (PASS/FAIL/UNKNOWN headline + named
residual), H (blind off-quorum multi-rater + IRR), I (segmentation + no-op + fidelity check), P2s.

**Still open (build-time, not blockers):** exact `T`/`G_s` from the real power calc (defaults shown are
worked, not authoritative); fixture corpus construction (the ≥5 instances/class are real labor); the
boundary-oracle's language coverage for the chosen fixtures.
