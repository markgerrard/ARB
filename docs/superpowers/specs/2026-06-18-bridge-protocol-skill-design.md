# Design — `bridge-protocol` skill (meta-skill: the build pipeline as a declared contract)

**Status:** DESIGN (pre-panel). Skill 1 of 3 (foundation; `diagnose` + `diagnose-steer` invoke it).

## Purpose
Codify the standard ARB build workflow as an invokable skill, so the pipeline is a *declared contract*
rather than convention. The pipeline:

    design → panel → spec → panel → plan → panel → codex-subagent TDD → tri-panel review →
    tri-panel merge gate → verified merge

## What the skill owns
1. **Gate contracts.**
   - *Review panel return shape:* each reviewer returns a one-line VERDICT from a fixed enum
     (SHIP / SHIP_WITH_NITS / FIX_BEFORE_MERGE / BLOCK_MERGE for code; PLAN/SPEC/DESIGN-READY vs
     -NEEDS-CHANGES for artifacts) + P0/P1/P2 findings with file:line + a concrete fix. Independent phase:
     reviewers cannot read each other's reports (write outside the repo-under-review until all land).
   - *Merge gate BLOCK conditions:* the gate MUST block on (a) any unresolved P0/P1; (b) a missing hard
     signal (see §2); (c) a **cheap-fake violation** (see §3); (d) an escaped-defect obligation not
     discharged (see §4). The gate is fail-closed: absence of a green signal is a block, not a pass.
2. **Verified-vs-judged exit rule.** No step emits `success` without a **hard signal**: a passing test
   suite, a landed commit (SHA verified from git), or a green live check. A model/worker *judging* the
   work done is `partial` until a hard signal confirms it. (This is the warm-seat "verify from git, not
   reply prose" rule, generalized to every gate.)
3. **Escaped-defect obligation.** Whenever **execution catches a defect that review missed**, the closing
   step MUST produce: a CHANGELOG entry (what + why), a defect-corpus row (disagreement-corpus or the
   escaped-defect journal as appropriate), and — if it generalizes — a standing-rule update. Skipping this
   is a gate failure.
4. **The cheap-fake standing rule (first-class gate requirement — the central lesson).**
   *A fake (mock, stub, fixture, pooled token) that is cheaper or simpler than the real component along
   the dimension that matters — latency, state, interface completeness — certifies behaviour the real
   component does not have.* Four escaped defects motivated it: (i) a mock SessionStore advertising methods
   the real store lacked; (ii) a mock deposit path hiding a data-loss window; (iii) a fake engine with an
   instant `start()` hiding a real ~30-90s startup; (iv) an EnginePool that only tolerated synchronous
   engine-start because every prior engine's start was sub-second.
   **Rule:** any pooled / adapter / wrapper / engine layer requires a **real-component integration test
   that preserves the costly dimension** (real latency, real state, real interface), not merely the
   interface. **The merge gate MUST BLOCK a design that tests such a layer only against a cheap fake.**
   This rule is the **dogfood test for `bridge-protocol` itself**: its own build must satisfy it.

## Shape (how it's invoked)
A SKILL.md under `skills/bridge-protocol/` describing the pipeline + the gate contracts as checkable
clauses, plus a machine-checkable gate spec (the BLOCK conditions expressed so a merge-gate step can
evaluate them, not just prose). `diagnose` and `diagnose-steer` invoke this skill rather than
re-specifying the pipeline.

## Dogfood / acceptance
- The skill's *own* build runs through the pipeline it declares.
- A test/fixture that fakes a pooled/adapter/wrapper/engine layer along a load-bearing dimension is
  BLOCKED by the gate the skill defines (demonstrated on a deliberately-cheap-fake example → gate fires).
- Verified-vs-judged: a step with no hard signal reports `partial`, never `success` (demonstrated).

## Open tensions for the panel (reasoned call wanted, not a default)
- How much of the merge gate is *machine-checkable* vs orchestrator-judgment? (A purely-prose gate is
  itself a cheap fake of a gate — it certifies "we have a gate" without enforcement. Lean toward at least
  the cheap-fake and verified-vs-judged conditions being mechanically checkable.)
- Granularity of the cheap-fake rule: how does the gate *detect* a cheap-fake violation without false-
  positives on legitimate unit-level mocks (the rule targets pooled/adapter/wrapper/engine layers, not all
  mocking)? What's the precise trigger?

---
## Panel outcome — 3/3 DESIGN-NEEDS-CHANGES, all changes ADOPTED (Mark, 2026-06-18)
Panels independently + unanimously flagged the load-bearing holes (no steer needed). Adopted into the spec:
- Cheap-fake rule REFRAMED: "the fake must not be cheaper than the real component on the load-bearing
  dimension" (a slow-start fake is valid; an instant fake / more-complete-than-real interface is the
  violation). Original "use the real component" was wrong — contradicts the slow-fake EnginePool fix.
- Mechanize via a `load_bearing_components` MANIFEST (check-doc-drift-style registry); gate blocks on a
  targeted layer with no entry / no dimension-preserving test. All gate I/O = structured schema, not prose.
- Verified-vs-judged: TWO signal classes, but the declarative gap stays VISIBLE — declarative steps get a
  WEAKER signal (artifact-landed SHA + panel consensus) + explicit "correctness rests on panel judgment";
  NOT a second hard-signal that papers over the absence of one (Mark's ruling).
- Dogfood needs negative controls run by the real gate entrypoint; gate non-bypassable.
- SPEC-PANEL ADDED CHECK (Mark): the manifest's `costly_dimension` must not be gameable (declaring a cheap
  dimension to dodge the real one) — the next-level evasion.
