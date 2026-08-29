# H2 Producer — Design

**Status:** v4 — design-panelled + spec-panelled + re-spec-reviewed + v3 re-confirm + operator-approved →
**ready for `writing-plans`.** The graduation criterion was hardened across 7 review rounds; the spiral
**terminated correctly** — not by finding the last hole but by *diagnosing the threat-class* of the remaining
ones: **mistake-class** holes (an honest reviewer's genuine dispositions misleading the FP rate — silence,
empty-run, small-sample, presence-not-validity, fabricated free-text FP-token) are all CLOSED (§5/§8);
**adversarial-disposition** holes (a reviewer *deliberately* mis-disposing to game graduation —
token-discrimination, proportion-floor evasion) are a **named, triggered out-of-scope non-goal** (§9,
threat-model match — ARB's threat is mistakes, not a malicious trusted operator). v4 folds the one remaining
mistake-class fix (anchor-check the `not_load_bearing` FP-token, §3/§5/§8) + that precise non-goal.
Follow-slice of the defect-detection skill: flips **H2 from dormant to operative**.

**Provenance note (why the graduation deny-proofs are load-bearing):** the panels caught this mechanism
almost-shipping a *hollow gate of its own* three times — never-graduates (uncomputable FP), false-graduates
(FP gameable by silence), and unstable-id (FP skewed by a shifting/colliding denominator). Each was "the
graduation criterion *looks* falsifiable but doesn't measure what it claims." The §5/§8 precision below is
what makes graduation **measurable, achievable, AND un-gameable** — the three legs it needs.

**Goal:** The H2 schema-enforcement gate is built and live (`gate.py` `h2_standing_check`/`_h2_section`,
`h2_assumptions.py` `validate_h2_section`) but **dormant** — nothing emits an `h2_section`, so it reports
`h2_status=dormant-no-producer` and never enforces. This slice builds the **producer**: the gate **derives**
candidate environmental assumptions from the review diff (the H1-analog), **forces a reviewer disposition per
candidate**, runs in **shadow mode** by default, and graduates to **block** via a falsifiable, self-computing,
silence-proof criterion.

---

## 1. Architecture

`h2_derive_candidates(repo, changed_paths, diff) -> list[CandidateAssumption]` in `gate.py`, reusing the same
review-diff source `h1_standing_check` uses. The gate matches derived candidates to `phase_input.h2_section`
rows **by candidate id**, computes each candidate's disposition, emits an `H2Record` in its result (the gate
stays **pure** — no I/O), and (per mode) notices or blocks. Additive; replaces the `dormant-no-producer` path.

## 2. Candidate-id contract — churn-invariant AND collision-distinct (fix: this is where the FP rate lives)

```
CandidateAssumption{ id, kind, callee, site: "relpath:line", occurrence: int }
id = f"{kind}:{relpath}:{callee}#{occurrence}"
```
- `callee` = the **as-written dotted call target** from the AST (`redis.from_url`, `psycopg.connect`,
  `subprocess.run`) — **no import-alias normalization** (it would re-open id instability; an import-path
  change is a rare, accepted edge).
- `occurrence` = the **1-based index of this `(kind, callee)` among the file's qualifying module-level calls
  in source order** — NOT a line number.
- **Churn-invariant:** inserting lines above a call does not change its source-order index → same id next run.
- **Collision-distinct:** two identical `requests.get(...)` calls in one file get `#1` and `#2` (a single
  shared id would silently merge two candidates → undercount the FP denominator → skew graduation).
- Match by **id**, never prose. A derived candidate whose id no row references is **unanswered**. A row
  referencing an **unknown** id is a malformed brief (rejected — no inventing dispositions).
- **Accepted edge:** the `occurrence` index shifts only if **another qualifying same-`(kind,callee)` call is
  inserted above** the site (rare); that re-numbers the later calls. Documented as accepted (the alternatives —
  line numbers, AST-node hashes — each have worse instabilities); not worth a stronger anchor.
