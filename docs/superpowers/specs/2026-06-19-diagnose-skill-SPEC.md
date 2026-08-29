# Spec — `diagnose` skill (read-only tri-model root-cause, contamination-hardened)

**Status:** SPEC (pre-panel). Implements the adopted design (`2026-06-18-diagnose-skill-design.md`).
Invokes `bridge-protocol` for its build/gate contract. Sibling: `diagnose-steer` (separate skill).

## 0. Goal & shape
A SKILL.md + a thin orchestration script that runs a 3-seat root-cause panel where the orchestrator is
**structurally unable to contaminate panel input**. Output: verified root cause + confidence + smallest
fix *direction*. **Read-only — no code applied.** Lives at `skills/diagnose/`.

## 1. The contamination boundary (the whole point — relocate the 3 high-bandwidth priors out of the orchestrator)
The design panel's load-bearing finding: blocking orchestrator *prose* isn't enough; **observable
selection, hypothesis assignment, and experiment design** are higher-bandwidth priors and must leave the
orchestrator.
- **1.1 Observable selection is NOT the orchestrator's — and the SCOPE that defines selection is a
  committed, run-independent artifact (round-1 P0: deterministic extraction over an orchestrator-authored
  source list is just contamination laundered through "deterministic").** The source list + time-window +
  scribe scope live in a committed `extraction_scope.json` (the diagnose analogue of bridge-protocol's
  `stage_modes.json`), DERIVED FROM THE TRIGGER BY A FIXED RULE (e.g. "all files in the failing test's
  import-closure; the window = [first-error − N, last-error]"), not hand-picked per run. The gate
  **recomputes** the scope from the trigger and BLOCKs if the run's observables don't match the
  rule-derived set (`block_reason: scope-override`). A per-run orchestrator nomination of a specific
  observable is a **declared exception** → the `diagnose` run is INVALID (use `diagnose-steer`). The
  deterministic extractor itself is a named artifact (Task: a script over the scope's source formats),
  so "deterministic extraction" cannot hide a selection prior.
- **1.2 No held hypothesis in the blind path.** The blind path has NO orchestrator hypothesis to assign.
  Seats generate candidate hypotheses **blind from the raw observations**, then assignment is
  **deterministic** (e.g. each seat steelmans a different top-candidate by a fixed rule / lot), never
  orchestrator-chosen. (A "held hypothesis to assign" belongs only to `diagnose-steer`.)
- **1.3 Neutral scribe runs in a CLEAN isolated context** (no orchestrator prompt/thoughts/history); its
  system prompt is strictly descriptive — extract observables, NO synthesis/evaluation. A scribe that saw
  orchestrator context is an orchestrator proxy → its output is rejected.

## 2. The synthesis bar (pre-registered differential predicate — not convergence)
A root cause is accepted ONLY with **positive disconfirmation of the alternative + a discriminating live
experiment**, never convergence alone. "Discriminating" is made non-theatre by **pre-registration of a
MUTUALLY-EXCLUSIVE predicate** (round-1 P0: `X != Y` states different *predictions*, not *exclusive* ones —
if A→X and B→(X or Y), observing X doesn't disconfirm B, yet `X≠Y` certifies; that's theatre). Before
observing, the panel records `{under A: observable = X AND under B: observable = NOT-X}` — the observable
that would FALSIFY each hypothesis. A **certifying seat** attests to **exclusivity** (not just `X≠Y`) and
must **independently derive** the discriminating observable, recorded with its own evidence. The certifier
is **decorrelated**: model ≠ the predicate's author, and **no reciprocal certification** within a run (A
certifies B while B certifies A → BLOCK; gate-checkable from the seat graph). Only then is the experiment
run + compared. A finding without a pre-registered exclusive, independently-certified predicate is not
synthesis-eligible. Flag any seat whose agreement lacked independent evidence (file:line + live data).

## 3. Assignment & seats
Three seats, decorrelated (distinct models; bridge engines + native). One steelmans the strongest
candidate, one steelmans the strongest alternative, one runs open. Independent phase: seats cannot read
each other's work until all submit (write outside the repo-under-diagnosis / return-as-reply). Convergence
counts ONLY if it survived the assigned adversary.

## 4. Output (read-only)
`diagnosis.json`: `{root_cause, confidence, fix_direction, differential_predicate:{A,X,B,Y,certified_by},
experiment_result, disconfirmed_alternatives:[...], seats:[{seat, verdict, evidence:[file_line|live_data]}],
flags:[seats-with-unsupported-agreement]}`. No code is written or applied.

## 5. Dogfood gates (via `bridge-protocol`; negative controls required)
The skill's build BLOCKs at merge if: (a) a path exists by which the orchestrator reaches panel input
other than the declared-exception channel — proven by a **contaminated-brief control the gate REJECTS** +
clean twin; (b) a "synthesis" with no pre-registered EXCLUSIVE predicate is accepted; (c) the neutral
scribe can be constructed with orchestrator context (a STATIC dispatch-hygiene check: dispatch passes
orchestrator context to the scribe → BLOCK); (d) assignment can be orchestrator-chosen in the blind path;
(e) **scope-override** — observables not matching the rule-derived `extraction_scope.json` aren't blocked;
(f) **SUBTLE contamination** (round-1 P1, the load-bearing control) — a *syntactically neutral* brief that
steers via SELECTION / ORDERING / emphasis of true observables (same observable set, no loaded phrase)
passes while a neutral twin also passes. The crude-only suite is the worst outcome (green while inert on
the channel that matters). For what the gate CAN'T mechanically catch (subtle framing beyond scope/
ordering), the spec routes the residual to **panel-judgment honestly** (an attested-not-verified residual,
exactly as `bridge-protocol` §2 does) rather than ship a silently-green gate. Each gate = a matched
blocks/clean-passes pair; (e)+(f) are REQUIRED, not optional.

