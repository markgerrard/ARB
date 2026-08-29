# Plan — `diagnose` + `diagnose-steer` skills (TDD; parallel-pair) — v2

> Implements specs `2026-06-19-diagnose-skill-SPEC.md` + `2026-06-19-diagnose-steer-skill-SPEC.md` (v2).
> v2 folds the QUAD plan-panel (codex + agy + cold-Opus + GLM-judge, 4/4 NEEDS-CHANGES). Python stdlib.
> TDD, commit per task. Skills at `skills/diagnose/` + `skills/diagnose-steer/`.

## 0. TWO-GATE ARCHITECTURE (quad P0 — the load-bearing clarification)
There are TWO distinct gates, and the plan must not conflate them:
- **The domain validators (SPLIT per-skill — GLM P1-2: a single shared validator carrying steer logic
  re-centralizes the contamination the two-skill split exists to separate, and edges to the one-flagged-
  engine anti-pattern):**
  - `_diagnose_common/neutral_validators.py` — the NEUTRAL block-reasons applicable to BOTH skills:
    `scope-override, blind-assignment-violation, non-exclusive-predicate, reciprocal-certification,
    scribe-contamination, cross-channel-routing`. It is a pure checker that references **NO steer
    block-reason** (enforced at C0, logic-level — see §2).
  - `skills/diagnose-steer/steer_validators.py` — the STEER-specific block-reasons, in the steer skill
    ONLY: `early-steer-reveal, steer-via-observation, unresolved-citation, no-discriminating-experiment,
    inverted-confidence, pmax-as-input`. `diagnose`'s gate = neutral only; `diagnose-steer`'s = neutral +
    steer. Both consume the shared `run_record` schema (a neutral data shape, common is fine).
- **`bridge-protocol`'s MERGE gate** (already built): certifies that the BUILD of the above validators is
  sound — a hard-signal that `tests/test_diagnose*.py` passed clean at HEAD, the manifest, etc. It does
  NOT evaluate diagnosis contamination (its block-reason set is closed). The skills' merge runs through it
  exactly as bridge-protocol's own build did.

## 1. run_record schema (the operand every hard task references — define it FIRST)
`run_record.json`: `{trigger, extraction_scope, observables:[...], seats:[{seat,model,role:blind|steelman|
alternative|scribe}], predicates:[<predicate record §D3>], dispatch_log_ref, confidence:{...}, steer?:{...}}`.
Committed schema under `skills/_diagnose_common/schemas/`.

## 2. File structure + parallel-pair safety (quad: freeze common API FIRST)
- **PRE-TASK C0 (serial, ONE owner, before either build seat starts):** land `skills/_diagnose_common/`
  skeleton — `collation.py`, `neutral_validators.py` (the NEUTRAL block-reasons + run_record schema —
  NO steer logic), `clock.py` (the tamper-evident monotonic dispatch log), and a FROZEN public API. The
  steer-specific validator does NOT live here — it ships in `skills/diagnose-steer/steer_validators.py`
  (§0). **C0 neutrality guard is LOGIC-level, not import-only (GLM P1-2 — an import check passes while
  `neutral_validators.py` carries steer `if`-branches):** the static assertion is `_diagnose_common`
  imports NO dispatch/steer symbol, AND `neutral_validators.py` references NO steer block-reason string
  (the set `{early-steer-reveal, steer-via-observation, unresolved-citation, no-discriminating-experiment,
  inverted-confidence, pmax-as-input}` appears nowhere in its source), AND `neutral_validators.py` never
  reads the `run_record.steer` field (GLM P2 — a string-only guard would miss a checker that branched on
  steer DATA without emitting a steer reason; the assertion is `"steer"` subscripting/attribute access on a
  run_record appears nowhere in its source). After C0, the two builds are
  parallel with DISJOINT write-sets: diagnose owns `skills/diagnose/**` + `tests/test_diagnose.py`; steer
  owns `skills/diagnose-steer/**` + `tests/test_diagnose_steer.py`; neither edits `_diagnose_common`.