- **Deny-proofs (§8):** (a) churn-perturbation — id survives line insertion above the site; (b) repeated-call —
  two identical calls get distinct ids.

## 3. h2_section format (migration — REPLACES the old shape) + forced disposition (fix #2)

The `h2_section` becomes an **object** (was a bare row list):
```
h2_section = { coverage_acknowledgment: {acknowledged: bool, additional_assumptions: [row...]},
               rows: [ DispositionRow... ] }
```
Each `DispositionRow` disposes one candidate by id with **exactly one** disposition:

| disposition | shape | meaning | graduation signal |
|---|---|---|---|
| `answered` | `{candidate_id, disposition:"answered", violating_run, evidence}` (evidence anchors a real artifact, existing anchor check) | real assumption, with a violating run | true positive |
| `not_load_bearing` | `{candidate_id, disposition:"not_load_bearing", reason, evidence}` — **`evidence` anchors a spot-checkable artifact** (the file:line showing *why* it's not load-bearing: a test-only call site, a feature-flag guard, a gracefully-degraded path) — same anchor check as `answered`/`flag` | reviewer judges the candidate is **not** a real held-axis assumption here | **FALSE-POSITIVE label** |
| `flag` | `{candidate_id, disposition:"flag", assumption, evidence}` | the assumption **is** violated (a real defect) | escalation |

The single disposition vocabulary `{answered, not_load_bearing, flag}` **replaces** the old
`{assumption,violating_run,evidence}` / `{decision:"FLAG"}` rows. `validate_h2_section` is updated to the new
object shape; `disposition:"flag"` is the only FLAG representation (the old `decision:"FLAG"` is removed).

**Migration honesty + FULL scope (don't undercount — the panel caught this under-scoped twice):**
`tests/test_bridge_protocol_gate.py` (the gate's non-H2 behavior) stays green. Every **old-format** use of an
`h2_section` (row-list) / bare `decision:"FLAG"` must be migrated to the new object+disposition shape — and
the plan's **first task greps `tests/` for every such use** rather than trusting a hand list. Known migration
sites (grep-verified at spec time, NOT assumed exhaustive): `tests/defect_hunts/test_h2.py` (the validator
tests — incl. `test_explicit_flag_is_valid_alternative`, whose semantics **flip**: a FLAG now **blocks**, §7,
not "valid alternative"), `tests/defect_hunts/test_wiring.py` (gate H2 wiring), and
`tests/defect_hunts/eval/negatives.json` (the `h2-safe-pinned-assumption` case carries an old-shape
`h2_section`). The spec does NOT claim those stay green — they are rewritten.

## 4. Derivation heuristics — thin, call-based, AST-not-grep (fix #3)

Each heuristic fires only on a **module-level CALL in the diff's added lines** (not a bare import; not a
comment/string), excluding calls inside `if __name__=="__main__":`, `if TYPE_CHECKING:`, or a **noop-guarded
try** (precise rule: the call is lexically in a `try` body that has ≥1 `except` handler whose body does NOT
re-raise — a bare `raise`; such calls are gracefully-degraded, not hard dependencies):

| signal (added-line, module-level call) | candidate |
|---|---|
| `redis.from_url(...)` / `redis.Redis(...)` | "Redis/Valkey reachable" |
| `psycopg.connect(...)` | "Postgres reachable" |
| `subprocess.run/Popen`, `socket.socket/create_connection`, `urllib.request.urlopen`, `requests.<verb>` | "external process/network reachable" |

**Dropped:** `os.environ.get(X, default)` → that is H1's input (config-drift). **Blind spot (bounded by §6):**
derives only static, module-level, call-site assumptions; not runtime/implicit/cross-file/cross-service.

## 5. Shadow / block + graduation — pure gate, complete-runs-only, silence-proof (fixes #2+#3 COUPLED)

`h2_mode` is a **`gate.py` module constant `H2_MODE`** (default `"shadow"`), NOT a `phase_input` field — the
builder must not flip enforcement per-review. Flipping shadow→block edits the constant, which changes the gate
object hash → the operator must **re-pin the trust root** (the same gate-change→re-pin workflow every gate edit
already follows). That re-pin *is* the operator's "I've earned it" action.

- **The gate is pure:** `evaluate` returns an `H2Record{run_id, h2_mode, derived:[candidate ids], dispositions,
  coverage_acknowledged: bool, complete: bool}` in its result. It does **no I/O**.
- **The collector (separate):** a step outside `evaluate` (CI / orchestrator / a `gate-collect` script) appends
  the `H2Record` (one JSON object per line) to an **append-only** shadow log. Path (pinned): `$ARB_H2_SHADOW_LOG`
  if set, else `$XDG_STATE_HOME/arb/h2-shadow-log.jsonl`, else `~/.local/state/arb/h2-shadow-log.jsonl` — a
  state dir **outside the judged repo tree** (never in-repo, so the clean-tree check never sees it). JSONL
  record schema = the `H2Record` fields (`run_id, h2_mode, derived:[id...], dispositions:[{candidate_id,
  disposition, valid:bool}], coverage_acknowledged, complete`). The graduation query reads this log.
- **`h2_status` enum (pinned):** `{ "shadow", "enforced", "static-only-unacknowledged", "flagged" }` — the
  legacy `"dormant-no-producer"` is removed (there is now a producer).
- **Log integrity is a documented NON-GOAL (threat-model match):** the shadow log is not tamper-protected
  (no hash-chain/signature). ARB's threat model is operator *mistakes*, not a malicious operator who edits a
  local log to force graduation; adversary-grade log integrity is a **productization-era** guard (alongside
  reviewer-attestation / the from→task ledger) — add it only if ARB ever runs where the operator isn't
  trusted. Building it now is over-engineering for the real stakes.
- **shadow:** unanswered candidates → loud non-blocking notice + `h2_status="shadow"`.
- **block:** unanswered candidate → `BLOCK_H2_STANDING_CHECK`; incomplete `answered` row → block; `flag` →
  block (§7); missing `coverage_acknowledgment` → block (§6).
- **`is_complete(record)` — an executable predicate (load-bearing; every prior round, a loose spot here got
  gamed — guard presence AND validity AND non-emptiness):**
  ```
  is_complete(record) :=
       record.coverage_acknowledged is True
   AND len(record.derived) >= 1                          # (v3) empty-run guard — no vacuous-complete
   AND for every derived candidate id: a row references it with disposition in {answered, not_load_bearing,
       flag} AND that row is VALID per validate_h2_section  # (v3) validity, not mere presence;
       # (v4) validate_h2_section now ANCHOR-CHECKS not_load_bearing too — the FP-numerator token is no
       #      longer fabricable free-text, closing the inner hollow spot v3's validity guard had on it
  ```
  A run is **not complete** if any derived candidate is undisposed OR its row is **invalid** (an `answered`
  with empty `violating_run` / unanchored evidence is NOT complete — presence ≠ validity), OR it has **zero**
  derived candidates (an empty diff cannot earn graduation credit), OR `coverage_acknowledged` is not `True`.
- **Graduation criterion (computable query over complete runs — measurable, achievable, un-gameable on all
  six rounds):** flip `h2_mode` shadow→block when ALL hold over the complete runs in the shadow log:
  - **≥10 complete runs** (each ≥1 *valid*-disposed candidate);
  - **≥20 total disposed candidates** across them — an FP rate over a handful of candidates is not a
    measurement (statistical grounding; also makes the denominator's 0/0 unreachable);
  - **discrimination present:** `Σ not_load_bearing ≥ 1` AND `Σ (answered + flag) ≥ 1` across the window — a
    *uniform* window (all one disposition) makes the FP rate meaningless and is excluded;
  - **`FP_rate = Σ not_load_bearing / Σ (answered + not_load_bearing + flag) < 0.10`** — denominator is
    **disposed**, never **derived**.
  Incomplete runs contribute to none of these. Query + flip live in `docs/runbooks/h2-graduation.md`.
  *Intended, not a bug:* a reviewer who judges the derivation useless and marks every candidate
  `not_load_bearing` (FP→100%) keeps it in shadow — that is correctly *withholding trust*, the system working.
  A genuinely zero-FP derivation (never one FP over the window) is a rare edge the operator graduates by
  documented manual judgment, not auto-flip (the discrimination floor guards the common rubber-stamp case,
  not the rare-perfect one).
- **Coupling (both required, or graduation is theatre):** #2 makes FP **measurable** (the label is captured),
  #3 makes it **achievable** (thin heuristics so a realistic diff doesn't flood). §8 deny-proves both.

## 6. coverage_acknowledgment — bound the false-confidence (fix #5)

`h2_section.coverage_acknowledgment = {acknowledged: bool, additional_assumptions: [DispositionRow...]}`. The
gate does not report an unqualified "H2 enforced/passed" unless `acknowledged is True`; absent/false → status
`static-only-unacknowledged` (notice in shadow, **block** in block-mode). `acknowledged:True` is a precondition
of `is_complete` (§5), so a run can't graduate without it. `additional_assumptions` are reviewer-added rows
(assumptions the static derivation missed, disposition `answered` or `flag`) — each is **validated like any
row** (well-formed or the section is invalid), and a `flag` among them **blocks** (§7). They are NOT derived
candidates, so they do not enter the FP numerator/denominator (the FP rate measures *derivation* accuracy, not
reviewer-added rows).

## 7. FLAG blocks regardless of mode (fix #4 — close the bypass)

A `flag` disposition asserts the assumption **is** violated — a reviewer-asserted defect, not a derivation
artifact. So `h2_standing_check` returns `flagged` → `evaluate` emits `BLOCK_H2_STANDING_CHECK` in **either**
mode. Closes the FLAG-all bypass (skip `violating_run`+`evidence`, gate passes).

## 8. Testing & deny-proof discipline (codex-TDD, failing-test-first)

Standard: per-heuristic derivation (call-based, AST-not-grep, module-level, the 3 exclusions incl. the precise
noop-try rule); shadow vs block; `validate_h2_section` new-shape; `test_bridge_protocol_gate.py` green;
`trust_root.json` re-pin. **The H2 validator (`skills/defect_hunts/h2_assumptions.py`) JOINS the trust-rooted
logic set (`logic_set_paths`)** — it is now load-bearing gate logic, so a tampered validator is caught by the
trust root rather than silently weakening enforcement.

**Hard deny-proofs (load-bearing + security-relevant):**
- **CANDIDATE-ID:** (a) churn-perturbation — insert a line above a call site; the id is unchanged. (b)
  repeated-call — two identical `requests.get(...)` in one file → distinct ids `#1`/`#2`.
- **GRADUATION — measurable + achievable + un-gameable on all six rounds (the headline):**
  (a) **measurable** — a `not_load_bearing` disposition lands in the record and the FP query computes it;
  (b) **un-gameable-by-silence** — a log of **incomplete** (undisposed) runs must NOT graduate (fail-before:
  today's `derived`-denominator lets `0/N` graduate; pass-after: `disposed`-denominator + complete-runs-only);
  (c) **silence boundary** — a run with exactly **one** candidate undisposed is **excluded** from the ≥10
  count (`is_complete` is hard, not "mostly complete");
  (d) **achievable** — a realistic single-file diff (a few service calls) derives **≤ K=3** candidates;
  (e) **un-gameable-by-empty-run (v3)** — a log of zero-derived-candidate runs must NOT graduate (no vacuous-
  complete; `0/0` never arises — `len(derived) ≥ 1` + the ≥20-disposed floor); inject-revert the `len≥1`
  guard → it graduates → red;
  (f) **validity-not-presence (v3)** — a run whose `answered` row has an **empty `violating_run` / unanchored
  evidence** must NOT count as complete (fail-before: presence-only `is_complete` counts it; pass-after:
  validity-required excludes it); inject-revert the validity clause → red;
  (g) **discrimination (v3)** — a **uniform** window (all `answered`, or all `not_load_bearing`) must NOT
  graduate (the FP rate is meaningless); inject-revert the discrimination floor → an all-`answered` window
  graduates → red.
  (h) **anchored FP-token (v4 — the last mistake-class fix)** — a `not_load_bearing` row with **unanchored /
  free-text `evidence`** must make the run **invalid → not complete** (fail-before: v3 anchored only
  evidence-bearing rows, so the FP token was fabricable; pass-after: it's anchor-checked like `answered`);
  inject-revert the `not_load_bearing` anchor check → a junk FP token counts → red.
- **FLAG-blocks (security — hardest):** a review where **every** candidate is `flag`ged must **BLOCK**, not
  pass — fail-before/pass-after + inject-revert (remove FLAG-block → deny-proof red).
- **coverage_acknowledgment (security — hardest):** a run that checked only static candidates must **not**
  report an unqualified pass without `acknowledged:True` — fail-before/pass-after + inject-revert.

## 9. Scope / deferred

- Ships in **`shadow` mode** with the full graduation machinery + `docs/runbooks/h2-graduation.md`. The flip to
  `block` is an operator action once ≥10 complete runs show FP < 10% (re-pins the trust root).
- Cross-file / cross-service / runtime derivation is **out of scope**, bounded by `coverage_acknowledgment`.
- H1 unchanged (it owns `os.environ` config-drift).

### NON-GOAL — graduation-gaming by *deliberate* mis-disposition (a named, triggered limit, not a hole)

The graduation criterion is hardened against an **honest reviewer's mistakes** (silence, empty-runs,
small-sample, presence-not-validity, a fabricated free-text FP-token — all closed §5/§8). It is **NOT**
hardened against a reviewer who **deliberately mis-disposes with intent to deceive the gate.** This is a
**choice matched to the threat model, not an oversight** — stated precisely so it can never become a
euphemism for an unaddressed hole:

- **What is out of scope (named):** graduation-gaming via deliberate disposition-distribution shaping —
  specifically **token-discrimination** (mark 1 candidate `not_load_bearing` + N real FPs as anchored
  `answered` to hide them and buy a sub-10% FP rate) and **proportion-floor evasion** (shaping the disposed
  mix to clear whatever ratio/spanning thresholds a stricter criterion would add). Once the reviewer
  *deliberately* controls the metric's input, the gaming surface is an **infinite regress** — every added
  threshold has a next shape that clears it.
- **Why:** ARB's threat model is **operator mistakes, not a malicious trusted operator** (solo, trusted
  infra). Gaming these requires *intent to deceive + real effort* (e.g. N anchored `answered` rows hiding
  real FPs) — an **adversary**, not a mistake. Same basis as the declined shadow-log hash-chain (§5) and the
  deferred from→task ledger / reviewer-attestation.
- **The trigger that re-scopes it:** if ARB ever runs with an **untrusted operator** (productization), this
  residual becomes in-scope. The known fixes are then: a **proportion FP floor** (`0.05 ≤ FP`), `Σ
  not_load_bearing ≥ 2` **spanning ≥2 distinct runs**, and adversarial-disposition guards. Build them *then*,
  not now.
- **Honest acknowledgment:** H2 graduation is sound against honest-reviewer mistakes and *not* against a
  deliberately-deceptive reviewer — a deliberate, threat-model-matched limit. A named limit beats a fake
  guarantee.

## 10. Workflow

(this) spec v2 → spec re-review → operator review → `writing-plans` → codex-TDD build → review panel →
merge-hold for the operator's `→ dev` review.
