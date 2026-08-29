# Spec — `bridge-protocol` skill (executable, enforceable build pipeline) — v4

**Status:** SPEC v3 (round-2 panel folded: codex + cold-Opus + GLM-judge, 3/3 NEEDS-CHANGES; agy timed
out). Central addition: **correctness-basis transitions** as a first-class concept (unifies the two P0s).

## 0. What it IS
`bridge-protocol` is an executable orchestration: `bridge-protocol(target=X)` drives
`design→panel→spec→panel→plan→panel→TDD→tri-review→merge-gate` as a workflow. Its review/gate stages are
diagnosis problems, so once built, **`diagnose`/`diagnose-steer` are the engines it invokes there**. It
builds all three skills — including itself — but **never certifies itself** (§1.4). Lives at
`skills/bridge-protocol/` (`SKILL.md` + a runnable `gate/`). All gate I/O is structured JSON.

## 1. Correctness-basis transitions (THE spine — every gate defines basis + what happens when it moves)
Every `phase_record` carries a `correctness_basis ∈ {manual-panel, diagnose, diagnose-steer,
external-base-case, hard-signal}` — *how this phase's correctness is established*. The bases are ordered by
strength (`manual-panel < diagnose/diagnose-steer < hard-signal`; `external-base-case` is the pinned root).

**1.1 The transition rule (the missing abstraction).** A phase-record is valid only under the basis
*available when it was produced*. When a **stronger basis becomes available** for a stage (a validator is
merged+verified, or a declarative artifact gains an executable verifier), **every existing phase-record for
that stage still carrying the weaker basis is INVALIDATED and must be re-evaluated before merge.** The gate
records `basis_available_at_merge` (recomputed) and BLOCKs if `phase_record.correctness_basis` is weaker
than what is now available (`block_reason: stale-correctness-basis`). This is the dual of the static
circular-dep check: (f-static) a design *references* a not-yet-merged validator → BLOCK; (f-dynamic) a
phase-record was panel-judged but its validator is now executable and it wasn't upgraded → BLOCK.
**`basis_available_at_merge` is a PURE FUNCTION of two named artifacts + HEAD (round-3 P1-1):**
`validator_readiness.json` (gate-PRODUCED — a pure function of prior `gate_result`s + git presence: is
`diagnose`/`diagnose-steer` merged AND verified) and `stage_modes.json` (COMMITTED — each stage's mode
∈ blind|steered|external and its eligible validators). Without these the rule is un-implementable; with
them two builders compute the same basis.
**Invalidation is BIDIRECTIONAL (round-3 P1-2):** §1.1 invalidates on *upgrade* (weaker→stronger
available); it MUST ALSO invalidate on *downgrade* — when a validator's own verification transitions to
`invalidated` (§6, e.g. an escaped defect found IN diagnose), every phase-record resting on that basis is
re-invalidated and must downgrade/re-evaluate. Staleness is caught both directions, not just upward.

**1.2 Bootstrap = runtime fallback ONLY.** "Use whatever validator exists now" governs a *running
instance* mid-build; it is **never** an artifact that passes merge. A merged phase-record must satisfy 1.1
against the basis available *at merge time*, not at production time.

**1.3 Per-validator, per-mode readiness.** `diagnose` must be merged+verified before any **blind** stage
may invoke it; `diagnose-steer` merged+verified before any **steered** stage may. Until each is ready, that
mode uses the manual base case. A plan upgrading a steered stage to `diagnose-steer` before it is verified
→ `block_reason: circular-validator-dependency` (this is why naming only `diagnose` was incomplete).

**1.4 Self-certification base case (breaks the regress by construction).** bridge-protocol's OWN
phase-records carry `correctness_basis: external-base-case` — certified by a **manually-trusted tri-panel
pinned at a specific SHA**, NEVER by a running bridge-protocol instance. The gate REJECTS any phase-record
where `reviewer_seat == skill-under-review` (`block_reason: self-certification`).