- `skills/diagnose/{SKILL.md, diagnose.py, dispatch.py (OWN, strict subset, NO steer field), extraction.py,
  extraction_scope.json}`; `skills/diagnose-steer/{SKILL.md, diagnose_steer.py, dispatch.py (OWN, +labelled
  steer channel), confidence_constants.json}`.

## 3. THE DOGFOOD MANDATE (quad lead — END-TO-END, not fixture-recognizer)
Every contamination control (D-/S- dogfood) MUST fire from a **real integrated run** — trigger →
`derive_scope` → `extract` (real fs/git) → seats → predicate → the domain validator on the resulting
`run_record` — NOT a unit call to a checker on a hand-built input. A control that blocks a hand-authored
`run_record` without producing it via a real run is REJECTED in review (the exact bridge-protocol
fixture-recognizer fail-open). Unit checks may exist additionally, but each control's PASS requires the
end-to-end path. **Mechanical invariant (not just review-time, cold-Opus N1):** each end-to-end test asserts
the `run_record` it validated carries a `dispatch_log_ref` produced by `_diagnose_common/clock.py` IN that
test — a hand-authored run_record has none, so the fixture-recognizer shortcut fails the assertion.

## Tasks — `diagnose`
### D1 — Deterministic extractor + committed scope (concrete contract; quad)
- [ ] `trigger.json` schema `{failing_test:str, error_log:str}` (v1 supports Python failing-test triggers;
  unsupported trigger → BLOCK, no improvising). `derive_scope(trigger)`: import-closure via **AST** over
  the failing test's local-module deps (terminates at non-local/stdlib); window = traceback lines in
  `error_log`. `extraction_scope.json` schema committed. `scope_matches` = **exact set-equality on
  normalised repo-relative paths**.
- [ ] END-TO-END test: real on-disk git fixture; `extract(derive_scope(trigger))` over real files; a run
  whose observables include an EXTRA real file → `scope-override` BLOCK; the rule-derived twin → pass.
### D2 — Blind generation + deterministic assignment (recorded seed)
- [ ] END-TO-END: seats generate candidates blind from real observations; assignment deterministic by
  fixed rule + a RECORDED SEED (same trigger → same assignment across two runs, asserted); orchestrator
  hypothesis in the blind path → `blind-assignment-violation` BLOCK.
### D3 — Structured exclusive predicate + independent certifier (mechanical vs semantic split)
- [ ] Predicate record: `{predicate_id, author_seat, hypothesis_a, hypothesis_b, observable,
  under_a:{op,value}, under_b:{op,not_value}, certifier:{seat,model,evidence_ref,derivation_ref,
  evidence_category}}`. **`evidence_category` ∈ {`live_data`,`file_line`,`none`} is RESOLVED MECHANICALLY
  from `evidence_ref` (a live-experiment result ref → `live_data`; a `path:line` ref → `file_line`; neither
  → `none`), committed in the record — never a seat-attested free scalar.** It is the sole source of S3's
  `strength_i` (GLM P1-1); a certifier whose stated `evidence_category` doesn't match what `evidence_ref`
  mechanically resolves to → BLOCK.
  MECHANICAL gate checks: `under_b.not_value` present (exclusive FORM — NOT-X on the SAME observable
  symbol); certifier `seat≠author` AND `model≠author_model`; NO reciprocal certification (seat-graph: directed edge `author→certifier` per predicate; ANY cycle → BLOCK). SEMANTIC exclusivity (is X genuinely incompatible with B) = panel-judged,
  attested-not-verified residual. END-TO-END: a non-exclusive predicate (no `not_value` / different
  observable) OR a reciprocal/ same-model certifier → BLOCK; an exclusive independently-certified one → pass.
### D4 — diagnose dispatch (strict subset, no steer) + scribe isolation (static)
- [ ] `diagnose` input schema is a STRICT SUBSET — a steer field anywhere → schema reject (static
  assertion). Scribe runs in clean context: a dispatch passing orchestrator context to the scribe →
  `scribe-contamination` BLOCK (static dispatch-hygiene check). Channels disjoint → cross-channel routing →
  BLOCK.
