# Spec — `diagnose-steer` skill (declared-steer variant of `diagnose`)

**Status:** SPEC (pre-panel). Implements `2026-06-18-diagnose-steer-skill-design.md`. Invokes
`bridge-protocol`. Sibling: `diagnose` (separate skill — never one shared flagged engine).

## 0. Goal & shape
The deliberate exception to `diagnose`'s blindness: when the orchestrator holds context the seats cannot
reconstruct (a prior incident, a system-specific fact, a search space too large for a blind panel in
budget), a steer toward it is correct — but as a **declared, attributed, bar-raising** act, never an
ambient leak. Read-only. Lives at `skills/diagnose-steer/`. It does NOT relax `diagnose`'s contamination
boundary; it adds ONE labelled channel and raises the confirmation bar.

## 1. Steer is explicit, attributed, and in its OWN channel
The orchestrator's hypothesis enters as a **labelled** input the seats see AS "orchestrator proposes X;
treat as a lead to test, not a finding." Observation fields stay neutral and non-orchestrator-sourced
exactly as in `diagnose` §1.1/§1.3; the steer lives in a separate marked channel — never phrased as a
neutral observation. A steer that enters through the observation channel → gate BLOCK.

## 2. Staged double-blind (labelling alone doesn't stop anchoring)
A labelled steer can still anchor how seats read neutral observations. So:
- **2.1** The open seat + the alternative-steelman seat first submit a **blind** observation summary,
  initial hypothesis set, and their disconfirmation criteria from neutral input ONLY — *before* the steer
  is revealed to them.
- **2.2** The steer is revealed ONLY to the seat assigned to steelman it (the others stay blind through
  their first submission); the record preserves pre-steer and post-steer states. **The gate enforces the
  BARRIER, not merely the recording (round-1 P1-2):** it asserts `steer_dispatched_at > max(blind_seat
  first-submission timestamps)` — gate-recomputed from the dispatch log, like bridge-protocol's
  `run_after_final_diff`. A run where the steer was dispatched/visible BEFORE the blind submissions →
  BLOCK[early-steer-reveal], even if a (fabricated) pre-steer state was recorded. (Recording-exists is not
  the protection; ordering is.)

## 3. Steer requires a stated, ADMISSIBLE reason (recorded with the diagnosis)
Legitimate reasons: **unreconstructable context** (cite a prior fact / incident id) OR **search-space
scale** (state the space size + budget). **Honest scope of the gate (round-1 P1): the gate checks
citation-EXISTENCE-AND-RESOLUTION (the incident-id resolves; a number is stated), NOT validity** —
"can't-derive" is a counterfactual about seat capability and motive-validity ("am I just confirming
faster?") are **panel-judged**, not mechanically checkable. The spec says this plainly rather than
implying "checkable obligation" closes it: a steer with no reason, or a citation that doesn't RESOLVE →
gate BLOCK (mechanical); whether a resolvable citation is a *genuine* unreconstructable need → panel
judgment (attested-not-verified residual, as `bridge-protocol` §2). Recorded with the root cause so a
wrong steered diagnosis is traceable to its stated justification.

## 4. The steer raises the bar; it cannot pre-empt disconfirmation
Synthesis STILL requires positive disconfirmation of the alternative + a pre-registered discriminating
live experiment (`diagnose` §2). The steer says "look here first," never "conclude this." **A steered
convergence counts as WEAKER** than an unsteered one.
**Confidence form (corrected — round-1 P0: the prior `P_max·(1−D)` was INVERTED, rewarding shallow steered
runs and punishing thoroughness):** `confidence = P_max · Q`, where:
- `P_max < 1` is the **steered ceiling** — a permanent discount vs an unsteered run (the steer happened;
  a steered convergence can NEVER reach the confidence an unsteered one could). This IS the floor-on-the-
  discount: `P_max` is a committed **gate-constant** (not a phase_input the builder can set to ~1), pinned
  to a defensible value and bounded away from 1.
- `Q ∈ (0,1]` is **disconfirmation QUALITY** and RISES with thoroughness (ruling out alternatives raises
  P(cause) by elimination): weighted by the *strength* of each disconfirmed alternative and the
  *exclusivity* of each pre-registered predicate (§diagnose 2) — NOT raw counts (count-based Q is
  strawman-inflatable). `Q` draws ONLY on §2-certified exclusive predicates. **`strength_i` is NOT a free
  scalar and NOT a constant 1.0 (which collapses Q to a count): it is sourced from the predicate's
  committed `evidence_category` (`live_data > file_line > none`, mechanically resolved from the certifier's
  `evidence_ref`), via committed gate-constants `strength_by_category`.** So padding with many
  certified-but-evidence-weak (`none`) predicates can NEVER outscore a few evidence-strong (`live_data`)
  ones.
So more, stronger disconfirmation → higher `Q` → higher confidence, up to the `P_max` ceiling. The steer
buys "look here first," and thoroughness earns confidence back toward the ceiling but never past it.
**The ceiling/quality math NEVER waives the hard gate:** steered + converged + no discriminating
experiment = FAIL regardless of `Q` (a separate BLOCK, not a confidence adjustment).

## 5. Assignment & output
Steered hypothesis → one steelman seat; the strongest alternative → another; third open (§diagnose 3).
`diagnosis.json` as in `diagnose` plus: `steered:true`, `steer_reason:{type, cited_fact|space+budget}`,
`pre_steer_state`, `post_steer_state`, `confidence_penalty:{P_max, D, floor}`.

## 6. Dogfood gates (via `bridge-protocol`; matched negative controls)
Build BLOCKs at merge if: (a) a steer whose citation doesn't RESOLVE passes (motive-validity is panel-
judged, §3); (b) a steered convergence with no discriminating experiment passes (the §4 hard block);
(c) a steer entering via the observation channel isn't blocked; (d) **early-steer-reveal** — the steer was
dispatched/visible before the blind seats' first submissions (`steer_dispatched_at ≤ max(blind submission
ts)`) isn't blocked, even WITH a recorded pre-steer state (§2.2 — the barrier, not the recording);
(e) `P_max` (the steered ceiling), the quality floor, OR `strength_by_category` is a builder-supplied
`phase_input` rather than a committed gate-constant, OR `Q` is inflatable by count — including the
**certified-strawman case: many certified-but-evidence-weak (`none`) predicates outscoring few
evidence-strong (`live_data`) ones must NOT pass** (an `uncertified→0` test alone misses this) — §4;
(f) the corrected confidence direction regresses (more disconfirmation → lower confidence). Each with a
matched blocks/clean-passes pair.

## 7. Open for the spec panel (do not pre-answer)
Is "all seats but the steelman stay blind through first submission" actually enforceable, or can the steer
leak via the shared dispatch? Are `w1,w2,floor` defensible or arbitrary (and should `D` be a function of
disconfirmation *quality*, not just count)? Is the "admissible reason" check real, or will any plausible
citation pass? Is the two-skills separation airtight, or does the shared lib re-introduce the leak?
