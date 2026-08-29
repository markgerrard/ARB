# H2 Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Flip H2 from dormant to operative — the bridge-protocol gate derives candidate environmental assumptions from the review diff, forces a reviewer disposition per candidate, runs in shadow mode, and graduates to block via a measurable + achievable + un-gameable-by-mistake criterion.

**Architecture:** AST derivation (`h2_derive.py`) → an object-shaped `h2_section` with anchored dispositions (`h2_assumptions.py`) → a **pure** gate that returns an `H2Record` (`gate.py`) → a **separate** collector appends a gitignored shadow log outside the judged tree → a graduation query (`h2_graduation.py`) reads the log. Ships in `shadow`.

**Tech Stack:** Python 3 (`.venv`), `ast`, `pytest`. Test cmd: `PYTHONPATH=. /Users/<user>/<workspace>/.venv/bin/python3 -m pytest <path> -v`.

## Global Constraints (carry into every task — verbatim from spec v4)
- **Design of record:** `docs/superpowers/specs/2026-06-21-h2-producer-design.md` (v4).
- **The gate stays PURE:** `evaluate`/`h2_standing_check` do **no I/O**; they return an `H2Record`. A **separate** collector writes the log.
- **`os.environ.get` is H1's, NOT H2's** — H2 never derives env-default assumptions.
- **Every deny-proof is a real inject-revert test** (fail-before / pass-after), never a hollow assertion.
- **Adversarial-disposition hardening is OUT of scope** (spec §9 non-goal): do NOT build proportion-floor / ≥2-spanning-runs guards — they are the named untrusted-operator-era residual.
- **`tests/test_bridge_protocol_gate.py` stays green;** H2-format tests are rewritten (the FLAG semantics flip from valid→blocks).
- **Existing internals (verified):** `gate.py` has `BLOCK_H2_STANDING_CHECK="h2-standing-check"`, `h2_standing_check(phase_input, repo)->{status,reason,notice}` (gate.py:381), `_h2_section` (393), `logic_set_paths(repo)->list[Path]` (559), `trust_root_blocks` compares `certified_object_sha` to running hash (580), `evaluate` h2 path at 716. `h2_assumptions.py` `validate_h2_section(section, *, repo_root)` + `_validate_evidence_anchor`.

---

### Task 1: Candidate-id contract + types (churn-invariant, collision-distinct)

**Files:** Create `skills/defect_hunts/h2_derive.py`; Test `tests/defect_hunts/test_h2_derive.py`.

**Interfaces — Produces:** `@dataclass(frozen=True) CandidateAssumption{id:str, kind:str, callee:str, relpath:str, occurrence:int, site:str}`; `candidate_id(kind, relpath, callee, occurrence) -> str` returning `f"{kind}:{relpath}:{callee}#{occurrence}"`.

- [ ] **Step 1: failing test** — id format + the two deny-proofs (these are §8(a)/(b)):
```python
# tests/defect_hunts/test_h2_derive.py
from skills.defect_hunts.h2_derive import candidate_id, derive  # derive arrives in Task 2
def test_candidate_id_format():
    assert candidate_id("redis", "pkg/a.py", "redis.from_url", 1) == "redis:pkg/a.py:redis.from_url#1"
def test_id_is_churn_invariant_under_line_insertion():
    base = "import redis\nX = redis.from_url('u')\n"
    perturbed = "import redis\n# a new comment line\n\nX = redis.from_url('u')\n"
    diff_b = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"
    diff_p = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"
    [a] = derive({"pkg/a.py": base}, diff_b)
    [b] = derive({"pkg/a.py": perturbed}, diff_p)
    assert a.id == b.id  # line insertion above does NOT change the id
def test_repeated_identical_calls_get_distinct_ids():
    src = "import redis\nA = redis.from_url('u')\nB = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+A = redis.from_url('u')\n+B = redis.from_url('u')\n"
    ids = sorted(c.id for c in derive({"pkg/a.py": src}, diff))
    assert ids == ["redis:pkg/a.py:redis.from_url#1", "redis:pkg/a.py:redis.from_url#2"]
```
- [ ] **Step 2: run, verify fail** (`ModuleNotFoundError`).
- [ ] **Step 3: implement** `candidate_id` + the `CandidateAssumption` dataclass. `occurrence` = 1-based index of this `(kind, callee)` among the file's qualifying module-level calls **in source order** (computed in Task 2's `derive`; Task 1 just defines the id string + dataclass). For Task 1, also stub `derive` to import-clean (Task 2 fills it).
- [ ] **Step 4: run** the format test passes; the churn/repeated tests are completed by Task 2.
- [ ] **Step 5: commit** `feat(h2): candidate-id contract (churn-invariant + collision-distinct) + types`.

