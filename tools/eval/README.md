# ARB eval suite — Instrument 1 (seeded floor-capability)

Measures one thing: **can a seat catch the *legible* defect classes at all** (detection demonstrably
above its own noise floor) + does the dispatch→score pipeline run. It is a **capability floor**, not a
graded reliability score, and it **emits no decorrelation / seat-keep-drop / ranking verdict — ever**
(eval-design v3 §3).

- **Design / schema:** `docs/eval-instrument1-v0-schema.md` (v0.2)
- **Principles it's built on:** `docs/measurement-principles.md` (P1 aggregate-validators-hide-correlated-error; P2 the-wall-stops-the-writer-not-the-reader)
- **Why decorrelation lives elsewhere:** `docs/eval-suite-design.md` (v3 — the internal decorrelation question is ill-posed; this suite is deliberately scoped away from it)

## Status

**Built + tested (deterministic core + confined dispatch pipeline):**
- `arb_eval/stats.py` — Wilson score interval (pure stdlib).
- `arb_eval/power.py` — the §0.5 power budget: set ONE target (`V`, α, `I_min`, `R_min`, `matcher_gate`),
  derive trials `T`, instance/repeat split, gold-set size, expected-UNKNOWN band. Mirrors the oracle's
  exact test (sizing must match what it sizes for).
- `arb_eval/viability.py` — the oracle: PASS iff `caught.lo > noise.hi`; FAIL iff `caught.hi ≤ noise.lo`;
  else UNKNOWN. Precision gate is intrinsic (a flag-everything seat sits at its own noise → never PASS).
- `arb_eval/report.py` — the structural wall: PASS/FAIL/UNKNOWN grid only (no raw rates in headline),
  a field-name denylist that *raises* on any seat-value/ranking field, alphabetical rows, and the
  named-residual disclaimer (convertibility accepted-not-walled).
- `arb_eval/schema.py` — scenario load + closed taxonomy (shared with the posture oracle) + class-level
  vs instance-level validity (≥ `I_min` distinct seeds).

**Built pipeline:** confined dispatch per seat×repeat → segment → off-quorum normalize → match
(class + proximity / tree-sitter|ctags boundary) → noise estimate over control loci → NDJSON
(authoritative) → report. Real-corpus builders/lint and gold matcher workflow are wired; live
docker/engine gates and real corpus authoring remain manual runbook work.

## Run

```bash
cd tools/eval
python3 -m pytest . -q                          # 58+ tests, all green
python3 -m arb_eval.cli plan                    # print the derived power budget
python3 -m arb_eval.cli plan --scenario scenarios/floor-001.example.json
python3 -m arb_eval.cli list                    # discovered scenarios
python3 -m arb_eval.cli run --scenario ...      # wired to pipeline.run_floor via cli.py
```

`plan` is the load-bearing demo: it shows the A/D/E consolidation producing coherent, mutually-consistent
numbers from one target — the thing the panel cared most about. (Running it already caught one real bug:
the power calc had to mirror the oracle's CI-vs-CI test, not a point-noise shortcut.)
