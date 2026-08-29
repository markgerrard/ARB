# Design — `diagnose-steer` skill (declared-steer variant of `diagnose`)

**Status:** DESIGN (pre-panel). Skill 3 of 3 (parallel with `diagnose`; both invoke `bridge-protocol`).

## Problem it solves
`diagnose` makes the orchestrator blind by design. But sometimes the orchestrator holds context the seats
genuinely cannot reconstruct — a prior incident, a system-specific fact, a search space too large for a
blind panel to cover in budget — and steering toward it is correct. `diagnose-steer` does **not** relax
`diagnose`; it makes orchestrator influence a **declared, attributed, bar-raising** act instead of an
ambient leak. A visible, on-record steer is a fundamentally different object from a hypothesis woven
invisibly into observation fields.

## Design

1. **Steer is explicit and attributed, never ambient.** The orchestrator's hypothesis enters as a
   *labelled* input the seats see AS a hypothesis from the orchestrator — "warm-Opus proposes X; treat as
   a lead to test, not a finding." Never phrased as a neutral observation. Observation fields stay neutral
   and non-orchestrator-sourced exactly as in `diagnose`; the steer lives in its **own marked channel**.

2. **Steer requires a stated reason, recorded with the diagnosis.** Legitimate: unreconstructable context;
   search-space scale. **Illegitimate (the thing to guard against): "I'm fairly sure and want it confirmed
   faster"** — exactly the case where the blind panel is most needed. The reason is recorded with the root
   cause, so a steered diagnosis that proves wrong is traceable to its stated justification.

3. **Steer raises the confirmation bar; it cannot pre-empt disconfirmation.** Synthesis STILL requires
   positive disconfirmation of the alternative + a discriminating live experiment. The steer says "look
   here first," never "conclude this." A steered convergence counts as **weaker** than an unsteered one —
   the prior is doing work that must be paid back. The gate encodes that asymmetry: **steered + converged +
   no disconfirming experiment = FAIL.**

4. **Assignment still applies:** steered hypothesis → one steelman seat; the strongest alternative →
   another; third open.

5. **Read-only.** Output: verified root cause + confidence + smallest fix direction + the recorded
   steer-reason + an explicit note that the diagnosis was steered.

6. **Dogfood gates (per `bridge-protocol`):** (a) a steer with no stated reason must FAIL the gate; (b) a
   steered convergence with no disconfirming experiment must FAIL the gate; (c) a steer entering through
   the observation channel rather than the labelled channel must BLOCK at merge.

## Open tension for the panel (reasoned call wanted)
- **Confidence penalty on steered convergence: fixed discount vs function of disconfirmation thoroughness.**
  A fixed discount is simple but crude; a function ("the more thoroughly the alternative was disconfirmed,
  the more the steer penalty is bought back") is principled but needs a defensible form. Panel's call, with
  the reasoning.

## What the panel should hunt (do not pre-answer)
Whether the "labelled channel" truly prevents the steer's framing from leaking into how seats read the
neutral observations (a labelled steer can still anchor); whether "stated reason" is auditable or just a
box-tick (what makes a reason legitimate vs the guarded-against "I'm sure, confirm faster"); whether the
weaker-convergence accounting is gameable; the relationship to `diagnose` (is the shared-engine leak risk
real enough to mandate two skills?).

---
## Panel outcome — 3/3 DESIGN-NEEDS-CHANGES, all changes ADOPTED (Mark, 2026-06-18)
Panels independently + unanimously flagged the load-bearing holes (no steer needed). Adopted into the spec:
- STAGED double-blind steer: open+alternative seats submit blind hypotheses + disconfirmation criteria
  BEFORE the steer is revealed; steer sent ONLY to the seat steelmanning it; pre/post-steer states
  preserved (proves the steer's effect, not just labels it).
- "Stated reason" needs admissibility criteria (cite the unreconstructable fact / state search-space
  size+budget), reviewer-challengeable; generic "confirm faster" rejected.
- Confidence penalty = P_max·(1−D), D from #alternatives-disconfirmed + #discriminating-experiments,
  FLOORED >0 (the prior happened); D only moves confidence ABOVE the hard gate — never waives the
  disconfirming-experiment block. TWO separate skills (shared mechanics via bridge-protocol only).
