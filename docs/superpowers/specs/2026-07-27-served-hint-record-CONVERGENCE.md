# Served-hint record — convergence criteria (when the loop stops)

**Status:** adopted 2026-07-28 under an explicit operator delegation ("decide now what convergence
means"). Binds future rounds of this spec. **Flagged for co-sign** — it sets a stopping rule at MUST
strength, which is constitution-layer under `CLAUDE.md`; recorded as adopted-by-delegation until
co-signed, not as self-authored doctrine.

## The problem this exists to solve

Nothing currently defines when the review loop ends. Every panel is disposed to find things; every
fold adds them; the document grows monotonically because growth is the integral of an unbounded
process. A line budget slows the growth *rate*; it cannot remove the ceiling, because the escape
hatch has already demonstrated that findings add irreducible content.

Without a finish line, every other fix — partitioning, checkpointing, tighter budgets — only raises
the altitude at which the same wall is hit. With one, the question stops being "how do we sustain
infinite growth" and becomes "how do we get through the remaining two or three rounds."

## STOP CONDITION A — converged (success)

**A round is the last fold when it returns no P0 and no P1 findings, and no seat votes `block`.**

Findings at P2 or below, and findings of the wording/clarity kind rather than the design-defect kind,
do not justify another fold. They are recorded in the panel record and either taken in the same
motion or dropped.

On that round: the fold is applied, the spec is **frozen**, and the panel record notes the freeze
with the run-id that produced it.

## STOP CONDITION B — not converging (failure)

**Three consecutive rounds at the same top severity, with no reduction in distinct design-defect
findings, means the loop has hit diminishing returns. Stop folding.**

This is not a success condition and must not be dressed as one. It means the method is no longer
paying for itself on this artefact, and continuing would produce growth without convergence. The
response is to change the method — partition the document, move to implementation-and-test, or take
an operator decision — **not** to run another fold.

### This condition has ALREADY fired, on the historical record

| Round | Verdict | Top severity | Findings | Seats at `block` |
|---|---|---|---|---|
| 2 | needs-changes | P1 | 15 | — |
| 3 | needs-changes | P1 | 12 (G-01..G-12) | 1 |
| 4 | needs-changes | P1 | 15 (H-01..H-15) | 1 |
| 5 | needs-changes | P1 | 12 (J-01..J-12) | 2 |

Four consecutive rounds at P1. Finding counts flat (15, 12, 15, 12). Blocking seats *increasing*.
By the criterion above, condition B was met at round 5 — which is the evidence-based case for
partitioning rather than running round 7 as another content round.

**Round 6 is nonetheless worth completing**, for a reason that is specific and not special pleading:
it is the first round carrying *executed* evidence (the §9 pass, which found two checks that five
rounds of reading missed, one of them green only when the implementation is broken). That is a
genuinely new input, not another pass of the same method. If round 6 still returns P1 with blocks,
condition B is confirmed rather than merely met, and no further fold should be run on the monolith.

## What freeze means

- The spec is not re-folded. New findings go to the panel record as post-freeze residuals.
- A **P0 or a P1 reopens it**; a P2 does not. Reopening is recorded with the finding that caused it.
- Freeze is not approval to merge or implement — those remain operator decisions.

## Anti-gaming

The stop conditions are read from the panel's *findings*, never from the orchestrator's summary of
them. Severity is the orchestrator's triage (per the panel calibration rule), so the temptation at
the boundary is to triage a P1 down to P2 and declare convergence. **Do not.** If a round's severity
is genuinely arguable, record it as arguable and take the stricter reading; a document frozen by
relabelling is worse than one that took an extra round, because the label is what every future
reader trusts.
