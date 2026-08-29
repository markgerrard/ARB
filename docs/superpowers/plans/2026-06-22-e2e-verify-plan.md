# e2e-verify (H2 first surface) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the e2e-verify spine (H2-coupled) + the H2 producer Level-B E2E surface — the
empirical real-boundary gate that runs the real H2 producer end-to-end and catches what the
analytical panel can't see — per spec `docs/superpowers/specs/2026-06-22-e2e-verify-design.md`.

**Architecture:** Each corpus case (`{files, diff, changed_paths, phase_input, expected}`) is driven
through the REAL gate seam: seed a tmpdir with `files` → call real `gate.h2_standing_check` (does a
real disk read via `_h2_candidate_files`) → append `result["record"]` via real
`h2_collector.append_record` → parse the JSONL back (harness glue; no producer reader) → call real
`h2_graduation.is_graduation_ready`/`fp_rate`. Real for everything the producer does; supplied only
for `diff`/`changed_paths`/`phase_input`. A guard-deletion mutation layer + a runtime boundary-honesty
canary keep the suite honest. Everything behind a `-m e2e` pytest marker.

**Tech Stack:** Python 3, pytest (markers, fixtures, subprocess), the existing `skills/defect_hunts/`
+ `skills/bridge-protocol/gate/` modules (real, never mocked).

## Global Constraints
- BUILD SCOPE ONLY: §6 H2 surface + the H2-coupled spine + the 3 deny-proofs + the `-m e2e` marker.
  DESIGN-ONLY, DO NOT BUILD: the §7 flip-gate; the UI/memory/API surfaces.
- Real boundary, never mocked: `_h2_candidate_files` (read), `append_record` (write),
  `is_graduation_ready`/`fp_rate` (graduation). Supplied-only: `diff`, `changed_paths`, `phase_input`.
- Hermetic: every case in an isolated tmpdir with `ARB_H2_SHADOW_LOG`, `XDG_STATE_HOME`, `HOME`
  redirected; no network, no git (allowlist the mutation subprocess).
- Existing tests stay green. The e2e suite runs only under `pytest -m e2e`.
- Each deny-proof is a real inject-revert, mutation-verified (delete the guard clause → the proof reds).
- Real symbol signatures (verified against source — use exactly):
  - `gate.h2_standing_check(phase_input: dict, repo: str|Path, changed_paths: list[str], diff: str)
    -> {"status","reason","notice","record": H2Record}`; status ∈
    `{shadow, enforced, flagged, static-only-unacknowledged}`.
  - `H2Record(run_id, h2_mode, derived: list[str], dispositions: list[dict], coverage_acknowledged: bool, complete: bool)`.
  - `h2_collector.append_record(record, *, log_path: Path|None=None) -> None` (writes
    `json.dumps(_record_payload(record), sort_keys=True)` + newline); `h2_collector.shadow_log_path() -> Path`.
  - `h2_graduation.is_graduation_ready(records, *, _disabled_guards=frozenset()) -> bool`;
    `h2_graduation.fp_rate(records) -> float|None`; `h2_graduation.GUARDS` =
    `{min_runs, min_disposed, discrimination, fp_threshold, complete_only}`.
  - `h2_assumptions.is_complete(derived, section, *, repo_root)`; its guards: coverage-acknowledged,
    derived≥1, validity (`validate_h2_section`), rows⊆derived, uniqueness, every-derived-has-a-row.
- Terminal state: **merge-hold for Mark's →dev review.** No autonomous merge to dev.

## Plan-panel revisions (BINDING — apply these to the tasks below; from codex + cold-Opus)

These supersede the task bodies where they conflict. They fix two hollowness traps (a read-proof and
a deny-proof that would pass without exercising the real thing) — the exact defect class this slice
exists to prevent — plus two ordering/mechanism P0s.

- **R1 (P0, ordering):** Build the **runner before the deny-proofs.** Renumber: **Task 10 = runner**
  (was 11), **Task 11 = the three deny-proofs** (was 10). The deny-proofs consume the runner's
  classification, so it must exist first.
