# Instrument 1 v0 schema — panel review synthesis

> Status: **review record** (not committed). Panel review of `docs/eval-instrument1-v0-schema.md`.
> Reviewers: codex, agy-print, cold-opus — independent, unique paths. Date: 2026-06-16.
> Reports: `/tmp/instr1-codex.md`, `/tmp/instr1-agy.md`, `/tmp/instr1-coldopus.md`.

## Verdicts — unanimous fix-before-build

| Reviewer | Verdict |
|---|---|
| codex | REJECT |
| agy-print | FIX_BEFORE_MERGE |
| cold-opus | FIX_BEFORE_BUILD (2 P0, 5 P1, 4 P2) |

**Key framing:** unlike v1/v2 (where the *question* was ill-posed), floor-capability is well-posed — these
are schema defects in a sound instrument. All fixable before code; no foundational reframe.

## Deduped findings + severity adjudication

### P0 — invalidates a core floor claim / load-bearing component

- **P0-A — single scenario + one seed per class ≠ "can catch class X"** (codex P0, cold-opus P0-1, 3/3).
  `repeats:10` adds *stochastic* variance against the *same* seed, not *instance* variance. `caught_rate`
  measures "catches *this* secrets-in-logs line"; the column says "class." N=1-instance result
  over-generalised to a class claim. **Fix:** ≥3 distinct seeds per class (instance bank), OR restate
  every claim/column as *instance-level* ("catches this seeded instance"), and label the construct honestly.
- **P0-B — gameable recall not closed; `true_fp_rate` is descriptive, doesn't gate PASS** (codex, agy,
  cold-opus P1-6; 3/3). `caught_rate` is a *≥1 match* indicator with class-only + a 10-line window, so a
  flag-everything seat lands an in-class finding near a seed and inflates floor recall; the precision
  counterweight is something a *reader* must apply, not a mechanical bar. **Fix:** gate PASS on a joint
  (recall, precision) condition, or require the matched finding to be the seat's primary/highest-severity
  claim at that locus, or narrow class-only matching.
- **P0-C — the matcher's own validation can't see the bias it exists to remove** (cold-opus P0-2 + agy
  normalizer). count-in==count-out is a *cardinality* invariant, not *fidelity*: a confident **mis-class**
  of a prose seat's vague finding is preserved 1-for-1, becomes a clean `detection-miss`, and a **global
  ~30-pair** gold set averages the (seat-correlated) error away — the v2 format-bias re-enters one layer
  in, invisibly. **Fix:** (1) **stratify the gold set per seat**, report per-seat normalizer fidelity;
  (2) normalizer emits `class: unknown` → `matcher-ambiguous` rather than forcing a confident label;
  (3) state the gold set runs **raw→normalize→match end-to-end** (not match-only).

### P1 — fix before build

- **P1-D — gold-set gate math self-defeating** (agy, cold-opus). ~30 pairs (≈2.5/class) cannot clear a
  0.9 Wilson lower-bound *even with a perfect matcher* → the "matcher recall < gate → no floor verdict"
  failsafe would **always** suppress the report. **Fix:** larger, seat-stratified set; gate on a point
  estimate with a stated CI, or restate the gate honestly.
- **P1-E — power starvation: N=10 + threshold 0.5 + Wilson lower-CI ⇒ majority UNKNOWN** (cold-opus P1-3).
  Counts 2/10–8/10 all land UNKNOWN (~7 of 11). The grid reads as seat behaviour but is the instrument's
  lack of power. **Fix:** power calc tying N to threshold/CI; raise N; state CI level; report expected
  UNKNOWN fraction up front.
- **P1-F — `floor_threshold: 0.5` mismatches the "catch at all" construct** (cold-opus P1-4). 0.5-lower-CI
  is a *reliability* bar, not *capability*; the scope says "at all." **Fix:** floor = lower-CI > 0 for "at
  all," or keep 0.5 and rename the construct to "reliably catches (≥50%)." Pick one, say which.
- **P1-G — the grid hands the reader a within-class ranking the wall doesn't block** (agy, cold-opus P1-5).
  The denylist blocks emitted *fields*, not reader-side sorting; printing per-cell `caught_rate`+CI lets a
  reader sort seats within any class column. **Fix:** headline grid shows only PASS/FAIL/UNKNOWN; relegate
  raw rates/CIs to the detail file; and **state honestly that any per-seat-per-class grid is
  reader-convertible — the residual leak is *inherent and accepted*, not "walled."** (The wall holds for
  its literal claim — no seat-value field emitted — but the artifact stays convertible. Name it.)
- **P1-H — gold-set adjudication protocol unspecified → re-imports author/quorum correlation** (cold-opus
  P1-7). The labels every floor error band hangs on are produced by an unspecified adjudicator; if that's
  a quorum/Claude author the "ground truth" inherits the correlation the instrument avoids. **Fix:** blind
  multi-rater adjudication, raters disjoint from the panel/quorum, report inter-rater agreement.
- **P1-I — normalizer count-invariant breaks operationally** (agy #1). Prose seats bundle multiple findings
  in one response, or return "no issues" (no-op); count-in==count-out then forces dropping valid findings
  or inventing dummies. **Fix:** a segmentation step before counting; an integrity check that handles
  bundling + the empty case.

### P2 — underspecified build choices (cold-opus)

- **P2** `enclosing function` match basis needs a boundary oracle (AST/ctags/indentation) + language-coverage statement.
- **P2** match-basis precedence (symbol vs window vs function) + whether basis weights match-strength is unspecified.
- **P2** Wilson CI confidence level never stated — it directly sets every cell's UNKNOWN rate.
- **P2** `legible: true … lint-checked` overclaims — a lint verifies field presence, not genuine legibility (human judgment). Reword "asserted by author; spot-audited."

## What survives (the schema got right)

NDJSON-authoritative; matcher-miss vs detection-miss split; off-quorum normalizer *intent*; the field
denylist + file separation (holds for its literal claim); class taxonomy shared with the posture oracle.

## Orchestrator read

Three recurring lessons, each one level deeper than its predecessor:
1. **The safeguard has a blind spot it can't see** — the global gold set can't detect seat-correlated
   matcher bias (P0-C). Same shape as the posture-oracle and the escaped-defect journal: name it, stratify it.
2. **The wall stops the writer, not the reader** (P1-G) — and the honest fix is to *accept and state* the
   residual convertibility, not claim a wall that isn't there. Drop raw rates from the headline.
3. **Repeats ≠ instances** (P0-A) and **N=10 ≠ power** (P1-E) — basic measurement hygiene the schema skipped.

All fixable in a v0.2 schema revision; floor-capability remains well-posed. Recommend: revise → (re-panel
or build). The decorrelation wall held throughout — no finding says v0 leaks a seat-drop verdict, only that
a per-class grid is reader-convertible and must say so.
