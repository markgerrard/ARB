# ARB eval suite — design (v3, the forced decision)

> Status: **decision record** (not committed). v1 (seeded corpus) and v2 (disagreement corpus) were both
> REJECTed by panel — see `eval-suite-design-review.md`, `eval-suite-design-review-v2.md`. v3 is not a
> third corpus reframe. The panel **proved the internal-measurement approach ill-posed across all
> reframes** (one logical argument, not a fix list), which *forces* a decision rather than another
> iteration. This doc records that decision and the one buildable instrument that survives it.

## 0. The proof that closes internal iteration

Three corpus reframes — seeded → harvested → disagreement — all hit the same wall:

> **Every internal catch-record is definitionally blind to what the whole panel missed.** A record of
> catches can only contain things that were caught; the **never-caught set** — exactly what justifies a
> deep seat — lives outside every internal corpus by construction. Seeded = legible-to-the-seeder;
> harvested = legible-enough-to-be-fixed; disagreement = legible-to-at-least-one-seat. The decorrelation
> question lives outside all three.

This is not a bug to patch in v4. It is a proof that v4, v5, … of any *internal* corpus hit the same
wall. So "keep iterating the design" is closed — by logic, not fatigue.

## 1. The decision: B (standing) + C (build) + A-passive (accrue). Not A-now.

| | What | Status |
|---|---|---|
| **B** | The cold-Opus-voting question is decided **on principle**, not by measurement. | **Standing answer (now).** |
| **C** | Ship **Instrument 1 only** — floor-capability + pipeline validation, wall made *structural*. | **Build (now).** |
| **A-passive** | Start the **escaped-defect journal** — the external oracle, accruing at reality's pace. | **Begin recording (now).** `docs/escaped-defect-journal.md` |
| **A-active** | Build a harness around the external oracle. | **Deferred** — no data to harness yet; A accrues passively first. |
| internal decorrelation instrument | seeded/harvested/disagreement corpus as a seat-drop oracle. | **Abandoned as ill-posed** (§0). The disagreement corpus survives *relabeled* as calibration data (`docs/disagreement-corpus.md`), barred from seat-drop. |

## 2. B — the standing answer (cold-Opus stays voting, on principle)

The eval work tried three times to find a number that overrides the principle, and **proved the number
does not exist** (§0). That is not a retreat; it is the eval delivering its actual finding: *this
decision is correctly made on principle, and here is the proof that's not a cop-out.*

The asymmetry measurement cannot touch:
- Dropping a seat that catches *illegible* defects you **can't measure** → unbounded cost (the escape
  bites prod).
- Keeping a maybe-redundant seat → one extra dispatch.
- Measurement is **blind to the upside by construction** (§0) → it cannot license the drop, because the
  thing that would justify dropping cold-Opus is the thing no internal instrument can see.

This is exactly what the `autonomous-mode` skill already concluded (cold-Opus voting, model-correlation
logged as an open limitation). v3 confirms it with a proof and adds nothing that overrides it. **Standing
answer: cold-Opus stays voting.** Revisit only if the escaped-defect journal (A) ever shows cold-Opus's
dissents systematically *don't* catch escapes — which is the only evidence that could move it, and it
must come from outside the panel.

## 3. C — Instrument 1 (the one buildable instrument), with the wall made structural

**Purpose (only this):** (a) validate the dispatch→findings→matcher→oracle pipeline; (b) **floor-capability
check** — can a seat catch the *legible* classes at all? Knowing a seat can't catch seeded secrets-in-logs
is worth knowing. That is the entire claim.

**Load-bearing rule (unchanged from v2 §1, now enforced in code per cold-opus P1-2/P2-3):**
> Instrument 1 emits **no** decorrelation / marginal-contribution / seat-drop verdict — ever.

**Structural-wall fixes (cold-opus v2 P1-2, P2-3):**
- **No ranked leaderboard.** A ranking *is* an input to a drop decision; cold-Opus ranks low on *legible*
  recall and the forbidden verdict re-enters through a comparative artifact. Instrument 1 reports an
  **unordered pass/fail-by-class table** ("does each seat clear the floor on each legible class?"), not a
  sorted ranking.
- **Mechanical output separation.** Instrument 1 and any decorrelation/voting artifact live in **separate
  output namespaces/files**; the reporter **refuses to emit any seat-drop field** from Instrument 1
  output. The wall is enforced by the code, not honored in prose (same lesson as the posture oracle
  needing to be mechanical, one level out).

