# Design — `diagnose` skill (read-only tri-model root-cause, contamination-hardened)

**Status:** DESIGN (pre-panel). Skill 2 of 3 (parallel with `diagnose-steer`; both invoke `bridge-protocol`).

## Problem it solves
In a dispatched root-cause panel, the orchestrator seat is also a participant with a prior. If it narrates
its hypothesis into the shared brief, every seat inherits that anchor and their agreement becomes
**shared-anchor convergence masquerading as independent corroboration** — destroying the decorrelation the
panel exists for. The skill's primary job is to make that structurally impossible.

## Design

1. **No orchestrator contamination of panel input.** A *fixed dispatch contract* sits between orchestrator
   and seats; the orchestrator cannot editorialize into the panel's frame — *including inside ostensibly-
   neutral observation fields* where loaded phrasing leaks priors. Candidate mechanisms (panel to weigh on
   merits, bias toward observations from a non-orchestrator source):
   - (a) **deterministic non-LLM extraction** of observables (logs, diffs, stack traces, metrics) into the
     brief — no orchestrator prose at all;
   - (b) a dedicated **neutral-scribe seat** that produces the observation section, which the orchestrator
     may not edit;
   - (c) **template-constrained orchestrator** — fixed fields, validated against an editorializing-phrase
     filter.
   Lean (a)+(b); let the panel argue. The contract must make it so the orchestrator's hypothesis cannot
   reach panel input through any channel.

2. **Hypothesis by assignment, not narration.** A held hypothesis is *assigned* to one seat to steelman;
   another seat steelmans the strongest alternative; a third runs open. Convergence counts only if it
   **survived the assigned adversary**.

3. **Synthesis bar.** A root cause is accepted only with **positive disconfirmation of the alternative +
   a discriminating live experiment** (an observation that would have come out differently if the cause
   were elsewhere) — convergence alone is never sufficient. Flag any seat whose agreement lacked
   independent evidence (file:line + live data).

4. **Read-only.** Output: verified root cause + confidence + the smallest correct fix *direction*. No code
   applied. (Mirrors how the EnginePool/SessionStore root-causes were reached this session: live
   experiment, not vote.)

5. **Dogfood gate (per `bridge-protocol`):** a design that lets the orchestrator reach panel input through
   any channel but the absent-by-design (or, in `diagnose-steer`, the assigned-steer) one must BLOCK at
   merge. The skill must demonstrate, by negative control, that a contaminated brief is rejected.

## Open tensions for the panel (reasoned call wanted)
- **One engine + steer-flag vs two skills** (`diagnose` / `diagnose-steer`). Lean *two separate*: a shared
  engine risks the steer-path's looser frame leaking into the blind path. Panel's call.
- (Carried by `diagnose-steer`, noted here for coherence:) whether the steered-convergence confidence
  penalty is a fixed discount or a function of disconfirmation thoroughness.

## What the panel should hunt (do not pre-answer)
Residual contamination channels the design above does NOT close: e.g. orchestrator choice of *which*
observables to extract (selection bias is a prior); the assignment step itself (who assigns? is the
assignment a steer?); the live-experiment design (can the orchestrator bias it?); whether "neutral scribe"
is itself an orchestrator proxy. Name the leak, rate it, propose the closure.

---
## Panel outcome — 3/3 DESIGN-NEEDS-CHANGES, all changes ADOPTED (Mark, 2026-06-18)
Panels independently + unanimously flagged the load-bearing holes (no steer needed). Adopted into the spec:
- Relocate the three high-bandwidth priors OUT of the orchestrator: observable SELECTION (automated/
  standardized extraction or neutral-scribe queries; orchestrator nomination = declared exception, not
  blind input); hypothesis ASSIGNMENT (seats brainstorm blind from raw observations, then assign
  deterministically — the BLIND path has no "held hypothesis" at all); experiment DESIGN.
- Synthesis bar = PRE-REGISTERED differential predicate (if A→O=X, if B→O=Y, X≠Y) certified by a non-author
  seat BEFORE observing. Neutral scribe runs in clean isolated context (strictly descriptive).
- TWO separate skills (not one engine+flag): diagnose has nowhere to put a steer (fail-closed). Dogfood
  needs matched contaminated-blocks / clean-twin-passes negative controls run by the real gate.