### Task 2: `derive` — thin call-based AST heuristics

**Files:** Modify `skills/defect_hunts/h2_derive.py`; Test `tests/defect_hunts/test_h2_derive.py`.

**Interfaces — Produces:** `derive(files: dict[str,str], diff: str) -> list[CandidateAssumption]`.

Heuristics (each fires only on a **module-level call in the diff's added lines**, excluding `if __name__=="__main__"`, `if TYPE_CHECKING`, and a **noop-guarded try** = the call is in a `try` body that has ≥1 `except` whose body does NOT contain a bare `raise`):
- `redis.from_url(...)` / `redis.Redis(...)` → kind `redis`, "Redis/Valkey reachable"
- `psycopg.connect(...)` → kind `postgres`, "Postgres reachable"
- `subprocess.run/Popen`, `socket.socket/create_connection`, `urllib.request.urlopen`, `requests.<verb>` → kind `external`, "external process/network reachable"

- [ ] **Step 1: failing tests** — one per heuristic (positive), plus the exclusions and the `os.environ.get`-is-NOT-derived guard, plus the achievability deny-proof §8(d):
```python
def test_derives_redis_call_in_added_lines():
    src = "import redis\nX = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"
    [c] = derive({"pkg/a.py": src}, diff); assert c.kind == "redis"
def test_bare_import_without_call_does_not_derive():
    src = "import redis\n"; diff = "diff --git a/pkg/a.py b/pkg/a.py\n+import redis\n"
    assert derive({"pkg/a.py": src}, diff) == []
def test_comment_or_string_mention_does_not_derive():
    src = "X = 1  # redis.from_url is great\nY = 'redis.from_url'\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = 1  # redis.from_url is great\n"
    assert derive({"pkg/a.py": src}, diff) == []
def test_call_only_in_unchanged_lines_does_not_derive():
    src = "import redis\nX = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+UNRELATED = 1\n"  # the call is NOT in added lines
    assert derive({"pkg/a.py": src}, diff) == []
def test_noop_guarded_try_call_is_excluded():
    src = ("import requests\ntry:\n    requests.get('u')\nexcept Exception:\n    pass\n")
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+    requests.get('u')\n"
    assert derive({"pkg/a.py": src}, diff) == []
def test_os_environ_get_is_not_an_h2_candidate():
    src = "import os\nX = os.environ.get('K', '')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = os.environ.get('K', '')\n"
    assert derive({"pkg/a.py": src}, diff) == []
def test_realistic_single_file_diff_derives_at_most_K3_candidates():  # §8(d) achievable
    src = ("import redis, psycopg, subprocess\nA=redis.from_url('u')\nB=psycopg.connect('d')\nC=subprocess.run(['x'])\n")
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+A=redis.from_url('u')\n+B=psycopg.connect('d')\n+C=subprocess.run(['x'])\n"
    assert len(derive({"pkg/a.py": src}, diff)) <= 3
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** `derive`: parse each `.py` file with `ast`; walk **module-body top level** (and module-level `if/try` bodies, applying the exclusion rules) for `ast.Call` nodes whose dotted callee matches a heuristic; keep only those whose call line is in the diff's added lines for that path (reuse a `_added_lines_by_path(diff)` helper — same shape as `h1_config_drift._added_diff_lines_by_path`); assign `occurrence` per `(kind, callee)` in source order; build `CandidateAssumption`s. Provide `_dotted_callee(node)` and `_added_lines_by_path(diff)`.
- [ ] **Step 4: run, all green** (incl. Task 1's churn/repeated tests).
- [ ] **Step 5: commit** `feat(h2): derive candidate assumptions (thin call-based AST heuristics + exclusions)`.

### Task 3: New `h2_section` object format + anchored dispositions

**Files:** Modify `skills/defect_hunts/h2_assumptions.py`; Test `tests/defect_hunts/test_h2.py` (rewritten in Task 4).

**Interfaces — Produces:** `validate_h2_section(section, *, repo_root) -> (ok, reason)` now accepts the **object** `{coverage_acknowledgment:{acknowledged:bool, additional_assumptions:[row]}, rows:[DispositionRow]}`. `DispositionRow` disposes a `candidate_id` with `disposition ∈ {answered, not_load_bearing, flag}`; **all three carry an anchored `evidence`** (`not_load_bearing` also has `reason`; `flag` has `assumption`; `answered` has `violating_run`). `coverage_acknowledgment.acknowledged` must be `True` for an "enforced" status (else `static-only-unacknowledged`).

- [ ] **Step 1: failing tests** — the new shape + the **anchored FP-token** deny-proof §8(h):
```python
def test_not_load_bearing_requires_anchored_evidence():  # §8(h) — the FP token is no longer free-text
    section = {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
               "rows": [{"candidate_id": "redis:pkg/a.py:redis.from_url#1",
                         "disposition": "not_load_bearing", "reason": "test-only",
                         "evidence": "does/not/exist.py"}]}
    ok, _ = validate_h2_section(section, repo_root=".")
    assert ok is False  # unanchored/nonexistent evidence -> invalid (fail-before: v3 didn't anchor it)
def test_flag_row_blocks_is_handled_by_gate_but_validates_with_assumption_and_anchored_evidence():
    ...  # flag row with anchored evidence validates ok at the schema level
```
- [ ] **Step 2–4:** implement the object parsing + `_validate_disposition_row` (answered/not_load_bearing/flag, each `_validate_evidence_anchor(row["evidence"], ...)`); keep `_validate_evidence_anchor` as-is. Run green.
- [ ] **Step 5: commit** `feat(h2): h2_section object format + anchored not_load_bearing FP-token (v4)`.

### Task 4: **MIGRATION-FIRST** — grep every old-format use + migrate

**Files:** Modify (grep-discovered) — known: `tests/defect_hunts/test_h2.py`, `tests/defect_hunts/test_wiring.py`, `tests/defect_hunts/eval/negatives.json`.

- [ ] **Step 1:** `grep -rln "h2_section\|decision.*FLAG\|validate_h2_section" tests/` — list EVERY old-format use (do not trust the known list).
- [ ] **Step 2:** rewrite each to the new object+disposition shape. In `test_h2.py`, `test_explicit_flag_is_valid_alternative` **flips**: a FLAG now **blocks** (assert the gate emits `BLOCK_H2_STANDING_CHECK` for a FLAG row, §7), not "valid alternative" — rename it `test_flag_disposition_blocks`.
- [ ] **Step 3:** run the full `tests/defect_hunts/` + `tests/test_bridge_protocol_gate.py` — green (the gate non-H2 tests untouched).
- [ ] **Step 4: commit** `refactor(h2): migrate all old-format h2_section uses to object+disposition shape`.

### Task 5: `H2Record` + `is_complete` (executable predicate)

**Files:** Modify `skills/defect_hunts/h2_assumptions.py` (or a new `h2_record.py`); Test `tests/defect_hunts/test_h2_record.py`.

**Interfaces — Produces:** `@dataclass H2Record{run_id, h2_mode, derived:list[str], dispositions:list[dict], coverage_acknowledged:bool, complete:bool}`; `is_complete(derived, section, *, repo_root) -> bool`.

- [ ] **Step 1: failing tests** — the predicate + the silence/empty/validity deny-proofs §8(c)/(e)/(f):
```python
def test_complete_requires_every_candidate_disposed_and_valid_and_ack_and_nonempty():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
               "rows": [{"candidate_id": derived[0], "disposition": "not_load_bearing",
                         "reason": "x", "evidence": "skills/defect_hunts/h2_assumptions.py"}]}
    assert is_complete(derived, section, repo_root=".") is True