**1.4a Author quorum (NORMATIVE — the author is contributor-admissible, certifier-inadmissible).** The
seat that BUILT the artifact under review (the `change_author`) may be ON the review panel as a
**contributor**: it supplies implementor context and may RAISE blocks — a self-flagged defect from the
implementor is high-value and must never be suppressed. But its agreement is **non-certifying**: it may
NOT be counted toward the quorum that clears the gate on its own work, and **a converged pass must hold
WITHOUT the author seat's vote** (decorrelation is measured on the non-author seats; the author's agreement
is, at most, a tiebreak-toward-caution). Same shape as M3 as a heard-but-non-quorum judgment seat.
*Gate-enforcement status (honest residual): NOT YET mechanically enforced for phase-record reviewer_reports
— the gate currently enforces only `reviewer_seat==skill-under-review` (§1.4) and `change_author ∉
certifying_seats` for ROOT ROTATIONS (§1.6), not author-non-quorum on ordinary reviews.* **Work item
(v-next):** the gate must read the `change_author` of a phase-record and BLOCK any record whose certifying
convergence DEPENDS on the author seat (i.e. re-evaluate the verdict with the author's vote removed; if it
no longer clears, BLOCK). Until then, the orchestrator enforces it manually (exclude the author from the
certifying set, OR include it as a contributor and verify by hand that the non-author seats converge).

**1.5 The trust root, named honestly.** Correctness ultimately rests on ONE human-certified base case at a
declared SHA. That root is **judged-by-human and cannot be gate-closed** — changing it is a deliberate
out-of-band act (§1.6). The spec states this plainly: it is *not* turtles all the way down; there is
exactly one named, human-judged root, and verified-vs-judged applies to it like everything else (it is
`judged`, not `verified`). Every other basis chains to it.

**1.6 The root is COUPLED to the object it certifies (round-3 P0 — the staleness mechanism can't reach the
top-of-order root).** §1.1 invalidates a record only when a *stronger* basis appears; `external-base-case`
is the top of the order, so §1.1 structurally **never** re-examines the root — yet the gate/SKILL.md it
certifies is a normal production artifact that drifts in-band. To stop the root silently certifying a gate
that no longer exists: the `trust_root.json` record pins `certified_object_sha` — a **tree hash over the gate's immutable
LOGIC+CONTRACT set: `{gate.py, gate/schemas/*, SKILL.md}`** — with an **explicit, named carve-out** of the
mutable contract-DATA the gate operates *on*: `trust_root.json` (self-reference — including it has no hash
fixpoint), `layer_registry.json`, `validator_readiness.json`, `stage_modes.json`, `gate/results/*`, and any
`*_result.json`. The carve-out is named and reviewed, NOT a silent narrowing: a change to the LOGIC set IS
a root-rotation event (the gate the root certifies actually changed); a change to the DATA is normal work
the gate itself gates (a `layer_registry.excluded_roots` edit is load-bearing per §3; a manifest change per
§2) — it is an *input* to the gate, not the gate. The running gate **gate-recomputes** `running_gate_sha`
over the identical LOGIC set; **BLOCKs on drift** (`stale-trust-root` when `running_gate_sha !=
certified_object_sha`). Dogfood asserts a change to ANY logic-set file — `gate.py`, a schema, OR
`SKILL.md` alone — trips `stale-trust-root`, while a normal `layer_registry.json` data change does NOT
(§7-12). **Rotation contract:**
advancing the root requires a NEW `trust_root.json` (`old_sha, new_sha, reason, certifying independent
seats, human_approver, invalidated_basis_records[]`) produced by a **fresh** human tri-panel at the new
SHA — "fresh" pinned as: all certifying seats distinct from the prior root's panel AND independent of the
change's author; the gate BLOCKs a root advance whose record still cites the prior SHA's panel or reuses a
prior seat (no rubber-stamping).
Root rotation is itself a basis transition (§1.1) that re-invalidates dependents.