- **R2 (P0, `complete_only` mutation recipe):** `complete_only` is NOT a deletable dict-entry line —
  `is_graduation_ready` implements it as `window = records_list if "complete_only" in disabled_guards
  else _complete_records(records_list)`. Task 8's mutation engine must support a **per-guard edit
  recipe** (not single-line-delete): for `complete_only`, the mutation replaces the window selection
  with the unfiltered `records_list` (i.e. *force* `_complete_records`-bypass) so the function stays
  runnable and the paired case reds on the *intended* (incomplete-record-counted) mechanism, not a
  NameError. The other four `GUARDS` (`min_runs`/`min_disposed`/`discrimination`/`fp_threshold`) use
  the dict-entry deletion. The registry entry carries the recipe kind per guard.
- **R3 (P1, read-boundary proof is HOLLOW — fix Task 3 + Task 6):** `run_case` must NOT return
  `read_files = list(case["files"].keys())` (that's the *supplied* keys — a mocked reader passes).
  Instead prove the read by an outcome that depends on the **seeded file BYTES**: the derived
  candidate ids must be derivable only from the tmp-file content (e.g. assert each expected derived
  id corresponds to a call expression that appears in the *seeded* file at `changed_paths`, read back
  from disk by the harness *separately* from the producer). Task 6's canary additionally asserts
  `_h2_candidate_files` origin (R6). Drop `read_files` as "proof"; replace with `read_evidence` =
  {changed_path → sha256 of the file the harness re-read from the tmp repo}, and assert the producer's
  derived ids are explained by those bytes.
- **R4 (P1, deny-proof #1 classification — fix the runner + Task 11):** the runner's `run_suite` must
  **invoke the boundary-honesty canary per case** and map **canary-failure → `block_unrun_count`
  (miscategorised)** and **expected-surface assertion-failure → `block_fail_count`**, reusing the
  `assert_case_expected(case, out)` helper (R5). `E2EResult.from_counts` already maps any
  `block_unrun>0`→BLOCK_UNRUN and `block_fail>0`→BLOCK_FAIL. Deny-proof #1 (mocked boundary) then
  exercises the REAL mapping (mock the collector → canary fails → runner classifies BLOCK_UNRUN),
  not a bare `pytest.raises`. Mutation-verify: delete the canary-call in the runner → proof #1 reds.
- **R5 (P1, Task 5 asserts the full surface + a shared helper):** factor
  `assert_case_expected(case, out)` (used by Task 5 AND the runner R4). It asserts exact
  `record_payload["derived"]`, the relevant `record_payload["dispositions"]`, `complete`, and the
  graduation counts/window fields per `expected` (spec §6.2 exact-JSONL on fields under test) — not
  just status + derived-subset.
- **R6 (P1, harden `assert_real_symbol` — Task 6):** compare `Path(fn.__code__.co_filename).resolve()`
  against the **real absolute module path** (passed in / resolved from ROOT), AND assert
  `fn.__module__`, in addition to `not isinstance(_, unittest.mock.Mock)`. `endswith(suffix)` alone
  is forgeable (a fake compiled from a path ending in the suffix passes).
- **R7 (P1, define the fixtures — Task 1 conftest):** add `real_gate`, `real_coll`, `real_grad`
  fixtures in `tests/e2e/conftest.py` using the Task-3 `_load` mechanism (gate.py/h2_collector.py via
  `spec_from_file_location`; `h2_graduation` via normal import). Task 6 consumes them.
- **R8 (P1, hermeticity blocks git/subprocess by default — Task 1):** the autouse fixture also blocks
  `subprocess`/`git` by default (wrap `subprocess.run`/`Popen` to raise), with an explicit
  **allowlist** flag the Task-8 mutation runner sets to spawn its one permitted subprocess.
- **R9 (P1, pin `H2_MODE=="shadow"` — Task 1):** add an assertion (in conftest or a dedicated test)
  that the real `gate.H2_MODE == "shadow"`, so corpus `status`/`notice` expectations (computed from
  `H2_MODE` at gate.py:400-424) cannot silently drift. If it ever flips, the e2e suite fails loudly.
- **R10 (P1/P2, duplicate-id evidence anchor MUST be a SEEDED tmp file — Task 4):** the
  `discovered/duplicate-id` case's disposition rows anchor `evidence` at a path **present in the
  seeded `files`** (so it exists in the tmp repo). The existing unit test anchors at a source-tree
  path that won't exist in tmp → `complete=False` would fire for *invalid-anchor* reasons, hollowing
  the **uniqueness** guard that deny-proof #3 and the Task-8 mutation depend on. The case must red
  *for the duplicate*, provable by a control: the same section with ONE row (not two) → `complete=True`.
  (This is the bad-anchor confound that bit the warm seat live during the H2 re-panel.)
- **R11 (P2, misc):** fix the File Structure line for `run_case` to `-> dict` (matches the body); add
  the `changed_paths`↔diff-header consistency helper test to Task 4; either add an
  `enumerated/no-candidate` case (spec §6.3) or mark broader enumerated coverage as deferred in this
  build slice (recommend: add the one no-candidate case; defer the wider trigger matrix with a logged
  residual).

## File Structure
- `tests/e2e/__init__.py` — package marker.
- `tests/e2e/conftest.py` — autouse hermeticity fixture (tmpdir + env redirect + socket/git block + allowlist).
- `tests/e2e/spine.py` — `E2EStatus` enum, `E2EResult` dataclass, detector canary helpers.
- `tests/e2e/h2_harness.py` — Level-B driver `run_case(case: dict, tmp: Path) -> E2EResult`.
- `tests/e2e/corpus.py` — `load_case(path)`, `iter_corpus()`, schema validation.
- `tests/e2e/h2_corpus/{enumerated,discovered,clean}/<id>/case.json` — the corpus.
- `tests/e2e/h2_guard_registry.json` — `{guard_id: {file, locator, paired_case_id}}`.
- `tests/e2e/test_h2_surface.py` — drive every corpus case, assert `expected`.
- `tests/e2e/test_h2_canary.py` — boundary-honesty canary.
- `tests/e2e/test_h2_mutations.py` — guard-deletion mutation layer + registry completeness.
- `tests/e2e/test_h2_graduation.py` — multi-record fixture + per-guard N−1 boundary fixtures.
- `tests/e2e/test_denyproofs.py` — the three committed deny-proofs.
- `tests/e2e/runner.py` — suite runner → `E2EResult` + `e2e_status.json` + exit codes 0/1/2.
- `pyproject.toml` — register `e2e` marker.

---

### Task 1: `-m e2e` marker + hermetic conftest

**Files:** Create `tests/e2e/__init__.py`, `tests/e2e/conftest.py`; Modify `pyproject.toml`.

- [ ] **Step 1: Failing test** `tests/e2e/test_hermetic.py`:
```python
import os, socket, pytest
pytestmark = pytest.mark.e2e
def test_env_redirected_to_tmp(tmp_path):
    assert os.environ["ARB_H2_SHADOW_LOG"].startswith(str(tmp_path.parent.parent)) or "/e2e" in os.environ["ARB_H2_SHADOW_LOG"]
def test_socket_blocked():
    with pytest.raises((OSError, RuntimeError)):
        socket.socket().connect(("1.1.1.1", 80))
```
- [ ] **Step 2: Run** `pytest -m e2e tests/e2e/test_hermetic.py -v` → FAIL (marker unknown / env not set).
- [ ] **Step 3: Implement** `pyproject.toml` add under `[tool.pytest.ini_options]`:
  `markers = ["e2e: real-boundary end-to-end verification (slow; run explicitly)"]`.
  `tests/e2e/conftest.py`:
```python
import os, socket, pytest
from pathlib import Path

@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    state = tmp_path / "state"; state.mkdir()
    monkeypatch.setenv("ARB_H2_SHADOW_LOG", str(state / "h2-shadow-log.jsonl"))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    real_connect = socket.socket.connect
    def _blocked(self, *a, **k): raise RuntimeError("e2e hermetic: network blocked")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    yield
```
  (git/subprocess block is enforced per-test where relevant; the mutation runner in Task 8 uses an
  explicit allowlisted subprocess.)
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): -m e2e marker + hermetic conftest`.

---

### Task 2: spine — `E2EStatus` / `E2EResult` three-state

**Files:** Create `tests/e2e/spine.py`, `tests/e2e/test_spine.py`.
**Produces:** `E2EStatus(PASS|BLOCK_FAIL|BLOCK_UNRUN)`; `E2EResult(status, detail, case_count, passed_count, block_fail_count, block_unrun_count)`; `E2EResult.merges() -> bool`.

- [ ] **Step 1: Failing test:**
```python
import pytest; from tests.e2e.spine import E2EStatus, E2EResult
pytestmark = pytest.mark.e2e
def test_only_pass_merges():
    assert E2EResult(E2EStatus.PASS, "ok", 1,1,0,0).merges() is True
    assert E2EResult(E2EStatus.BLOCK_FAIL, "broke", 1,0,1,0).merges() is False
    assert E2EResult(E2EStatus.BLOCK_UNRUN, "vacuous", 0,0,0,0).merges() is False
def test_zero_case_is_block_unrun():
    assert E2EResult.from_counts(case_count=0, passed=0, block_fail=0, block_unrun=0).status is E2EStatus.BLOCK_UNRUN
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `spine.py`:
```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
class E2EStatus(Enum):
    PASS="pass"; BLOCK_FAIL="block-fail"; BLOCK_UNRUN="block-unrun"
@dataclass
class E2EResult:
    status: E2EStatus; detail: str
    case_count: int; passed_count: int; block_fail_count: int; block_unrun_count: int
    def merges(self) -> bool: return self.status is E2EStatus.PASS
    @classmethod
    def from_counts(cls, *, case_count, passed, block_fail, block_unrun, detail=""):
        if case_count == 0 or block_unrun > 0:
            return cls(E2EStatus.BLOCK_UNRUN, detail or "vacuous/unrun", case_count, passed, block_fail, block_unrun)
        if block_fail > 0:
            return cls(E2EStatus.BLOCK_FAIL, detail or "feature broken", case_count, passed, block_fail, block_unrun)
        return cls(E2EStatus.PASS, detail or "ok", case_count, passed, block_fail, block_unrun)
```
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): spine E2EStatus/E2EResult three-state`.

---

### Task 3: Level-B harness — drive the real seam

**Files:** Create `tests/e2e/h2_harness.py`, `tests/e2e/test_harness.py`.
**Consumes:** `gate.h2_standing_check`, `h2_collector.append_record/shadow_log_path`, `h2_graduation`.
**Produces:** `run_case(case: dict, tmp: Path) -> dict` returning
`{"status", "record_payload", "log_records", "fp_rate", "read_files"}`.

- [ ] **Step 1: Failing test** (hand-built clean case, asserts BOTH real boundaries fired):
```python
import json, pytest; from pathlib import Path
from tests.e2e.h2_harness import run_case
pytestmark = pytest.mark.e2e
CLEAN = {
  "files": {"pkg/a.py": "import os\n"},
  "diff": "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -0,0 +1 @@\n+import os\n",
  "changed_paths": ["pkg/a.py"],
  "phase_input": {"h2_section": {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []}, "rows": []}},
}
def test_seam_crosses_both_boundaries(tmp_path):
    out = run_case(CLEAN, tmp_path)
    assert out["status"] in {"shadow","enforced","flagged","static-only-unacknowledged"}
    assert "pkg/a.py" in out["read_files"]            # real disk READ happened
    assert Path(out["log_path"]).read_text().strip() != ""   # real WRITE happened
    assert isinstance(out["log_records"], list)
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `h2_harness.py`:
```python
from __future__ import annotations
import json, importlib, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root from tests/e2e/h2_harness.py

def _load(modname: str, relpath: str):
    # gate.py + h2_collector.py live under skills/bridge-protocol/ (hyphen → not a normal package).
    # Mirror tests/test_bridge_protocol_gate.py:14-18 exactly.
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod)
    return mod

def run_case(case: dict, tmp: Path) -> dict:
    gate = _load("bridge_protocol_gate", "skills/bridge-protocol/gate/gate.py")
    coll = _load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")
    grad = importlib.import_module("skills.defect_hunts.h2_graduation")  # defect_hunts is a normal package
    repo = tmp / "repo"; repo.mkdir(parents=True, exist_ok=True)
    for rel, content in case["files"].items():
        p = repo / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, encoding="utf-8")
    result = gate.h2_standing_check(case["phase_input"], repo, case["changed_paths"], case["diff"])
    log_path = coll.shadow_log_path()
    coll.append_record(result["record"], log_path=log_path)
    records = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {"status": result["status"], "record_payload": records[-1] if records else None,
            "log_records": records, "log_path": str(log_path),
            "fp_rate": grad.fp_rate(records), "read_files": list(case["files"].keys())}
```
  Import mechanism VERIFIED against `tests/test_bridge_protocol_gate.py:14-18`. `h2_graduation` is a
  normal package import (`skills/defect_hunts/` is not hyphenated). Do NOT stub any module.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): Level-B harness drives the real H2 seam`.

> **Builder note (load-bearing):** confirm the real import path for `gate.py`/`h2_collector.py`
> (hyphenated dir) by grepping how `tests/test_bridge_protocol_gate.py` imports them, and reuse that
> exact mechanism. Do NOT stub the modules.

---

### Task 4: corpus loader + seed cases (incl. duplicate-id)

**Files:** Create `tests/e2e/corpus.py`, `tests/e2e/h2_corpus/clean/001-test-only/case.json`,
`tests/e2e/h2_corpus/discovered/duplicate-id/case.json`,
`tests/e2e/h2_corpus/enumerated/redis-from-url/case.json`, `tests/e2e/test_corpus.py`.
**Produces:** `iter_corpus() -> Iterator[tuple[str, dict]]`, `load_case(path) -> dict`, `validate_case(case)`.

- [ ] **Step 1: Failing test:**
```python
import pytest; from tests.e2e.corpus import iter_corpus, validate_case
from tests.e2e.h2_harness import run_case
pytestmark = pytest.mark.e2e
def test_all_cases_valid():
    cases = dict(iter_corpus()); assert cases
    for cid, c in cases.items(): validate_case(c)  # raises on bad schema
