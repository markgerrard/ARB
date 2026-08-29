# Instrument 1 build — execution-primary panel review

> Review record (not committed). Panel review of `tools/eval/` (the built code). Reviewers: codex,
> agy-print, cold-opus (general-purpose Opus, with Bash) — independent, **execution-primary** (run-and-break,
> reading-only findings lower-confidence). Date: 2026-06-16. Reports: `/tmp/instr1b-{codex,agy,coldopus}.md`.

## Why execution-primary
A prior bug (`power.compute` → T=3 vs ~19) **passed all 13 unit tests** because the tests asserted
*coherence* and the calc was coherently wrong; only *running* `plan` caught it. So the brief made running
the load-bearing task. It paid off: the panel found a *second* instance of that exact bug class (cap-as-
converged) that reading + coherence-tests both missed again.

## Verdicts
codex REJECT · agy FIX_BEFORE_MERGE · cold-opus SHIP_WITH_NITS (0 P0, 3 P1, 3 P2). Orchestrator
adjudication: **FIX_BEFORE_COMMIT** — cold-opus is lenient because nothing is wrong in the *default* range,
but every defect bites a user authoring a non-default scenario (i.e. real use).

## Confirmed sound (by independent execution — do not touch)
- **Wilson + z_for correct** to ≤2e-9 vs TWO independent references (codex: stdlib `NormalDist.inv_cdf`;
  cold-opus: erf-bisection), incl. non-tabulated alpha. The stats primitive is solid.
- **No wrong PASS** in ~30 adversarial viability cases; tiny-n → UNKNOWN; the T=3 class is genuinely fixed.
- `run` fails loud (NotImplementedError), never fakes a verdict.

## Fixes (all surfaced by RUNNING)
1. **[P0] Cap-as-converged** (cold-opus P1-1; 3/3 saw the symptom). `_min_trials_above_noise` / `_min_gold`
   return `cap` (2000) when unresolvable, indistinguishable from convergence; `plan --scenario {V:0.02}`
   prints `T=2000, 45×45` as a real budget. **Same shape as the T=3 bug.** Fix: raise / flag `capped=True`
   on cap; CLI prints "UNACHIEVABLE — raise V or ν". Never present the cap as a converged T.
2. **[P1] Wall is a name-denylist, trivially escaped** (3/3 by demonstration): `seat_quality`, `ordering`,
   `rank_order`, `goodness`, `tier`, `quality`, `priority`; nested `{"detail":{"rank":1}}`; whitespace
   `" rank "`; and a **seat keyed `"codex (rank 1)"` rendered into the grid**. Fix: invert to an
   **allowlist** — `guard` raises unless every key ∈ a fixed permitted set, recursing into nested
   structures; `render_grid` restricts seat keys to registered ids / safe charset and class keys to the
   taxonomy. (Principle P2 / closed-taxonomy lesson, one level out.)
3. **[P1] `L=n` control-loci assumption unenforced/unsurfaced** (cold-opus P1-2). Budget T assumes noise is
   estimated over L=n=T control loci; the oracle uses whatever `noise_n` the scenario has. Small L →
   all-UNKNOWN (reads as "weak seats", means "too few control loci"); large L → PASS well before T
   (`classify(2,2,0,100)`→PASS). Fix: add `control_loci` to schema; `plan` surfaces "budget assumes ≥T
   control loci/class"; validate `control_loci ≥ T`.
4. **[P1→doc] true-ν → UNKNOWN not FAIL, 108/108** (agy + codex). The power budget *implied* a noise seat
   resolves FAIL at T; impossible (FAIL needs non-overlapping CIs, caught==noise overlaps). cold-opus
   reframed correctly: **at-noise→UNKNOWN is the right conservative behavior**; FAIL = demonstrably *below*
   noise (a real failure mode). Fix the calc's *implied promise* + docstrings (incl. viability.py line-42
   comment that says "indistinguishable/below" where code is strictly below) — **not** the oracle.
5. **[P2] No range validation** (3/3): `alpha=0` crashes `z_for`; `alpha=1.2`→T=2; `V≤0`→cap; negative `nu`
   crashes. Fix: validate `0<alpha<1`, `0<V<1`, `0≤nu<1` at load (clean `ScenarioError`); clamp wilson
   `k∈[0,n]`.
6. **[P2] CLI leaks tracebacks** on missing/malformed scenarios (3/3) — inconsistent with `_list` which
   catches cleanly. Fix: `_plan`/`_run`/`main` catch `ScenarioError`/`JSONDecodeError`/`FileNotFoundError`.

## Meta
The execution-primary spine was the decisive choice: the cap-as-converged P0 was found by *running a
plausible non-default input*, not by reading. The remediation adds tests that assert **behavior under
degenerate input** (cap raises, escapes blocked, bad params rejected) — not happy-path coherence, which is
the property the original suite wrongly validated.