## 6. Relationship to `diagnose-steer` (TWO skills, never one flagged engine)
`diagnose` has **no input surface for a steer** — by construction it cannot be steered (fail-closed input
minimization); a dogfood asserts `diagnose`'s input schema is a STRICT SUBSET with no steer channel.
Steering is the separate `diagnose-steer` skill. **DISPATCH is NOT shared (round-1 P1-4): dispatch is the
channel contamination travels through, so it sits ABOVE the shared line — per-skill dispatch.** Genuinely
neutral mechanics (artifact capture, collation) live in the common lib, and so MAY the **neutral domain
validator** (`_diagnose_common/neutral_validators.py` — the blind/scope/exclusivity/scribe/cross-channel
block-reasons) **as a pure checker, BUT ONLY under a logic-level neutrality guard: it must reference NO
steer block-reason whatsoever** (`early-steer-reveal, steer-via-observation, unresolved-citation,
no-discriminating-experiment, inverted-confidence, pmax-as-input` appear nowhere in its source — a
build-time static assertion, not merely an import check). The **steer-specific validator lives in
`diagnose-steer` ALONE** — a single shared validator carrying both blind and steered logic re-centralizes
the contamination the two-skill split exists to separate (the one-flagged-engine anti-pattern). The blind /
steered / scribe channels are DISJOINT with a dogfood that cross-channel routing (e.g. seat A's pre-submit
artifact reachable by seat B, or the steer reaching a blind seat) → BLOCK. "Below the validators" means
"below the contamination boundary" — and the shared neutral checker is admissible precisely because the
guard proves it carries none of that boundary's steer logic.

## 7. Open for the spec panel (do not pre-answer)
Is deterministic non-LLM extraction *sufficient* for real diagnoses, or does it miss observables a smart
scribe would find (and does the scribe re-introduce a selection prior)? Is "non-author certifies X!=Y"
gameable (collusion / a weak certifier)? Can the contaminated-brief negative control be made truly
representative, or will it only catch crude contamination and miss subtle framing?