## 2. Load-bearing manifest (cheap-fake rule) — gate enforces PRESENCE, panel judges CORRECTNESS (stated honestly)
`load_bearing_components.json` per entry: `{component, layer, costly_dimensions:[...non-empty],
dimensions_considered_and_excluded:[{dimension, why_not_load_bearing, waiver_reviewer, waiver_finding_id,
waiver_status:approved|rejected}], production_component, dimension_preserving_tests:{<dim>:<test>},
dimension_evidence:{<dim>:<how>}, fake_tests_allowed_for:[...]}`.
**Cheap-fake rule:** a fake must not be cheaper than the real component on ANY declared load-bearing
dimension (slow-start fake = valid; instant fake / over-complete mock = violation).
**Completeness (mechanical):** a registry-required dimension (§3) absent from both `costly_dimensions` AND
an *approved* exclusion → BLOCK. **Honest scope of the claim (round-2 P1-1):** the gate enforces that the
exclusion FIELD is present AND **resolves `waiver_finding_id` to a real panel finding genuinely approved by
`waiver_reviewer`** (existence/authenticity is gate-checkable; round-3 P2 — resolve the record, don't just
validate well-formedness). It does NOT verify the waiver is *correct* — exclusion correctness is
**panel-judged**, and the spec says so rather than implying the gate closes it. An approved waiver of a registry-required dimension SHOULD additionally carry a corpus
row / repro (§7-routed) so the "non-load-bearing" claim is testable, not merely writable.

## 3. Layer registry — BIDIRECTIONAL fail-closed (round-2 P1-2)
`gate/layer_registry.json`: committed, versioned, owned. **"Production" is the DEFAULT, not a whitelist
(round-3 P0; confirm-round: flip to exclusion-default to kill hiding-by-omission):** EVERY changed file is
production UNLESS it matches a committed `excluded_roots` allowlist (e.g. `["tests/**","docs/**",
"*.md"]`). **Any production (non-excluded) file that resolves to no layer → BLOCK[unclassified].** There is
no inclusion whitelist to hide outside of — a production module in a brand-new top-level dir is production
by default and must resolve to a layer or block. `excluded_roots` is the single visible reviewed boundary;
*adding* to it (carving something out of production) is the reviewed edit, and the gate treats an
`excluded_roots` change as itself a load-bearing change requiring panel sign-off. The gate
computes changed files from `git diff --diff-filter=ACMR $(git merge-base <target-branch> HEAD)...HEAD`.
This closes BOTH the in-tree evasion (`lib/engines.py` resolving to no layer) AND the move-it-outside
evasion (new top-level dir) — the same move as the costly-dimension dodge, one layer down. **Meta-control
(round-3):** the gate rejects a registry that lacks a catch-all OR under which any path in any inclusion
root resolves to nothing ("degenerate/empty" is too weak a check). Residual named honestly: a
dynamically-generated module never in the diff is invisible to a git-diff gate — pinned, not hand-waved.
Symbol-extraction fallback: classify at file granularity, erring toward requiring a manifest entry.

## 4. Structured gate I/O — SPLIT input vs gate-produced (round-2; codex)
- `phase_input.json` (BUILDER-supplied): `{phase, phase_class:executable|declarative, artifact_sha,
  correctness_basis, reviewer_reports:[{seat,verdict,findings:[{id,severity,file_line,fix,status}],
  certified_components:[...]}], hard_signal_evidence:{command,cwd,test_count,check_id,commit_sha,
  captured_output_path}|null, manifest_ref, escaped_defect:{triggered,changelog,corpus_row,standing_rule,
  state:open|fixed|invalidated}}`.
- `gate_result.json` (GATE-produced ONLY): `{gate_decision:pass|block, block_reasons:[], derived_head,
  derived_basis_available, run_after_final_diff, schema_valid, verified:bool, judged:bool}`. The gate
  IGNORES/REJECTS any builder-supplied `gate_decision`/`block_reasons`.