def test_duplicate_id_excluded_from_window(tmp_path):
    case = dict(iter_corpus())["discovered/duplicate-id"]
    out = run_case(case, tmp_path)
    assert out["record_payload"]["complete"] is False         # malformed → incomplete
    # complete=False ⇒ excluded from the graduation WINDOW (not denominator)
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `corpus.py` (glob `h2_corpus/**/case.json`,
  json-load, validate required keys `files,diff,changed_paths,phase_input,expected`); write the 3
  `case.json` files. The `discovered/duplicate-id` case: `files` with one `redis.from_url` call,
  `phase_input.h2_section.rows` = TWO valid disposition rows for the SAME derived candidate id
  (mirror `tests/defect_hunts/test_h2_record.py::test_duplicate_disposed_candidate_makes_incomplete`),
  `expected.record.complete=false`. The `enumerated/redis-from-url` case: a diff adding a module-level
  `redis.from_url(...)`, `expected.derived` contains the redis candidate id, `phase_input` answers it.
  The `clean/001-test-only` case: a test-only diff, `expected.derived=[]`.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): corpus loader + seed cases (clean/discovered:duplicate-id/enumerated)`.

---

### Task 5: `test_h2_surface` — assert every case's `expected`

**Files:** Create `tests/e2e/test_h2_surface.py`.
- [ ] **Step 1: Failing test:** parametrize over `iter_corpus()`; for each, `run_case`, assert
  `out["status"] == case["expected"]["status"]`, `set(case["expected"]["derived"]) <= set(out["record_payload"]["derived"])`,
  and `out["record_payload"]["complete"] == case["expected"]["record"]["complete"]`.
- [ ] **Step 2: Run** → FAIL (until impl matches). **Step 3:** fix any case `expected` mismatch by
  running the real seam and recording the real values (the corpus is ground truth authored OUTSIDE
  the producer — set `expected` from the spec'd behaviour, not by copying producer output blindly;
  if they differ, that's a finding to record). **Step 4: Run** → PASS. **Step 5: Commit**
  `feat(e2e): test_h2_surface asserts corpus expectations through the real seam`.

---

### Task 6: boundary-honesty canary

**Files:** Create `tests/e2e/test_h2_canary.py`; extend `spine.py` with
`assert_real_symbol(func, module_suffix)`.
- [ ] **Step 1: Failing test:**
```python
import unittest.mock as m, pytest
from tests.e2e import spine
pytestmark = pytest.mark.e2e
def test_symbols_are_real(real_gate, real_coll, real_grad):  # fixtures resolve the real modules
    for fn, suffix in [(real_gate.h2_standing_check,"gate.py"), (real_gate._h2_candidate_files,"gate.py"),
                       (real_coll.append_record,"h2_collector.py"), (real_coll.shadow_log_path,"h2_collector.py"),
                       (real_grad.is_graduation_ready,"h2_graduation.py")]:
        spine.assert_real_symbol(fn, suffix)   # __module__/co_filename/not Mock