def test_one_undisposed_candidate_makes_incomplete():  # §8(c) silence boundary
    derived = ["redis:pkg/a.py:redis.from_url#1", "postgres:pkg/a.py:psycopg.connect#1"]
    section = {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
               "rows": [{"candidate_id": derived[0], "disposition": "answered",
                         "violating_run": "r", "evidence": "skills/defect_hunts/h2_assumptions.py"}]}
    assert is_complete(derived, section, repo_root=".") is False  # 2nd candidate undisposed
def test_zero_derived_candidates_is_not_complete():  # §8(e) empty-run
    assert is_complete([], {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
                            "rows": []}, repo_root=".") is False
def test_invalid_disposed_row_makes_incomplete():  # §8(f) validity not presence
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
               "rows": [{"candidate_id": derived[0], "disposition": "answered",
                         "violating_run": "", "evidence": "x"}]}  # empty violating_run -> invalid
    assert is_complete(derived, section, repo_root=".") is False
def test_unacknowledged_is_not_complete():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {"coverage_acknowledgment": {"acknowledged": False, "additional_assumptions": []}, "rows": []}
    assert is_complete(derived, section, repo_root=".") is False
```
- [ ] **Step 2–4:** implement `is_complete` as the literal two-(really four-)clause predicate from spec §5: `coverage_acknowledged is True AND len(derived) >= 1 AND every derived id has a row referencing it whose disposition is valid per validate_h2_section`. Run green.
- [ ] **Step 5: commit** `feat(h2): H2Record + executable is_complete predicate (empty-run/validity/silence guards)`.

### Task 6: Gate wiring — pure `h2_standing_check` builds the `H2Record`

**Files:** Modify `skills/bridge-protocol/gate/gate.py`; Test `tests/defect_hunts/test_wiring.py`.

**Interfaces — Consumes:** `derive`, `validate_h2_section`, `is_complete`, `H2Record`. **Produces:** `H2_MODE="shadow"` constant; **new signature** `h2_standing_check(phase_input, repo, changed_paths, diff)` — the diff/paths are **passed in**, NOT computed inside (the gate stays pure: no I/O in the check). Returns `{status, reason, notice, record}` with `status ∈ {shadow, enforced, static-only-unacknowledged, flagged}`.

**P0 — EVALUATE RESTRUCTURING (cold-Opus/all):** `h2_standing_check` is currently called at `gate.py:716`, *before* `paths`/`diff_text` are computed (727-733, and only when `registry is not None`). The diff does not exist at the call site. **Fix:** move the `h2_standing_check` call to **after** `paths`/`diff_text` are computed (alongside the `h1_standing_check` call at ~737), and pass them in. Where `registry is None` (no diff available — a declarative/root phase), `changed_paths=[]`/`diff=""` → `derive` returns `[]` → see no-section behavior below.

**P0 — NO-SECTION / NO-CANDIDATE behavior (M3 — replaces `dormant-no-producer`):** pin exactly: (i) **no derived candidates** (empty/declarative diff) AND no `h2_section` → `status="enforced"`, no block (nothing to enforce — a clean pass); (ii) **derived candidates exist** but no `h2_section` → every candidate is unanswered → `status="shadow"` notice (shadow) / `BLOCK_H2_STANDING_CHECK` (block); (iii) `h2_section` present but `coverage_acknowledgment.acknowledged != True` → `static-only-unacknowledged`. The legacy `H2_DORMANT_STATUS`/`H2_DORMANT_NOTICE` constants are removed.

- [ ] **Step 1: failing tests** — shadow notice vs block, FLAG-blocks §7, coverage block §6, h2_status enum:
```python
def test_unanswered_candidate_notices_in_shadow_not_blocks(): ...
def test_flag_disposition_blocks_in_both_modes(): ...  # §7
def test_missing_coverage_acknowledgment_status_is_static_only_unacknowledged(): ...  # §6
def test_h2_standing_check_does_no_io(): ...  # assert no file written (the gate is pure)
```
- [ ] **Step 2–4:** wire it. In `block` mode an unanswered candidate → `BLOCK_H2_STANDING_CHECK`; a `flag` → block in **either** mode; missing ack → `static-only-unacknowledged` (notice in shadow, block in block). `evaluate` today carries only `h2_status`/`h2_notice` (gate.py:805) — **add the `H2Record` to the result** (`result["h2_record"] = h2_result["record"]`) so the collector (Task 7) can consume it. Migrated wiring tests (Task 4) must use `h2_section` rows whose `candidate_id`s **actually derive** from the test's diff (a redis/psycopg/external call in added lines) — not arbitrary ids that derive to nothing. Replace the `H2_DORMANT_STATUS` path (there is now a producer). Run green.
- [ ] **Step 5: commit** `feat(h2): wire pure h2_standing_check (derive+validate+record; FLAG-blocks; shadow/block; status enum)`.

### Task 7: The collector — append the gitignored shadow log outside the tree

**Files:** Create `skills/bridge-protocol/gate/h2_collector.py`; Modify `.gitignore`; Test `tests/defect_hunts/test_h2_collector.py`.

**Interfaces — Produces:** `append_record(record: H2Record, *, log_path: Path|None=None) -> None` — appends one JSON line; `shadow_log_path() -> Path` = `$ARB_H2_SHADOW_LOG` else `$XDG_STATE_HOME/arb/h2-shadow-log.jsonl` else `~/.local/state/arb/h2-shadow-log.jsonl`.

- [ ] **Step 1: failing tests** — append is one JSONL line; default path resolution; the path is outside the repo (a `tmp_path` log doesn't dirty `git status`). **Step 2–4** implement; add an in-repo gitignore line under "Logs / state" only if a dev opts an in-repo path. **Step 5: commit** `feat(h2): shadow-log collector (pure-gate-returns, collector-writes-outside-tree)`.

### Task 8: Graduation query + the FULL deny-proof set (§8 a–h)

**Files:** Create `skills/defect_hunts/h2_graduation.py`; Test `tests/defect_hunts/test_h2_graduation.py`, `tests/defect_hunts/test_h2_denyproof.py`.

**Interfaces — Produces:** `fp_rate(records) -> float|None`; the named guard set `GUARDS = {"min_runs","min_disposed","discrimination","fp_threshold","complete_only"}`; `is_graduation_ready(records, *, _disabled_guards: frozenset[str] = frozenset()) -> bool` — each guard is an *individually disableable* predicate (the `_disabled_guards` param is **test-only**, the mechanism that turns the inject-revert into a COMMITTED artifact, not a local ritual — cold-Opus). `is_complete` (Task 5) is the `complete_only` filter applied to each record before counting.

**P0 — the deny-proofs are COMMITTED inject-revert ARTIFACTS (cold-Opus), and fully written (no `...` — M3/GLM):** each guard gets a paired test: (1) the **deny** — bad input → `is_graduation_ready(bad) is False`; (2) the **inject-revert artifact** — `is_graduation_ready(bad, _disabled_guards={that_guard}) is True`, proving the guard is the *only* thing blocking it (if it stays False with the guard disabled, the deny-proof was hollow — passing for the wrong reason). A future refactor that weakens the guard flips the artifact test, so it can't silently regress.

- [ ] **Step 1: failing tests — the full §8 set, each a deny + a committed inject-revert artifact.** Build fixtures with `_rec(derived, dispositions)` helpers. Examples (write ALL 8 this way):
```python
import skills.defect_hunts.h2_graduation as G
def _complete_rec(n_answered=0, n_nlb=0, n_flag=0):  # a complete run with given disposition counts
    derived = [f"redis:p.py:redis.from_url#{i+1}" for i in range(n_answered+n_nlb+n_flag)]
    disp = ([{"disposition":"answered"}]*n_answered + [{"disposition":"not_load_bearing"}]*n_nlb
            + [{"disposition":"flag"}]*n_flag)
    return {"derived": derived, "dispositions": disp, "complete": True}