**Ground truth the gate RECOMPUTES (never trusts):** `git rev-parse HEAD`; `hard_signal.commit_sha==HEAD`;
clean tree / no tracked change after run; `run_after_final_diff` (derived); `basis_available` (§1.1).
**Honestly-named residual (round-2 P1-3):** the gate confirms `command` *references* the manifest's
`dimension_preserving_tests` and that `commit_sha==HEAD`, but it does NOT re-execute the command — so
`captured_output_path`/`exit_code` are **attested** (NOT recomputed; "spot-checked" dropped as an
undefined mechanism per round-3 — the residual risk is pinned by dogfood §7-7, not by an unstated control).
The spec says this plainly; it does not over-claim "never trusts builder."
Declarative phases: `correctness_basis∈{manual-panel,diagnose,diagnose-steer,external-base-case}`,
`verified:false, judged:true`; gate BLOCKs a declarative phase asserting `verified:true`.

**4a. Orchestrator-narrated doneness is NOT a gate input (NORMATIVE — verified-vs-judged applied to the
orchestrator's own language).** The orchestrator may not assert a stage is done/verified/merge-ready;
only the hard signal at the SHA may. *Already gate-enforced:* `gate_decision, block_reasons, verified,
judged, hard_signal` are FORBIDDEN phase_input fields (`BLOCK_BUILDER_DECISION`); declarative-`verified:true`
blocks; executable hard-signals are recomputed from git. So a narrated "reviewed enough → pass" cannot
reach a gate decision directly. *Honest residual (NOT yet gate-enforced — work item, same shape as §1.4a):*
the gate trusts the orchestrator's TRANSCRIPTION of `reviewer_reports` (seat verdicts + findings) without
authenticating them against the seats' actual outputs — so doneness can still be laundered via fabricated
panel verdicts. v-next: the gate must tie each `reviewer_report` to the seat's real signed output (content
hash / artifact ref it resolves) and reject a record whose verdicts can't be authenticated.
*Origin (the discipline this clause encodes):* the orchestrator narrating a conclusion AHEAD of the
verifying signal — instances "confirmatory third seat," "dispatching the fix," "reviewed exhaustively,
won't re-review" (each benign only because the signal then agreed). The rule: justify a pass by "this SHA's
gate is green," never by review count or narration.

