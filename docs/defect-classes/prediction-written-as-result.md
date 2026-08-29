# Prediction written as result

**The class where fabricated evidence enters the repo without anyone lying.** An expected outcome
gets written in the grammar of an observation — "produces X", "returns Y", "confirmed" — *before
the run that would produce it*. If the run then happens and agrees, nothing looks wrong. If the
run is skipped, deferred, or forgotten, a claim-shaped sentence sits in the repo permanently with
no execution behind it and nothing downstream able to tell the difference.

**The failure is not predicting. Predicting is good practice** — it is pre-registration, and it is
exactly what makes a mismatch *visible* when the run disagrees. The failure is writing the
prediction **as a result**: a docstring saying "inject-revert produced OPEN DOOR" is an evidence
claim, and it existed before any evidence did.

## The rule

**Expected outcomes are written as predictions until an execution artefact exists.** Claim-shaped
sentences get written *from* the run, not before it. Concretely:

- Before the run: "expected: this should fail with OPEN DOOR" — a prediction, clearly marked.
- After the run: paste what actually happened, including the parts you did not predict.
- If the two disagree, the disagreement is the finding — that is the pre-registration paying off.

## The detection move

- **On any claim-shaped sentence in a commit, docstring, or report:** *does an execution artefact
  for this exist, and did I read it?* Not "would this be true" — *was it observed*.
- **On any document written alongside the work it describes:** *which sentences here were written
  before their evidence?* Those are the ones to re-derive from output.

## Canonical instance (2026-07-26, the gate's deny-proof)

While building the deny-proof for the bus-side gate, the docstring recorded:

```
INJECT-REVERT RESULT, run 2026-07-26 — proof is not vacuous:
  `if not found.attested:` -> `if False:` produced
    AssertionError: OPEN DOOR: {'task': 't', 'claim_ref': 'c-1'} was admitted
  `if declared_lane == "exempt":` -> `if False:` produced
    AssertionError: OPEN DOOR: {'task': 't', 'lane': 'exempt'} was admitted
```

The first was true. **The second was false** — that injection left the suite green
(see [`refusal-is-ambient-assert-the-code`](refusal-is-ambient-assert-the-code.md)). Both
sentences were written before either injection ran, in identical confident grammar, and the
docstring was already saved to disk when the run contradicted it.

Two things make this worth filing rather than shrugging off:

1. **It happened inside the countermeasure.** The subject matter was *unverified claims*; the
   artefact was a proof *about verification*; the defect operated anyway. Working on the problem
   confers no immunity from it.
2. **Only the run caught it.** No review of the docstring would have — it was well-formed,
   plausible, and internally consistent. It read exactly like the true half.

## Relationship to the sibling class

[`verification-is-context-triggered-not-risk-triggered`](verification-is-context-triggered-not-risk-triggered.md)
explains *when* verification fires. This entry covers what happens in the window before it fires:
the claim is already written down. The two compose badly — a scripted check that runs later
"confirms" a sentence that was authored earlier, and the artefact ends up looking verified either
way.

## Not yet enforced by machinery

Unlike [`refusal-is-ambient-assert-the-code`](refusal-is-ambient-assert-the-code.md), this class
has **no lint behind it yet**, and the honest reason is that detecting "claim written before
evidence" from the text alone is not obviously decidable — the sentence looks identical in both
cases. The tractable version is a convention plus a narrow check: result-shaped sections in test
docstrings (`INJECT-REVERT RESULT`, `OBSERVED`, `RUN OUTPUT`) must cite a run, and the check flags
those headings when no adjacent artefact reference exists. Recorded here as the next harvest
candidate rather than claimed as done — which is, itself, the rule this entry is about.