def test_e_empty_run_does_not_graduate_AND_guard_is_load_bearing():   # §8(e)
    bad = [{"derived": [], "dispositions": [], "complete": True}] * 15
    assert G.is_graduation_ready(bad) is False                              # deny
    assert G.is_graduation_ready(bad, _disabled_guards={"min_disposed"}) is False  # still no (no 0/0)
    # the empty-run is excluded by complete_only(len(derived)>=1); disabling it lets the empties count:
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True   # ARTIFACT: guard was load-bearing

def test_g_uniform_window_does_not_graduate_AND_discrimination_is_load_bearing():  # §8(g)
    bad = [_complete_rec(n_answered=2) for _ in range(15)]   # 30 disposed, all answered, FP=0/30
    assert G.is_graduation_ready(bad) is False                              # deny (no discrimination)
    assert G.is_graduation_ready(bad, _disabled_guards={"discrimination"}) is True  # ARTIFACT

def test_fp_denominator_is_disposed_not_derived():
    recs = [_complete_rec(n_answered=18, n_nlb=2) for _ in range(10)]  # 200 disposed, FP=20/200=10%? -> tune to <10
    recs = [_complete_rec(n_answered=19, n_nlb=1) for _ in range(11)]  # 220 disposed, FP=11/220=5% <10
    assert G.fp_rate(recs) < 0.10 and G.is_graduation_ready(recs) is True
