# Plan — `bridge-protocol` skill (TDD task breakdown) — v2

> Build per spec `docs/superpowers/specs/2026-06-18-bridge-protocol-skill-SPEC.md` (v5, §1.6 hash-domain
> revised). v2 folds the 3/3 plan-panel (codex+cold-Opus+GLM-judge). TDD: failing test → fail → minimal
> impl → pass → commit. Python stdlib only. Skill at `skills/bridge-protocol/`.

**Goal:** a fail-closed merge-gate (`gate.py`) + the SKILL.md contract, with a dogfood suite that proves
the gate fires on EVERY §5 BLOCK condition (a–l) AND passes the faithful twin of each.

## File structure
- `skills/bridge-protocol/SKILL.md` — the invokable pipeline contract.
- `skills/bridge-protocol/gate/gate.py` — the runnable gate (LOGIC).
- `skills/bridge-protocol/gate/schemas/*.json` — schemas (LOGIC): phase_input, gate_result, trust_root,
  trust_root_rotation, layer_registry, load_bearing_components, validator_readiness, stage_modes.
- `skills/bridge-protocol/gate/layer_registry.json`, `stage_modes.json` — committed contract DATA.
- `skills/bridge-protocol/gate/validator_readiness.json` — materialized DATA (producer: §Task 5).
- `skills/bridge-protocol/gate/results/` — append-only `gate_result` history (DATA; source for readiness).
- `skills/bridge-protocol/gate/trust_root.json` — the external base case (pinned POST-review, §Task 10).
- `tests/test_bridge_protocol_gate.py` — dogfood (16 matched-pair controls).

**Hash domain (load-bearing, §1.6):** `certified_object_sha` = tree hash over `{gate.py, gate/schemas/*,
SKILL.md}` ONLY. Carve-out (DATA, never hashed): `trust_root.json, layer_registry.json,
validator_readiness.json, stage_modes.json, results/*, *_result.json`. Field names follow the spec exactly:
input carries `hard_signal_evidence` (not `hard_signal`); `gate_decision`/`block_reasons` exist ONLY in the
gate-produced `gate_result`.

## Tasks

### Task 1 — Schemas + a DEPTH-bounded validator (fail-closed at the entry point)
- [ ] Failing test: the validator recurses into EXACTLY the nested paths the BLOCK evaluator reads —
  `reviewer_reports[].findings[].status` (§5a), `escaped_defect.state` (§5j),
  `hard_signal_evidence.commit_sha` (§5b), `dimensions_considered_and_excluded[].waiver_status` (§2). A
  malformed nested input (e.g. finding with no `status`) → REJECT at load; a `phase_input` carrying
  `gate_decision`/`block_reasons` → REJECT (§5i).
- [ ] Impl: write 8 JSON Schemas + a stdlib validator that recurses the enumerated paths (document the
  bounded scope + residual). Commit.

### Task 2 — Git ground-truth + run-attestation (the gate recomputes; stale output detectable)
- [ ] Failing test (temp git repo): `derive_ground_truth` returns `{head, clean_tree}`; a
  `hard_signal_evidence.commit_sha != HEAD` → BLOCK[b]; `run_after_final_diff` DERIVED not read. Run
  attestation: evidence carries `tree_hash, captured_output_sha256, exit_code, start/end, runner_id`; the
  gate verifies `tree_hash == HEAD tree` and that `captured_output_sha256` matches the file at
  `captured_output_path` → a STALE log (mismatched sha256) or wrong-tree → BLOCK; a fresh matching one →
  PASS. (Honest scope §4: the gate does not re-EXECUTE the command; it pins the captured artifact to HEAD.)
- [ ] Impl: `git rev-parse`, `git status --porcelain`, tree hash, sha256. Commit.

### Task 3 — Layer registry (production-by-DEFAULT) with DETERMINISTIC matching
- [ ] Failing test: matching algo = normalize POSIX rel path → `excluded_roots` first (then non-production)
  → else production: most-specific layer rule, else catch-all, else BLOCK[unclassified]/setup-error. A
  production file in a new top-level dir resolving to no layer → BLOCK; a `tests/` file → non-production; an
  **`excluded_roots` edit is itself load-bearing** → requires a reviewer finding (BLOCK without sign-off).
- [ ] Impl + meta-control (registry with no catch-all, or any production path resolving to nothing → setup
  error). Commit.

### Task 4 — Manifest cheap-fake completeness + waiver RESOLUTION
- [ ] Failing test: registry-required dimension absent from BOTH `costly_dimensions` AND an approved
  exclusion → BLOCK[missing-required-dimension]; engine declaring only `interface` for a latency component →
  BLOCK; faithful twin (declares latency + a latency-preserving test) → PASS; `waiver_finding_id` that does
  NOT resolve to a real finding approved by `waiver_reviewer` → BLOCK; an approved-resolving waiver → PASS.
- [ ] Impl. Commit.