**4b. A spec with a LOAD-BEARING open does NOT pass spec-panel (NORMATIVE — verified-vs-judged applied to
specs).** An "open question" on a load-bearing axis (a safety/correctness/contamination property, an
execution blast-radius, a determinism basis the gate must recompute against) is an UNVERIFIED PREMISE;
shipping the spec with it open is the spec-level equivalent of emitting success without a hard signal. A
spec-panel reviewer that finds a load-bearing axis parked in an "open questions" section returns
SPEC-NEEDS-CHANGES — the axis must be RESOLVED in the spec (a stated position + its failure mode), not
deferred. *Legitimately deferrable* to the plan: a MECHANISM whose choice depends on empirical facts not
yet in hand — but only when (i) the REQUIREMENT it must meet is fixed in the spec as a hard must-have, (ii)
a FAIL-CLOSED default is stated for "no mechanism meets it," and (iii) the deferral carries a *pass bar*
(named acceptance criteria the plan's spike must clear). Mechanism-pending-spike-with-a-bar is a plan
decision; a load-bearing unknown smuggled past the gate as "open" is not. *Honest residual (NOT yet
mechanized — work item):* this is a spec-PANEL discipline (the panel must flag load-bearing opens); the
merge gate operates on builds, so it cannot itself enforce it. v-next: a spec-panel checklist item / lint
that flags an "open"/"TBD" sitting on a declared load-bearing axis. *Origin:* the diagnose-live-panel
spec parked its execution-sandbox containment and recompute-determinism in §10 "open"; the spec-panel (3/3)
returned NEEDS-CHANGES precisely because those were load-bearing, and the fix was to resolve them in-spec
(requirements + fail-closed default) while deferring only the sandbox MECHANISM to a spike with a pass bar.

## 5. Merge-gate BLOCK conditions (fail-closed; each maps to a gate-read/recomputed field)
(a) any P0/P1 finding `status=open`; (b) executable phase `hard_signal=null` or `commit_sha≠HEAD`;
(c) cheap-fake violation / missing required dimension (§2,§3); (d) unclassified production symbol — either
direction (§3); (e) declarative phase asserting `verified:true`; (f-static) references a not-yet-merged
validator; (f-dynamic) `stale-correctness-basis` (§1.1); (g) per-mode readiness violated (§1.3);
(h) `self-certification` (reviewer_seat==skill-under-review, §1.4); (i) builder-supplied
`gate_decision`/`block_reasons` present; (j) undischarged escaped-defect obligation (state not in
{fixed,invalidated}); (k) `stale-trust-root` (`running_gate_sha != certified_object_sha`, or a root advance
without a complete fresh-panel rotation record, §1.6); (l) `stale-correctness-basis` downward (a record
resting on a now-`invalidated` validator, §1.1).

## 6. Escaped-defect obligation
Execution catches a defect review/tests missed → CHANGELOG + corpus row (disagreement-corpus if
seat-vs-seat; escaped-defect-journal if whole-panel+suite miss; both → journal+cross-link) + standing-rule
if it generalizes. Terminal states: `fixed | invalidated` (round-2 P2-2: a flaky-test false-alarm is
discharged as `invalidated`, not inflated into the journal). Undischarged → BLOCK (§5j).

## 6a. Promoted standing checks — gate-change / security-property review discipline (NORMATIVE)
Three defect-classes from the corpus (orchestrator-patterns Pattern F) earned promotion to STANDING review
bars applied to any change touching a gate, a recompute, an authenticity check, or a contamination boundary.
*Recurrence basis (honest, per the promotion-panel):* all three recurred across VARIED SURFACES within ONE
build (the diagnose-live-panel build) — varied surfaces, not yet varied builds; cross-build recurrence would
strengthen them further (tracked in the corpus). Names are kept IDENTICAL to Pattern F so §6b retrieval
indexes a name that exists in the source; instances are DISJOINT (no defect counts toward two classes).
*Honest residual:* these are review discipline, NOT yet gate-mechanized (the merge gate operates on builds
and cannot, e.g., detect an unfaithful fixture). v-next mechanization = a spec-panel/review-brief checklist
lint that flags gate/security tests not consuming production output (task #10 family).

- **`orchestrator-supplied-state-forgeable`** — instances: the diagnose-live-panel content channel (`repo_root` read) + the
  bus-submission provenance (`phase_input`). Verification anchored to caller-supplied MUTABLE state is
  forgeable by construction. **BAR: a gate-touching change must trace its verification's data source to
  IMMUTABLE / independent ground truth** (git blob at a committed SHA, an independent ledger) — never
  caller-supplied mutable state. Where no independent source exists, NAME the limit (honest-limit-named >
  fake-guarantee-shipped).
- **`control-proves-only-its-path`** — instances: the swap-control (proved the window channel, missed
  content) + the forge-control (proved a fixture, missed `phase_input` provenance). A passing adversarial
  control proves the logic moves on the channel it TARGETS, not that every input is anchored. **BAR: an
  adversarial control must be shown to traverse the VULNERABLE channel** (enumerate input channels; one
  control per channel that MOVES the verdict — clean twin passes, mutated blocks).
- **`cheap-fake-hidden-by-wrong-axis`** — instances: node-id regex (tested reject-garbage, never
  accept-the-real-distribution), count-normalized confidence (tested strong-vs-weak, never weak-additions),
  noisy-OR within-category (tested cross-category, never within-category padding). A test green on the
  easy/negative axis is silent on the load-bearing/positive axis. **BAR: a gate/security test must exercise
  the LOAD-BEARING axis on representative production input, and a fix must be DENY-PROVEN (shown to FAIL
  against the old/wrong implementation).** *Scope (promotion-panel):* this targets UNFAITHFUL fixtures (a
  fixture diverging from production on the load-bearing dimension) and wrong-axis tests — it does NOT ban
  hand-constructed inputs per se; adversarial/deny-proof twins and validated mocks legitimately craft inputs
  to MOVE the verdict. The defect is *unfaithful*, not *constructed*.

## 6b. Retrieval — the review panel CONSULTS the defect-class vocabulary (NORMATIVE; STICKY hook)
A corpus written but never read is a diary, not a loop — and a convention that relies on the reviewer
remembering is silently skippable on a busy review (promotion-panel/GLM). So retrieval is made STICKY: the
review-brief template for any change touching a gate / recompute / authenticity check / contamination
boundary carries a REQUIRED field — **"§6a bars applied: which, and the evidence each control MOVED the
verdict?"** — and the panel cannot return a verdict with that field blank. A finding matching a named class
(Pattern F + §6a) is TAGGED as a recurrence (strengthening its promotion case); recognizing a recurrence as
a recurrence — not re-discovering it as new — is how the loop compounds. The hook is wired into the
review-brief shape (orchestrator-patterns Pattern C). *Honest residual:* the field is a review-brief
convention enforced by the orchestrator today, not a mechanized gate step (the gate sees builds, not review
briefs) — v-next mechanization is the spec-panel/review-brief lint (task #10 family).

## 7. Dogfood / acceptance (negative controls via the REAL gate; assert exact block_reason per case)
(1) load-bearing change, no dimension test → BLOCK[cheap-fake]; (2) dimension-faithful twin → PASS;
(3) unit-mock outside layers → PASS; (4) engine declares only `interface` for a registry-latency component
→ BLOCK[missing-required-dimension]; twin declares it → PASS; (5a) new symbol under layer path, no entry →
BLOCK[unclassified]; (5b) production symbol matching no pattern (`lib/engines.py`) → BLOCK[unclassified]
(bidirectional); (6) declarative phase → `verified:false,judged:true` pass; asserting `verified:true` →
BLOCK; (7) executable phase, SHA≠HEAD or forged stale `captured_output_path` → BLOCK; clean match → PASS;
(8) builder-supplied `gate_decision` → BLOCK/ignored; (9) **plausible-waiver** of a required dimension →
gate PASSES the presence check, `gate_result` marks `exclusion_panel_judged:true` (documents that panel
catches a lie out-of-band — matches §2's honest claim); (10) **stale-correctness-basis**: a phase-record
panel-judged for a stage whose validator is now merged+verified → BLOCK[stale-correctness-basis]; (11)
self-cert: reviewer_seat==skill-under-review → BLOCK[self-certification]; (12) **stale-root**: a change to ANY
file in the certified object — `gate/**` OR **`SKILL.md` alone** — past `certified_object_sha` with no
rotation record → BLOCK[stale-trust-root]; root advance citing the prior SHA's panel or reusing a prior
seat → BLOCK; (13) **boundary-evasion**: a production (non-excluded) file in a brand-new top-level dir
resolving to no layer → BLOCK[unclassified] (production-by-default, nothing to hide outside of); (14) **downward-invalidation**: a record on a
validator later marked `invalidated` → BLOCK[stale-correctness-basis]. Controls must **mutate inputs**
(not be fixture-recognizers) and assert exact `block_reason`. Meta-control: a registry lacking a catch-all,
or under which any inclusion-root path resolves to nothing, is a setup error (not just degenerate/empty).
The skill's own build passes this gate.

## 8. Out of scope
Not a CI platform; a contract + runnable LOCAL gate (advisory-for-merge; "BLOCK" becomes binding only when
wired as a non-bypassable CI status check — a deployment follow-up). The diagnose-as-gate-engine upgrade
happens only after diagnose is merged+verified (§1.1, §1.3).