**Confounds (all CLOSED by the v2 panel, retained):** non-gameable recall (unverified-findings split +
true-FP penalty + precision counterweight); off-quorum format-normalizing matcher + class/proximity
match + matcher-miss vs detection-miss separated + gold matcher-validation set; factorial `model × harness`
matrix + base-model identity; N≥10; cost-per-true-finding inherits FP advisory band; calibration vs
multi-rater consensus band. Defect classes = the posture-oracle taxonomy + correctness/perf/logic/test-gap.

**Scope:** reviewer/conformance roles only (corpus measures *reviewing*); implementer/adjunct fit needs a
separate generation-quality oracle, deferred. CLI shape lifted from ccswarm (`run`/`list`/`plan`/`diff`/
`approve`), evidence in **append-only NDJSON** (authoritative; OTel spans are a derived observability
projection, never the measurement source — v2 P1-6).

## 4. A-passive — the escaped-defect journal (the only thing that can answer the question)

`docs/escaped-defect-journal.md`, started now, empty (correctly). Records every defect the panel
**reviewed-and-passed** that **later escaped** (prod incident / late catch), with a **leave-one-seat-out**
annotation (*would removing seat X have changed the outcome?*, grounded in the seat's actual review trace,
not speculation). An escape is a *whole-panel miss by construction* — the never-caught set §0 says is
absent from every internal corpus. This is where B's standing answer eventually gets external signal, if
it ever does. **But even this oracle is bounded** — it sees only the *observed / surfaced* never-caught
set (an escape that never visibly bit never enters); the unobserved escape is its structural blind spot,
the §0 invariant one level out by *reality's* legibility rather than the panel's. A wider net than any
internal corpus, not an unbounded one (see the journal's own ⚠ section).

**It is a journal, not a harness** — you record escapes as reality produces them; you do not build-and-run.
Sparse is honest: measuring panel blind spots requires evidence from *outside* the panel. A-active (a
harness around this oracle + leave-one-seat-out decision-impact metric) is deferred until entries accrue.

## 5. The disagreement corpus — kept, relabeled (not abandoned)

`docs/disagreement-corpus.md` survives as **Instrument-2-lite: calibration/agreement data on legible
defects** — *not* the decorrelation oracle it was first conceived as (its lone-correct metric is the
rejected confounder, and it's legibility-bounded per §0). It has real value: DC-001 (agy's
severity-overcall on a latent/conditional defect) is exactly the kind of live **calibration** read it's
good for — which seats over/undercall, accruing over time. **Barred from seat-drop verdicts** and from the
seeded suite, same as Instrument 1.

## 6. What each instrument answers (the map)

| Instrument | Answers | Cannot answer | Status |
|---|---|---|---|
| Instrument 1 (seeded floor) | "Can this seat catch legible classes at all?" + pipeline OK | anything about panel value / decorrelation | **build now** |
| Disagreement corpus (Instr-2-lite) | seat calibration / agreement on legible defects | decorrelation / seat-drop (legibility-bounded) | **keep, relabeled** |
| Escaped-defect journal (A-passive) | "Does a seat catch what the panel missed *and reality surfaced*?" | unobserved escapes (never visibly bit) — bounded by reality's legibility, not unbounded | **start accruing** |
| Principle (B) | "Should cold-Opus stay voting?" → **yes**, on the unmeasurable-but-real asymmetry | — | **standing answer** |

## 7. Cross-role model appraisal (your live use)

Served by Instrument 1, **reviewer-role only**, honestly bounded: pi-harness vs claude-code-harness seats
get a floor-capability pass/fail-by-class read on legible defects + calibration data from the disagreement
corpus. It will **not** tell you who's the "best reviewer" (that's decorrelation, ill-posed internally) —
it tells you who clears the floor and who over/undercalls. For implementer-role appraisal, the
generation-quality oracle is still to be designed.

## 8. Drift track — unchanged, deferred (v2/v3+)

No cheap ground truth for orchestrator drift; same candidates (injected out-of-scope temptation;
trace-diff goal-vs-decisions). After Instrument 1 ships.

## 9. Meta (kept in the design on purpose)

Three times this conversation, dogfooding caught a confidently-wrong measurement of the panel's own value;
this third time it caught that the *measurement approach itself* is ill-posed for the decorrelation
question, across three reframes, for a structural reason no iteration escapes. The benchmark would have
told you to drop your best seat — three different ways. You won't, because each time you ran the design
through the seat before trusting it. The edge is *dogfooding the measurement, not just the thing measured*
— and v3 is what that edge produces: ship the honest floor-check, decide the unmeasurable on principle
with the proof in hand, and let reality (not a manufactured proxy) accrue the only data that could ever
say otherwise.