### D5 — diagnose dogfood (matched pairs, end-to-end; honest subtle residual)
- [ ] For each control a contaminated real-run → BLOCK + clean twin → pass. (f) SUBTLE: block the
  MECHANICALLY-catchable subset (observables diverging from rule-derived scope by selection/ordering) →
  `scope-override`/ordering BLOCK; route un-catchable framing to panel-judgment honestly (attested
  residual — do NOT over-promise a mechanical block). Run the skill's BUILD through bridge-protocol.

## Tasks — `diagnose-steer`
### S1 — Labelled steer channel (own dispatch, disjoint from observation)
- [ ] END-TO-END: steer enters a marked channel; a steer in the observation channel →
  `steer-via-observation` BLOCK; diagnose-steer's dispatch is its OWN (not shared).
### S2 — Staged double-blind + tamper-evident TEMPORAL BARRIER (quad: not a steer-seat-written log)
- [ ] `_diagnose_common/clock.py` provides an append-only `dispatch_log.jsonl`: events
  `{blind_prompt_sent, blind_submission_received, steer_sent, artifact_visible}` with seat/channel id,
  **monotonic sequence number** (the authority — owned by the common lib, NOT seat-written), timestamp,
  artifact hash. Barrier gate uses SEQUENCE first: `seq(steer_sent) > max seq(blind_submission_received)`;
  missing/duplicated/seat-supplied events → BLOCK. END-TO-END: an early steer (steer_sent seq before blind
  submissions) → `early-steer-reveal` BLOCK even with a recorded pre-steer state; correct ordering → pass.
### S3 — Admissible-reason (honest) + corrected confidence (committed constants; quad)
- [ ] Citation must RESOLVE (mechanical) → unresolved → `unresolved-citation` BLOCK; motive-validity
  panel-judged (residual). `confidence_constants.json`: explicit `{P_max:<val <1>, floor:<val >0>,
  weights, strength_by_category:{live_data:<hi>, file_line:<mid>, none:<lo>}}` — **all committed; the
  per-category strengths are gate-constants, never phase_input (GLM P1-1).** `Q = min(1, Σ(weight_i ·
  exclusivity_i · strength_i) / norm)` over **§D3-CERTIFIED predicates ONLY** (uncertified → 0
  contribution), where **`strength_i = strength_by_category[predicate.certifier.evidence_category]`** (the
  D3-committed, mechanically-resolved category — NOT a constant 1.0, which would degenerate Q to a
  count that's strawman-inflatable). **`norm = Σ weight_i` over the SAME contributing certified predicates
  (GLM P2 — a count-NORMALIZING denominator), so Q is a quality-weighted AVERAGE of `exclusivity·strength`
  ∈ (0,1]: adding a weak (`none`) predicate provably LOWERS Q for ALL N, not just at the dogfood's tested
  counts. A fixed/constant `norm` is a BLOCK (it lets Σ grow unboundedly with count → inflation at scale).**
  `confidence = P_max · Q`. END-TO-END assertions: (i) a THOROUGH run
  (more certified exclusive disconfirmations) scores HIGHER than a shallow one (direction — catches the
  inverted formula → `inverted-confidence` regression test); (ii) **certified-strawman control (GLM P1-1):
  MANY (≥10) certified-but-evidence-weak predicates (`evidence_category:none`) score LOWER Q than FEW (≤2)
  evidence-strong ones (`live_data`)** — the high weak-count exercises the `norm`-normalization at scale
  (not just two fixed points); catches count-based Q inflation, which an `uncertified→0` test alone does NOT; (iii) a phase-supplied `P_max` OR `strength_by_category` → `pmax-as-input` BLOCK
  (constants come from the committed file); uncertified shallow checks raise Q by 0.
### S4 — diagnose-steer dogfood (matched pairs, end-to-end) + steered+no-experiment hard block
- [ ] Controls (a)–(f) per spec §6, each end-to-end; steered+converged+no discriminating experiment → FAIL
  regardless of Q. Build through bridge-protocol.

## Self-review
Two gates separated; run_record schema defined first; every hard check has a concrete record + a mechanical
predicate; every control is END-TO-END (no fixture-recognizer); `_diagnose_common` API frozen pre-parallel.