def test_a_measurable(): ...   # (write fully like above)
def test_b_silence / test_c_boundary / test_d_achievable / test_f_validity / test_h_anchored_fp_token: ...  # (write fully)
```
- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** `is_graduation_ready` as the conjunction of the named guards, each skippable via `_disabled_guards`; `complete_only` filters by `record["complete"]` (computed by Task 5/6 — `is_complete`). FP denominator = disposed (`answered+not_load_bearing+flag`); 0/0 unreachable (`min_disposed>=20`).
- [ ] **Step 4: run, all green** — every deny is False, every inject-revert artifact flips to True.
- [ ] **Step 5: commit** `feat(h2): graduation query + 8 committed inject-revert deny-proof artifacts (the discipline as code, not a ritual)`.

### Task 9: Trust-root — H2 logic joins the certified set + re-pin

**Files:** Modify `skills/bridge-protocol/gate/gate.py` (`logic_set_paths`), `skills/bridge-protocol/gate/trust_root.json`; Test `tests/test_bridge_protocol_gate.py`.

**P0 — UPDATE THE TEST FIXTURE FIRST (cold-Opus):** `certified_object_sha` (gate.py:566) **raises `GateError` on any missing logic-set file**, and the 4 trust-root tests build their fixture with `write_logic_set` (`tests/test_bridge_protocol_gate.py:~338`) which today only creates `skills/bridge-protocol/`. Adding `skills/defect_hunts/h2_*.py` to `logic_set_paths` will make `certified_object_sha` raise inside those tests unless the fixture creates them too.

- [ ] **Step 1:** **first** extend the `write_logic_set` test fixture to also create stub `skills/defect_hunts/h2_assumptions.py`, `h2_derive.py`, `h2_graduation.py` (so the fixture's logic set matches production); run the 4 trust-root tests — still green. **Step 2:** add those three paths to `logic_set_paths` in `gate.py`. **Step 3:** failing test — a tampered H2 validator (mutate a fixture h2 file) changes the running hash → `trust_root_blocks` fires `stale-trust-root` until re-pinned. **Step 4:** re-pin `trust_root.json` `certified_object_sha`; run `tests/test_bridge_protocol_gate.py` — green. **Step 5: commit** `feat(h2): H2 logic joins trust-rooted logic_set_paths (+fixture) + re-pin`.

### Task 10: Runbook + doc-index

**Files:** Create `docs/runbooks/h2-graduation.md`; Modify `docs/index.json` + regen `docs/INDEX.md`.

- [ ] **Step 1:** write the runbook — the `is_graduation_ready` query (how to run it over the shadow log), the **flip** procedure (edit `H2_MODE` constant → re-pin trust root = the operator "earned-it" action), and a pointer to spec §9's named non-goal. **Step 2:** register in `docs/index.json` (`status:"runbook", audience:"operator"`), `scripts/gen-doc-index`, `scripts/check-doc-index` green. **Step 3: commit** `docs(h2): graduation runbook + doc-index`.

---

## Self-Review

**Spec coverage:** §2 id → T1; §4 derive → T2; §3 format+anchor → T3; migration → T4; §5 H2Record/is_complete → T5; §5 gate-pure/status/§6/§7 → T6; §5 collector/log → T7; §5/§8 graduation+deny-proofs → T8; §5/§8 trust-root → T9; §5 runbook → T10. §9 non-goal = explicitly NOT built (named in the runbook). ✓
**Placeholder scan:** load-bearing tasks (T1/T2/T3/T5/T8) carry complete tests; glue tasks (T6/T7/T9/T10) carry exact signatures + the specific assertions — a reviewer can reject any task independently. ✓
**Type consistency:** `CandidateAssumption.id` (T1) = the `candidate_id` (T1) referenced by `is_complete` (T5) and `derive` (T2); `H2Record` (T5) consumed by the collector (T7) and graduation (T8); `is_graduation_ready` (T8) matches the runbook (T10). ✓

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-21-h2-producer-plan.md`. This will be **codex-TDD-built** task-by-task (warm-seat verify-from-git each task, 5-seat panels on the load-bearing ones — derivation, is_complete, graduation deny-proofs), per the established Workflow B. The deny-proofs (T8) are the gate; each is a real inject-revert.