def test_canary_trips_on_mock(real_coll):
    with pytest.raises(AssertionError):
        spine.assert_real_symbol(m.Mock(), "h2_collector.py")
def test_both_side_effects(tmp_path):
    from tests.e2e.corpus import iter_corpus; from tests.e2e.h2_harness import run_case
    out = run_case(dict(iter_corpus())["enumerated/redis-from-url"], tmp_path)
    assert out["read_files"]                              # READ boundary
    assert out["log_records"]                             # WRITE boundary
    assert out["record_payload"]["derived"]              # read produced real candidates
```
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `assert_real_symbol`:
```python
import unittest.mock, inspect
def assert_real_symbol(func, module_suffix):
    assert not isinstance(func, unittest.mock.Mock), "symbol is a Mock"
    fn = inspect.unwrap(func)
    assert fn.__code__.co_filename.endswith(module_suffix), f"{fn.__code__.co_filename} not {module_suffix}"
```
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): boundary-honesty canary (origin + both side-effects)`.

---

### Task 7: guard registry + completeness test

**Files:** Create `tests/e2e/h2_guard_registry.json`, `tests/e2e/test_registry_complete.py`.
- [ ] **Step 1: Failing test:** assert registry guard_ids cover exactly the `h2_graduation.GUARDS`
  set plus the named `is_complete` guards (`coverage_ack, derived_nonempty, validity, rows_subset,
  uniqueness, every_derived_has_row`); fail if codebase has a guard with no registry entry.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `h2_guard_registry.json` mapping each guard_id to
  `{"file": "...", "locator": "<unique source substring of the clause>", "paired_case_id": "..."}`;
  completeness test imports `h2_graduation.GUARDS` and a hardcoded `IS_COMPLETE_GUARDS` list and
  asserts set-equality with the registry keys.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): guard registry + completeness test`.

---

### Task 8: guard-deletion mutation layer (subprocess)

**Files:** Create `tests/e2e/test_h2_mutations.py`, `tests/e2e/_mutate.py`.
- [ ] **Step 1: Failing test:** parametrize over registry; for each guard: copy the real source tree
  to a tmp dir, delete the `locator` line in the target file, run the `paired_case_id` case via a
  **subprocess** whose `PYTHONPATH` points at the mutated copy, assert the case REDs on the intended
  assertion (capture the subprocess's failing assertion text — not just nonzero exit). Then restore
  (tmp copy discarded) and assert the case greens against the real tree.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** `_mutate.py` (copytree, locator-delete, write a
  tiny driver script that imports from the mutated copy + runs the one case + prints the assertion
  result as JSON) + the subprocess invocation (allowlisted per Task 1). Assert minimal-pair: the
  case reds only for ITS guard, not others (spot-check 1 cross pair).
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): guard-deletion mutation layer (subprocess, intended-failure)`.