### Task 5 — Correctness-basis transitions (define the readiness SOURCE + genesis)
- [ ] Sub-spec first: `validator_readiness.json` schema = list of `{validator_id, mode:blind|steered,
  status:absent|merged-unverified|verified|invalidated, merge_sha, verified_sha, invalidated_sha_reason,
  source_gate_result_path, source_gate_result_sha256, effective_from_sha}`. **Producer:** after a
  validator's gate passes, an appended `gate/results/<sha>.json` is the source; `validator_readiness.json`
  is the materialized projection (a named step writes it). **Genesis (fail-closed):** no `results/` history
  ⇒ all validators `absent` ⇒ only the manual base case is available (this is bridge-protocol's own state).
- [ ] Failing test: `basis_available(validator_readiness, stage_modes, head)` is pure; manual-panel record
  for a stage whose validator is now `verified` → BLOCK[stale-correctness-basis] (upward); a record on a
  now-`invalidated` validator → BLOCK (downward); steered stage upgraded before `diagnose-steer` verified →
  BLOCK[circular-validator-dependency]; genesis → manual base case, no false stale. Commit.

### Task 6 — Trust-root coupling (LOGIC-set tree hash) + rotation schema
- [ ] Failing test: `certified_object_sha` = tree hash over `{gate.py, gate/schemas/*, SKILL.md}` ONLY
  (carve-out enforced — changing `layer_registry.json` does NOT change it; changing `gate.py`/a schema/
  `SKILL.md` DOES); gate recomputes `running_gate_sha` identically; drift → BLOCK[stale-trust-root]; a
  `reviewer_seat == skill-under-review` → BLOCK[self-certification]. Rotation: `trust_root_rotation` schema
  `{old_sha, new_sha, reason, certifying_seats[], human_approver, change_author, invalidated_basis_records[]}`;
  BLOCK unless `old_sha==current root`, `new_sha==recomputed logic-set hash`, `certifying_seats` disjoint
  from prior root seats AND from `change_author`, `human_approver` present, listed invalidated records exist.
- [ ] Impl (hashlib over sorted path+content of the logic set). Commit.

### Task 7 — BLOCK-condition evaluator (wire §5 a–l) → emits `gate_result`
- [ ] Failing test: `evaluate(phase_input, repo) -> gate_result` with the exact `block_reasons` for a
  battery covering EACH of §5 a–l (enumerate the cases in the test): (a) open P0/P1 finding; (b) SHA
  mismatch; (c) cheap-fake; (d) unclassified; (e) declarative asserting verified:true; (f-static/dynamic);
  (g) per-mode; (h) self-cert; (i) builder-supplied decision; (j) escaped-defect open; (k) stale-root;
  (l) downward. A clean executable phase + matching attested hard-signal → PASS. Absence of green = block.
- [ ] Impl: compose Tasks 2–6, fail-closed. Commit.

### Task 8 — SKILL.md (the invokable contract)
- [ ] Write `skills/bridge-protocol/SKILL.md`: when to invoke; phases; how diagnose/diagnose-steer plug in
  (bootstrap order §1.1-1.3, external base-case self-cert §1.4-1.6, transition rule). Prose mirroring the
  spec, pointing at gate.py. (Covered by the hash-drift dogfood: editing SKILL.md trips stale-trust-root.)
- [ ] Commit.

### Task 9 — Dogfood suite (16 matched pairs; mutate inputs; assert exact block_reason)
- [ ] `tests/test_bridge_protocol_gate.py` — for EACH §5 condition a BLOCK control AND a faithful-twin PASS:
  cheap-fake/twin; unit-mock-outside-layers PASS; costly-dimension-evasion/twin; unclassified (both
  directions)/classified PASS; declarative verified:true BLOCK / verified:false PASS; forged-stale-output
  BLOCK / fresh PASS; builder-supplied-decision BLOCK; plausible-waiver PASS(panel-judged) + unresolved
  waiver BLOCK; stale-basis up+down BLOCK / correct-basis PASS; self-cert BLOCK; stale-root (gate/schema/
  SKILL.md) BLOCK / data-change PASS; per-mode circular BLOCK; **open-finding BLOCK / resolved PASS (§5a)**;
  **escaped-defect open BLOCK / fixed PASS (§5j)**. Each MUTATES inputs (not a fixture-recognizer). Plus the
  registry meta-control. Run; green. Commit.

### Task 10 — Bootstrap the external base case (POST-review; worker does NOT self-certify)
- [ ] The WORKER commits `gate/trust_root.pending.json` (schema-valid placeholder) + the Task-6 enforcement
  tests. The worker does NOT author the real root (it cannot supply an external cert for its own build — that
  would be self-certification, §1.4).
- [ ] **Orchestrator integration step (post-build, after the tri-panel review of the built object):** pin the
  REAL `gate/trust_root.json` with `certified_object_sha` = recomputed logic-set hash, `certifying_seats` =
  the review panel (codex+cold-Opus+GLM-judge), `change_author` = the builder seat (MUST be disjoint from
  certifying_seats), `human_approver`, `judged_not_verified:true`. Then run `gate.py` against the repo →
  `gate_result.gate_decision == pass` (the skill passes its own gate, hash-domain carve-out makes this
  computable). This is the merge-gate of bridge-protocol's own build.

## Self-review
Every §5 BLOCK condition → a Task test + a Task-9 matched pair (block + faithful twin). No placeholders:
each task names its concrete operands (readiness source, hash domain, rotation fields, attestation fields).
Bootstrap respects the external-base-case rule (worker never self-certifies).