---

### Task 9: graduation multi-record + per-guard N−1 fixtures

**Files:** Create `tests/e2e/test_h2_graduation.py`.
- [ ] **Step 1: Failing test:** a ≥10-record green fixture → `is_graduation_ready(records)` True;
  then per-guard N−1: `min_runs` (9 records → False), `min_disposed` (<20 disposed → False),
  `discrimination` (all-uniform → False), `fp_threshold` (fp≥0.10 → False), `complete_only`
  (an incomplete record present → that record excluded; if it pushes below floor → False). Each
  asserts the guard BITES at the off-by-one (with-guard False / loosened True via `_disabled_guards`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the synthesized-record builders (dicts matching
  `_record_payload` shape: `complete`, `dispositions:[{disposition:...}]`). Use the real
  `is_graduation_ready(..., _disabled_guards=...)` hook to show each guard is the one biting.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(e2e): graduation multi-record + per-guard N−1 boundary fixtures`.

---

### Task 10: the three committed deny-proofs

**Files:** Create `tests/e2e/test_denyproofs.py`.
- [ ] **Step 1: Failing test:**
  1. **mocked-boundary → block-unrun(miscategorised):** build a harness variant whose collector is a
     Mock; assert the canary (`assert_real_symbol`) fails → the runner classifies it `BLOCK_UNRUN`
     (miscategorised), NOT pass.
  2. **unrunnable / zero-case → block-unrun:** run the runner over an EMPTY corpus subset → `E2EResult`
     status `BLOCK_UNRUN` (case_count 0). Mutation: delete the `case_count==0` clause in
     `spine.from_counts` → this proof reds (confirm both directions).
  3. **real-breakage → block-fail:** take a corpus case whose `expected` says caught, inject a real
     producer break (run against a copy with the `is_complete` uniqueness clause deleted so the
     duplicate-id case is wrongly `complete=True`) → the surface assertion fails → `BLOCK_FAIL`.
     Mutation-verified (restoring the clause greens it).
- [ ] **Step 2–4:** implement, run → PASS; for proofs (2) and (3) run the guard-deletion both
  directions and record it in the commit. **Step 5: Commit**
  `feat(e2e): three committed deny-proofs (miscategorised/unrun/breakage), mutation-verified`.

---

### Task 11: runner + `e2e_status.json` + exit codes

**Files:** Create `tests/e2e/runner.py`, `tests/e2e/test_runner.py`.
- [ ] **Step 1: Failing test:** `run_suite(corpus_subset) -> E2EResult`; writes `e2e_status.json`
  (status + counts); a thin `main()` exits 0/1/2 for pass/block-fail/block-unrun. Test asserts a
  green subset → exit 0 + status file `pass`; an empty subset → exit 2 + `block-unrun`.
- [ ] **Step 2–4:** implement `run_suite` (iterate corpus, run_case, tally counts → `E2EResult.from_counts`),
  write JSON, map status→exit code. → PASS. **Step 5: Commit** `feat(e2e): suite runner + e2e_status.json + exit codes`.

---

## Self-Review
- **Spec coverage:** §6.1 seam → T3; §6.2 corpus+phase_input → T4; §6.3 categories → T4; §6.4
  graduation+N−1 → T9; §6.5 mutation+registry → T7/T8; §6.6 canary → T6; §6.7 hermeticity+exit codes
  → T1/T11; §3 three-state → T2; §12 three deny-proofs → T10; `-m e2e` → T1. §7 flip-gate + UI/memory/API:
  correctly ABSENT (design-only).
- **Placeholder scan:** the `_import_gate` helper in T3 is the one under-specified spot — flagged with
  a builder note to mirror the existing gate-import mechanism (verify against `tests/test_bridge_protocol_gate.py`).
- **Type consistency:** `E2EResult`/`E2EStatus` (T2) used consistently in T10/T11; `run_case` return
  keys consistent T3→T4/T5/T6; `H2Record` payload fields match source.

## Execution Handoff
Subagent-driven (codex-TDD per task, warm-seat verify-from-git), then panel review, then merge-hold.
