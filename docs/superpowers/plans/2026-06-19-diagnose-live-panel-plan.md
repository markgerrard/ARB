# diagnose/diagnose-steer LIVE PANEL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stubbed diagnose/diagnose-steer panel (hardcoded `SEATS`, synthetic `generate_blind_candidates`, attested `clean_scribe_context`) with a real, contamination-proof-by-construction 3-model decorrelated panel + isolated scribe.

**Architecture:** skill-authors-and-seals — the skill's deterministic extraction composes and seals every seat/scribe brief (canonical env-free byte-form) BEFORE the orchestrator sees it; the orchestrator forwards opaque sealed envelopes verbatim via the bridge; the gate verifies by RECOMPUTATION (not hash-attestation) on two named determinism bases. The failing test is run ONCE under a fail-closed containment envelope to derive the traceback. Confidence corrected to noisy-OR.

**Tech Stack:** Python stdlib + `jsonschema` (declared). Bridge `scripts/agent-dispatch` for seats. Implements `docs/superpowers/specs/2026-06-19-diagnose-live-panel-design.md`.

## Global Constraints

- Python stdlib + `jsonschema>=4` only (no other new deps).
- TDD: failing test first; every dogfood fires END-TO-END from a real run (no fixture-recognizer); test records are schema-conformant via `assert_run_record_conformant`.
- Read-only diagnosis: no code is applied. The ONE execution is the contained single test run (Task 1/2).
- `_diagnose_common` neutrality guard still holds: no steer symbol/reason in common (steer logic stays in diagnose-steer).
- Fail-closed everywhere: any unmet invariant → `verified:false / harness_only:true` with a specific `blocking_real_use` reason, never a partial/degraded green.
- Commit per task on the worker's worktree branch; report SHA + pytest. The orchestrator integrates (merge, gate-onboard).
- Block reasons added this plan: `brief-tampered`, `brief-not-skill-authored`, `submission-inconsistent`, `unverified-without-panel`. Fail-loud `blocking_real_use` values: `test-containment-unavailable`, `test-execution-timeout`, `test-nonreproduction`, `incomplete-panel`, `bridge-unavailable`.

---

## File Structure

- `skills/_diagnose_common/canonical.py` (NEW) — `canonical_bytes(obj)` + `seal(obj)`; the env-free normalization + content hash both skill and gate use. One responsibility: deterministic byte-form.
- `skills/_diagnose_common/neutral_validators.py` (MODIFY) — add the validator-side fail-loud (`unverified-without-panel`) + the recompute-comparison helper used by the gate.
- `skills/diagnose/containment.py` (NEW) — `run_contained(argv, cwd, timeout) -> ContainedResult`; the §7 execution envelope behind one interface (mechanism chosen by Task 0 spike). One responsibility: bounded test execution.
- `skills/diagnose/extraction.py` (MODIFY) — `derive_scope` consumes `recorded_traceback` instead of `error_log`.
- `skills/diagnose/dispatch.py` (MODIFY) — trigger = `{failing_test}` node-id; drop `error_log`.
- `skills/diagnose/briefs.py` (NEW) — `author_briefs(failing_test, repo_sha, recorded_traceback, constants)` (pre-response) + `author_post_briefs(constants, sealed_submissions)` (post-response). Pure functions; the authorship boundary.
- `skills/diagnose/panel.py` (NEW) — the dispatch orchestration: seal → forward-opaque via bridge → independent-phase collation → fail-loud. One responsibility: live-panel lifecycle.
- `skills/diagnose/diagnose.py` (MODIFY) — `run_diagnose` wires Tasks 1–7; remove the stub `SEATS`/`generate_blind_candidates`.
- `skills/diagnose/panel_constants.json` (NEW) — committed gate-constants (roster, role rule, certifier rule, scribe template, collation order).
- `skills/diagnose-steer/steer_validators.py` (MODIFY) — noisy-OR Q.
- `skills/diagnose-steer/diagnose_steer.py` (MODIFY) — steer brief sealed+recomputed; live panel.
- `skills/bridge-protocol/gate/gate.py` (MODIFY) — `brief-not-skill-authored` recompute check + `brief-tampered`.
- Tests: `tests/test_diagnose_common.py`, `tests/test_diagnose.py`, `tests/test_diagnose_steer.py`, `tests/test_diagnose_containment.py` (NEW), `tests/test_bridge_protocol_gate.py` (gate check).

---

## Task 0 — SPIKE: containment mechanism (precedes Task 1; has a pass-bar)

**Files:**
- Create: `skills/diagnose/containment.py`
- Test: `tests/test_diagnose_containment.py`

**Interfaces:**
- Produces: `run_contained(argv: list[str], cwd: str, timeout_s: float) -> ContainedResult` where `ContainedResult = {"reproduced": bool, "returncode": int|None, "stdout": str, "stderr": str, "timed_out": bool, "contained": bool}`. `contained=False` means the mechanism could not enforce all five invariants on this host (→ caller fail-closes with `test-containment-unavailable`).

**Spike goal:** choose the mechanism (macOS `sandbox-exec` profile vs container) that DEMONSTRABLY delivers all five invariants on the fleet, proven by escape-tests. The mechanism is chosen because an attempted escape fails closed, NOT by preference.

**SPIKE RESULT (2026-06-19, executed on the fleet — PASSED via `sandbox-exec`; no Docker needed):** all five invariants demonstrated against real escapes (egress→`PermissionError`; write-outside→blocked+no-leak, write-inside→ok; hang+child→`killpg` reaps, 0 survivors). Four gotchas the implementation MUST encode (learned empirically — getting any wrong silently breaks containment OR causes false non-reproduction):
1. **Profile is `(allow default)` minus `(deny network*)` + write-confinement — NOT `(deny default)`.** A deny-default profile breaks the interpreter's own startup (Homebrew python hung on egress; system python exited rc=72 with no output) → every test false-non-reproduces. Allow-default-minus-the-two-things-we-care-about preserves normal execution while containing.
2. **The work-dir `subpath` MUST be `os.path.realpath`-resolved** (`/private/tmp/…`, not `/tmp/…`). `/tmp` is a symlink; `sandbox-exec` matches realpaths, so an unresolved `/tmp` subpath denies *legitimate* writes-inside → false non-reproduction.
3. **Also `(allow file-write* (subpath "/private/var/folders"))`** — the macOS interpreter tmp; without it pytest/python tempfile writes are denied.
4. **Wall-clock timeout + reaping is in PYTHON** (`subprocess.communicate(timeout=)` + `os.setsid`/`start_new_session=True` then `os.killpg(SIGKILL)`) — there is no `timeout` binary on macOS (`gtimeout` only if coreutils installed; don't depend on it).

**Attested residual (record in spec §7):** under `(allow file-read*)` the contained test CAN read host files (incl. secrets) but CANNOT exfiltrate (network denied + writes confined) — read-but-can't-exfil is the posture; acceptable for a failing-test run.

- [ ] **Step 1: Write the escape-test suite (failing first)**

```python
# tests/test_diagnose_containment.py
import textwrap, sys
from pathlib import Path
from skills.diagnose.containment import run_contained

def _script(tmp_path, body):
    p = tmp_path / "t.py"; p.write_text(textwrap.dedent(body)); return [sys.executable, str(p)]

def test_network_egress_blocked(tmp_path):
    r = run_contained(_script(tmp_path, """
        import socket
        socket.create_connection(("1.1.1.1", 53), timeout=3)
    """), cwd=str(tmp_path), timeout_s=10)
    # egress attempt must NOT succeed: either contained=False (host can't sandbox) or the call failed
    assert (not r["contained"]) or (r["returncode"] != 0)

def test_write_outside_confine_fails(tmp_path):
    r = run_contained(_script(tmp_path, """
        open("/Users/<user>/.diag_escape_probe", "w").write("x")
    """), cwd=str(tmp_path), timeout_s=10)
    assert (not r["contained"]) or (r["returncode"] != 0)
    assert not Path("/Users/<user>/.diag_escape_probe").exists()

def test_hanging_test_and_children_reaped(tmp_path):
    r = run_contained(_script(tmp_path, """
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        time.sleep(120)
    """), cwd=str(tmp_path), timeout_s=3)
    assert r["timed_out"] is True
    # no orphaned child: give reaping a moment, then assert none of our sleepers survive
    import subprocess as sp
    out = sp.run(["pgrep", "-f", "time.sleep(120)"], capture_output=True, text=True)
    assert out.stdout.strip() == ""
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_diagnose_containment.py -v` → FAIL (`run_contained` undefined).

- [ ] **Step 3: Implement `run_contained` with the spike-chosen mechanism**

Spike both, keep the one that passes all escape-tests. Skeleton (process-group reaping is mechanism-independent; the sandbox wrapper is the spike variable):

```python
# skills/diagnose/containment.py
from __future__ import annotations
import os, signal, subprocess, shutil

def _sandbox_wrapper(argv: list[str], cwd: str) -> tuple[list[str], bool]:
    """Return (wrapped_argv, contained). SPIKE-VALIDATED mechanism = sandbox-exec with an
    ALLOW-DEFAULT-minus-{network,writes-outside} profile (deny-default breaks interpreter startup).
    The work-dir subpath MUST be realpath-resolved (/tmp is a symlink). If sandbox-exec is absent,
    return (argv, False) so the caller fail-closes (test-containment-unavailable)."""
    sbx = shutil.which("sandbox-exec")
    if not sbx:
        return (argv, False)
    work = os.path.realpath(cwd)
    profile = (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        "(deny file-write*)"
        f'(allow file-write* (subpath "{work}"))'
        '(allow file-write* (subpath "/private/var/folders"))'
        '(allow file-write* (subpath "/dev"))'
    )
    return ([sbx, "-p", profile, "--", *argv], True)

def run_contained(argv: list[str], cwd: str, timeout_s: float) -> dict:
    wrapped, contained = _sandbox_wrapper(argv, cwd)
    if not contained:
        return {"reproduced": False, "returncode": None, "stdout": "", "stderr": "",
                "timed_out": False, "contained": False}
    proc = subprocess.Popen(wrapped, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)  # own process group for reaping
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return {"reproduced": proc.returncode != 0, "returncode": proc.returncode,
                "stdout": out, "stderr": err, "timed_out": False, "contained": True}
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # reap test + children
        proc.communicate()
        return {"reproduced": False, "returncode": None, "stdout": "", "stderr": "",
                "timed_out": True, "contained": True}
```

- [ ] **Step 4: Run escape-tests to verify they pass** — `pytest tests/test_diagnose_containment.py -v` → PASS. If `sandbox-exec` cannot enforce an invariant on the fleet, the spike escalates to the container variant; if neither, `contained` stays `False` and the caller fail-closes (Task 1 Step covers that path). Record the chosen mechanism in `containment.py`'s module docstring.

- [ ] **Step 5: Commit** — `git add skills/diagnose/containment.py tests/test_diagnose_containment.py && git commit -m "feat(diagnose): contained test-execution envelope (spike: <mechanism>)"`

---

## Task 1 — Trigger reduction + recorded_traceback (run the test once, fail-closed)

**Files:**
- Modify: `skills/diagnose/dispatch.py`, `skills/diagnose/extraction.py`, `skills/diagnose/diagnose.py`
- Migrate ALL `derive_scope` call-sites (plan-panel P0-1): `skills/diagnose/diagnose.py` AND
  `skills/diagnose-steer/diagnose_steer.py:66` (currently calls the 2-arg form) — both move to the 3-arg
  form in this task so nothing is left calling the old signature.
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `run_contained` (Task 0).
- Produces: `record_traceback(repo_sha_checkout: str, failing_test: str) -> dict` → `{"reproduced": bool,
  "window": {"start": int, "end": int}, "traceback": str, "blocking": str|None}` — `window` is an OBJECT
  `{start,end}` to match `run_record.json` (plan-panel P0-1: the schema uses `{start,end}`, NOT a list);
  `DIAGNOSE_INPUT_REQUIRED = {"failing_test"}`; `derive_scope(failing_test, recorded_traceback, root)`.

- [ ] **Step 1: Write failing tests**

```python
def test_trigger_rejects_error_log_and_freetext(tmp_path):
    from skills.diagnose.dispatch import validate_diagnose_input
    assert "unknown-field" in validate_diagnose_input({"failing_test": "t.py::x", "error_log": "boom"})
    # REAL pytest node-ids must be ACCEPTED (GLM P1: regex must not reject the tests the skill exists to diagnose)
    for ok in ["pkg/t.py::test_a", "pkg/sub/t.py::TestClass::test_method",
               "pkg/t.py::test_x[param-1]", "a/b/t.py::TestC::test_y[case 2-id]"]:
        assert validate_diagnose_input({"failing_test": ok}) == [], ok
    # only genuine non-node-ids rejected:
    assert "invalid-field" in validate_diagnose_input({"failing_test": "not a node id"})
    assert "invalid-field" in validate_diagnose_input({"failing_test": "pkg/t.py"})       # no ::
    assert "invalid-field" in validate_diagnose_input({"failing_test": "pkgnopy::test_a"}) # no .py
# DENY-PROVEN (orchestrator, before build): the OLD regex [\w./-]+::[\w\[\]-]+ WRONGLY REJECTED
# TestClass::test_method, TestC::test_y[case 2-id], TestA::TestB::test_c (3/5 real ids) AND wrongly
# ACCEPTED pkgnopy::test_a (no .py). The acceptance assertions above FAIL on the old regex -> they are the
# load-bearing positive case, not a faith broadening. The build must keep this acceptance corpus.

def test_recorded_traceback_failclosed_when_uncontained(monkeypatch, tmp_path):
    import skills.diagnose.diagnose as D
    monkeypatch.setattr(D, "run_contained", lambda *a, **k: {"contained": False, "reproduced": False,
        "returncode": None, "stdout": "", "stderr": "", "timed_out": False})
    rec = D.record_traceback(str(tmp_path), "pkg/t.py::test_a")
    assert rec["blocking"] == "test-containment-unavailable"

def test_recorded_traceback_failclosed_on_nonreproduction(monkeypatch, tmp_path):
    import skills.diagnose.diagnose as D
    monkeypatch.setattr(D, "run_contained", lambda *a, **k: {"contained": True, "reproduced": False,
        "returncode": 0, "stdout": "", "stderr": "", "timed_out": False})
    rec = D.record_traceback(str(tmp_path), "pkg/t.py::test_a")
    assert rec["blocking"] == "test-nonreproduction"
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/test_diagnose.py -k "trigger or recorded_traceback" -v` → FAIL.

- [ ] **Step 3: Implement** — in `dispatch.py` set `DIAGNOSE_INPUT_REQUIRED = DIAGNOSE_INPUT_ALLOWED = {"failing_test"}`; node-id validation must ACCEPT real pytest node-ids — `<path>.py` followed by one-or-more `::`-separated segments, segments may contain a parametrize `[...]` with arbitrary chars: `re.fullmatch(r"[\w./-]+\.py(::[^:\[\]]+(\[.*\])?)+", failing_test)` else `invalid-field`; the `.py` path part resolves in-repo. (GLM P1: the prior `[\w./-]+::[\w\[\]-]+` rejected class-based + parametrized ids — the majority of real failing tests.) In `diagnose.py` (repo@SHA content read via `git show {repo_sha}:{relpath}` / `git cat-file`, repo-relative — see Task 1 Files; the contained run uses a throwaway checkout at `repo_sha`):

```python
import re, tempfile, subprocess
from skills.diagnose.containment import run_contained

def record_traceback(repo_sha_checkout: str, failing_test: str) -> dict:
    argv = ["python", "-m", "pytest", failing_test, "-q", "--no-header"]
    r = run_contained(argv, cwd=repo_sha_checkout, timeout_s=120)
    if not r["contained"]:
        return {"reproduced": False, "window": {"start": 0, "end": 0}, "traceback": "", "blocking": "test-containment-unavailable"}
    if r["timed_out"]:
        return {"reproduced": False, "window": {"start": 0, "end": 0}, "traceback": "", "blocking": "test-execution-timeout"}
    if not r["reproduced"]:
        return {"reproduced": False, "window": {"start": 0, "end": 0}, "traceback": "", "blocking": "test-nonreproduction"}
    tb = r["stdout"] + r["stderr"]
    window = _window_from_traceback(tb)  # -> {"start": <first>, "end": <last>} traceback line numbers
    return {"reproduced": True, "window": window, "traceback": tb, "blocking": None}
```
`derive_scope(failing_test, recorded_traceback, root)`: AST import-closure over `failing_test`'s path (existing) + window from `recorded_traceback["window"]` (not `error_log`).

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_diagnose.py -k "trigger or recorded_traceback" -v` → PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(diagnose): trigger={failing_test}; skill-derived recorded_traceback, fail-closed"`

---

## Task 2 — Canonical env-free byte-form + seal (the recompute foundation)

**Files:**
- Create: `skills/_diagnose_common/canonical.py`; Modify: `skills/_diagnose_common/__init__.py`
- Test: `tests/test_diagnose_common.py`

**Interfaces:**
- Produces: `canonical_bytes(obj: dict) -> bytes` (repo-relative paths, sorted keys, no timestamps/abs-paths/floats-with-platform-repr), `seal(obj: dict) -> str` (`sha256(canonical_bytes)`). Exported in `__all__`.

- [ ] **Step 1: Write failing test (determinism across env)**

```python
def test_canonical_bytes_env_invariant():
    from skills._diagnose_common import canonical_bytes, seal
    a = {"path": "pkg/m.py", "obs": ["pkg/m.py:3"], "role": "blind"}
    # same logical brief, different dict order + an absolute-path attempt -> identical canonical bytes
    b = {"role": "blind", "obs": ["pkg/m.py:3"], "path": "pkg/m.py"}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert seal(a) == seal(b)

def test_canonical_rejects_absolute_path():
    from skills._diagnose_common import canonical_bytes
    import pytest
    with pytest.raises(ValueError):
        canonical_bytes({"path": "/Users/<user>/x.py"})  # abs path must not survive into a sealed brief
```

- [ ] **Step 2: Run to verify fail** → FAIL (undefined).

- [ ] **Step 3: Implement**

```python
# skills/_diagnose_common/canonical.py
from __future__ import annotations
import hashlib, json

def _check(obj):
    if isinstance(obj, str) and obj.startswith("/"):
        raise ValueError(f"absolute path in sealed brief: {obj!r}")
    if isinstance(obj, dict):
        for v in obj.values(): _check(v)
    elif isinstance(obj, list):
        for v in obj: _check(v)

def canonical_bytes(obj: dict) -> bytes:
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

def seal(obj: dict) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
```
Export both from `__init__.py` `__all__`.

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(_diagnose_common): canonical env-free byte-form + seal"`

---

## Task 3 — Committed gate-constants (panel_constants.json)

**Files:**
- Create: `skills/diagnose/panel_constants.json`
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Produces: `panel_constants.json` = `{"roster":[{"role":"blind","model":<m1>},{"role":"alternative","model":<m2>},{"role":"open","model":<m3>}], "scribe":{"model":<ms>,"system_prompt":"<committed descriptive-only template>"}, "certifier":{"rule":"model!=author_model and not reciprocal"}, "role_assignment":{"rule":"top-candidate-by-fixed-rank","seed":<int>}, "collation_order":"by-seat-id-asc"}`. Loaded by `briefs.py`.

- [ ] **Step 1: Write failing test** — assert the file exists, parses, has all 5 keys, distinct roster models (decorrelated), and the scribe system_prompt contains no synthesis verbs (descriptive-only):

```python
def test_panel_constants_committed_and_decorrelated():
    import json
    from pathlib import Path
    c = json.loads(Path("skills/diagnose/panel_constants.json").read_text())
    assert {r["model"] for r in c["roster"]} .__len__() == 3   # decorrelated
    assert all(k in c for k in ["roster","scribe","certifier","role_assignment","collation_order"])
    assert "synthesize" not in c["scribe"]["system_prompt"].lower()
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Create the file** with the committed constants (pick 3 distinct fleet models for the roster, e.g. the decorrelated set used in review; scribe template strictly descriptive).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(diagnose): committed panel_constants.json (roster/roles/certifier/scribe/collation)"`

---

## Task 4 — Pre-response brief authoring + sealing (skill-authors-and-seals)

**Files:**
- Create: `skills/diagnose/briefs.py`
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `canonical_bytes`/`seal` (Task 2), `panel_constants.json` (Task 3), `derive_scope`/`extract` (Task 1).
- Produces: `author_briefs(failing_test, repo_sha, recorded_traceback, constants) -> list[SealedBrief]` where `SealedBrief = {"role":str,"model":str,"brief":dict,"seal":str}`; the brief body is a pure function of inputs — observables (repo-relative), structured role task, committed role prompt; NO raw trigger prose.

- [ ] **Step 1: Write failing test (purity + determinism + no-prose)**

```python
def test_author_briefs_is_pure_and_recomputable(tmp_path, monkeypatch):
    from skills.diagnose.briefs import author_briefs
    import json
    from pathlib import Path
    c = json.loads(Path("skills/diagnose/panel_constants.json").read_text())
    rt = {"reproduced": True, "window": {"start": 3, "end": 9}, "traceback": "pkg/m.py:5 boom UNIQUEPROSE"}
    b1 = author_briefs("pkg/test_m.py::test_x", "deadbeef", rt, c)
    b2 = author_briefs("pkg/test_m.py::test_x", "deadbeef", rt, c)
    assert [x["seal"] for x in b1] == [x["seal"] for x in b2]      # deterministic
    blob = json.dumps(b1)
    assert "UNIQUEPROSE" not in blob                               # raw traceback prose must NOT reach any brief (strict, not an OR)
    assert len(b1) == 4                                            # scribe + 3 seats
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `author_briefs`: for the scribe + each roster role, build `brief = {"role":role, "observables":<extracted, repo-relative>, "task":<committed role task>, "system_prompt":<committed prompt>}`, `seal=seal(brief)`. The brief draws ONLY from extracted observables (via `derive_scope`/`extract` over the window) + committed constants — never `recorded_traceback["traceback"]` raw text.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(diagnose): pre-response brief authoring + sealing (pure, env-free)"`

---

## Task 5pre — Extend the run_record schema to carry the recompute basis (plan-panel P1)

**Files:** Modify `skills/_diagnose_common/schemas/run_record.json`, `tests/test_diagnose_common.py`

The gate (Task 6) recomputes against committed fields — if the schema doesn't carry them, recompute is
vacuous. Add (all optional for non-live records; REQUIRED when `verified:true`): `repo_sha:str`,
`recorded_traceback:{reproduced:bool, window:{start:int,end:int}, traceback_sha256:str}` (the traceback is
HASH-ANCHORED, not stored raw), `sealed_briefs:[{role,model,seal,brief}]`, `submissions:[{role,seat,
seal,bus_reply_ref,bus_reply_sha256}]` (bus_reply_ref/sha = a §7b consistency record; forgery-resistant authenticity is deferred), `post_briefs:
[{role,seal,brief}]`. `window` is `{start,end}` (object). Test: a record with these validates; a
`verified:true` record missing any → schema-invalid.
- [ ] Write the schema-field test → FAIL → add the fields → PASS → commit `feat(_diagnose_common): run_record carries recompute basis (repo_sha, anchored traceback, sealed briefs/submissions+bus refs)`.

---

## Task 5a — Live dispatch (forward-opaque) + bus-anchored submissions + independent phase

**Files:** Create `skills/diagnose/panel.py`; Test `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `author_briefs` (Task 4); the bridge (`scripts/agent-dispatch`).
- Produces: `run_panel(sealed_briefs, work_dir) -> {"submissions":[{role,seat,seal,bus_reply_ref,bus_reply_sha256}], "blocking":str|None}` — forwards each `SealedBrief` VERBATIM via `agent-dispatch` (`--ceiling Read,Grep,Glob`, read-only) to its roster model; submissions written OUTSIDE `repo_root` (under `work_dir`) until all submit; EACH submission records the bridge/bus reply ref + its sha256 for non-adversarial consistency checks; a missing seat → `blocking="incomplete-panel"`; bus/seat down → `"bridge-unavailable"`.

- [ ] **Step 1: Failing tests** — (a) partial panel → `incomplete-panel`; (b) bridge down → `bridge-unavailable`; (c) submissions written outside `repo_root`; (d) each submission carries `bus_reply_ref` + `bus_reply_sha256` matching the (mocked) bus reply. (Mock `agent-dispatch`/bus to return canned replies / raise.)
- [ ] **Step 2: Run → FAIL.** **Step 3: Implement** `run_panel`. **Step 4: PASS.** **Step 5: Commit** `feat(diagnose): live forward-opaque dispatch + bus-anchored submissions + independent phase`.

## Task 5b — Post-response briefs (certifier + collation)

**Files:** Modify `skills/diagnose/briefs.py`; Test `tests/test_diagnose.py`
- Produces: `author_post_briefs(constants, sealed_submissions) -> list[SealedBrief]` — pure function of `(constants, sealed submissions)`; deterministic.
- [ ] Failing test (determinism over same sealed submissions) → FAIL → implement → PASS → commit `feat(diagnose): post-response certifier/collation briefs (pure over sealed submissions)`.

## Task 5c — Rewire `run_diagnose` + remove the stub

**Files:** Modify `skills/diagnose/diagnose.py`; Test `tests/test_diagnose.py`
- [ ] Rewire: validate trigger → `record_traceback` (fail-closed) → `author_briefs` → `run_panel` → `author_post_briefs` → assemble run_record (carrying the Task-5pre fields). **Remove stub `SEATS`/`generate_blind_candidates`.** `verified` stays False until Task 7's validator passes. **MIGRATE the stub-pinning tests** (`test_diagnose.py` asserting `panel_executed:False`/`live-panel-not-wired`) to assert the LIVE behaviour (plan-panel P0-2). → FAIL → implement → PASS → commit `feat(diagnose): rewire run_diagnose to the live panel; remove stub; migrate stub tests`.

---

## Task 6 — Gate recompute checks (anchored; brief-tampered, brief-not-skill-authored, submission-inconsistent)

**Files:**
- Modify: `skills/bridge-protocol/gate/gate.py`
- Test: `tests/test_bridge_protocol_gate.py`

**Interfaces:**
- Consumes: `canonical_bytes`/`seal`, `author_briefs`/`author_post_briefs`, the run_record's Task-5pre fields, the bus reply record.
- Produces: `brief_authorship_blocks(run_record, bus_records) -> list[str]` — recomputes each dispatched brief from the run_record's COMMITTED basis (pre-response from `(failing_test, repo@repo_sha, recorded_traceback, constants)`; post-response from `(constants, sealed submissions)`) and byte-compares the canonical form; `brief-tampered` if dispatched hash ≠ recorded seal; `brief-not-skill-authored` if recomputed canonical ≠ dispatched; **`submission-inconsistent` if a submission's `bus_reply_sha256` has no matching supplied bus record** (§7b consistency only; not a forgery guarantee because the caller supplies `bus_records`).

- [ ] **Step 1: Write failing tests (THREE negative controls + clean twin)** — each from a real/realistic run_record:
  - `test_gate_blocks_contaminated_trigger_authorship` — a pre-response brief authored from a contaminated trigger (window override / injected prose) → recompute from `{failing_test}` disagrees → `brief-not-skill-authored`.
  - `test_gate_blocks_swapped_traceback` — the SAME failing_test but a SWAPPED `recorded_traceback` (different window) → the recomputed pre-response brief differs → `brief-not-skill-authored` (proves the recompute is anchored to the committed traceback, not "agreeing with itself").
  - `test_gate_blocks_submission_inconsistent_with_supplied_bus_records` — a submission whose `bus_reply_sha256` matches NO supplied bus record → `submission-inconsistent`.
  - `test_gate_blocks_transit_tamper` → `brief-tampered`; `test_gate_passes_clean_consistent_run` → `[]`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `brief_authorship_blocks` calling the SAME `author_briefs`/`author_post_briefs` the skill used + the supplied bus-record consistency check; wire into the gate's block aggregation.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(gate): anchored recompute + submission consistency (tampered/not-authored/inconsistent)"`

---

## Task 7 — Scribe-isolation-verified + validator-side fail-loud (unverified-without-panel)

**Files:**
- Modify: `skills/_diagnose_common/neutral_validators.py`, `skills/diagnose/diagnose.py`
- Test: `tests/test_diagnose_common.py`, `tests/test_diagnose.py`

**Interfaces:**
- Produces: in the domain validator, `unverified-without-panel` when `verified:true` but panel evidence incomplete (missing a roster seat's sealed submission, certifier not independent, or barrier ordering unsatisfied). Scribe isolation: the scribe brief must equal the recomputed committed template (Task 6 covers the recompute; this asserts the scribe-specific case).

- [ ] **Step 1: Write failing tests** — (a) a hand-built `verified:true` with no submissions → `unverified-without-panel` (the producer→validator migration: forged record can't validate clean); (b) clean full-panel verified record → no block; (c) scribe brief ≠ committed template → block.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the validator check (consumes the run_record's sealed submissions + roster from constants). `run_diagnose` sets `verified:true` ONLY when the validator's panel-evidence check passes; else `harness_only:true`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat: validator-side fail-loud (unverified-without-panel) + scribe-isolation verified"`

---

## Task 8 — Confidence correction: noisy-OR over CAPPED ALTERNATIVES (corrects MERGED diagnose-steer)

**Files:**
- Modify: `skills/diagnose-steer/steer_validators.py`, `skills/diagnose-steer/confidence_constants.json`
- Modify: `skills/bridge-protocol/gate/load_bearing_components.json` — the `diagnose-steer` `confidence-correctness`
  dimension currently pins `test_s3_confidence_direction_and_weak_count_normalization_are_end_to_end` with
  evidence prose *"norm = sum(weight) so weak padding lowers Q"* (plan-panel/cold-Opus residual: this
  formula is REPLACED here, so that test ref dangles + the prose goes false). Repoint the
  `confidence-correctness` dimension_preserving_test to the new `test_s3_confidence_both_properties` and
  rewrite the evidence prose to the capped-alternative semantics. (Additive-elsewhere; this one entry edited.)
- Test: `tests/test_diagnose_steer.py`

**Interfaces:**
- Produces: `Q = 1 - Π_alt (1 - capped_disconf(alt))` where `capped_disconf(alt) = min(cap, max over that
  alternative's §D3-certified predicates of exclusivity*strength)`; `confidence = P_max*Q`. NOTE: the
  product is over DISTINCT ALTERNATIVES (keyed by the predicate's `hypothesis_b`), NOT over predicates —
  plain per-predicate noisy-OR reopens within-alternative count-inflation (plan-panel/GLM). `cap` is a
  committed gate-constant in `confidence_constants.json`.

- [ ] **Step 1: Write the END-TO-END test asserting BOTH properties on the cases that fail today**

This must be END-TO-END (real run_diagnose_steer producing certified predicates), NOT a bare unit call on
undefined symbols (the plan-panel caught the bare-unit version as green-by-construction). Build real
schema-conformant certified predicates via the integrated path, then:

```python
def test_s3_confidence_both_properties(tmp_path):
    # helper: run a real steer panel whose certified predicates we control by fixture (each genuinely §D3-certified)
    # (a) WITHIN-ALTERNATIVE CAP: 12 file_line predicates disconfirming the SAME alternative do NOT outscore 2
    q_many_weak_same_alt = confidence_of(certified=[fl(alt="A")]*12)
    q_few_weak_same_alt   = confidence_of(certified=[fl(alt="A")]*2)
    assert q_many_weak_same_alt == q_few_weak_same_alt        # capped: padding ONE alt can't inflate (the axis that fails today)
    # (b) ACROSS-ALTERNATIVE MONOTONICITY: eliminating an ADDITIONAL distinct alternative raises Q
    q_one_alt  = confidence_of(certified=[fl(alt="A")])
    q_two_alts = confidence_of(certified=[fl(alt="A"), fl(alt="B")])
    assert q_two_alts > q_one_alt                              # ruling out more distinct alternatives raises Q
    # and a strong single disconfirmation still beats many weak of the same alt:
    assert confidence_of(certified=[ld(alt="A")]) > q_many_weak_same_alt
```

- [ ] **Step 2: Run → FAIL** — current weighted-average dilutes on (b); plain noisy-OR (if naively
  implemented) fails (a) because `[fl]*12` of one alt would exceed `[fl]*2`.
- [ ] **Step 3: Implement** capped-alternatives noisy-OR in `compute_confidence`:

```python
cap = constants["alt_contribution_cap"]            # committed gate-constant (e.g. 0.9)
by_alt = {}                                        # alternative -> best exclusivity*strength
for p in certified_predicates:                     # §D3-certified ONLY; uncertified contribute 0
    alt = p["hypothesis_b"]
    strength = strength_by_category[resolve_evidence_category(p["certifier"]["evidence_ref"])]
    by_alt[alt] = max(by_alt.get(alt, 0.0), p_exclusivity(p) * strength)
prod = 1.0
for alt, best in by_alt.items():
    prod *= (1.0 - min(cap, best))                 # CAP per alternative -> within-alt padding can't inflate
Q = 1.0 - prod                                     # monotone in DISTINCT alternatives eliminated
confidence = constants["P_max"] * Q
```
Add `alt_contribution_cap` to `confidence_constants.json` (committed; phase-supplied → `pmax-as-input` BLOCK, same as the other constants).

- [ ] **Step 4: Run → PASS** — within-alt cap (a) AND across-alt monotonicity (b) both hold.
- [ ] **Step 5: Commit** — `git commit -am "fix(diagnose-steer): capped-alternative noisy-OR confidence (monotone + count-bounded)"`

---

## Task 9 — diagnose-steer live panel + sealed steer brief

**Files:**
- Modify: `skills/diagnose-steer/diagnose_steer.py`
- Test: `tests/test_diagnose_steer.py`

**Interfaces:**
- Consumes: Tasks 1–7 (the live panel + seal/recompute), Task 8 (confidence).
- Produces: `run_diagnose_steer` wires the live panel; the steer brief is skill-authored+sealed (the declared steer is a committed, attributed input the gate recomputes); fail-loud preserved (stubbed→live transition keeps `verified:false` until full panel + barrier satisfied).

- [ ] **Step 1: Write failing tests** — (a) the steer brief is sealed + gate-recomputable from `(declared_steer, failing_test, constants)`; an orchestrator-reshaped steer brief → `brief-not-skill-authored`; (b) fail-loud still structural (stubbed/partial → no verified=true); (c) the barrier (`max_seq`) + prev_hash rejection still hold over the live dispatch. **MIGRATE the stub-pinning steer tests** (`test_diagnose_steer.py` asserting `panel_executed:False`/`live-panel-not-wired`, incl. `test_stubbed_steer_panel_run_fails_loud...`) to assert the LIVE behaviour (plan-panel P0-2 — these are unowned otherwise).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the steer-side wiring reusing `panel.run_panel` + `briefs` (steer adds the labelled steer channel to the brief inputs; barrier composed from `max_seq`). `derive_scope` call-site here is already the 3-arg form (migrated in Task 1).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(diagnose-steer): live panel + sealed/recomputed steer brief; migrate stub tests"`

---

## Task 10 — Gate-onboard the new/changed files (additive; orchestrator-integrated)

**Files:**
- Modify: `skills/bridge-protocol/gate/layer_registry.json`, `skills/bridge-protocol/gate/load_bearing_components.json`
- Test: `tests/test_bridge_protocol_gate.py`

- [ ] **Step 1: Write failing test** — the self-gate (`test_bridge_protocol_passes_its_own_gate_default_path`) passes with the new files classified; new production files (`canonical.py`, `containment.py`, `briefs.py`, `panel.py`) have manifest entries with REAL dimension-preserving tests.
- [ ] **Step 2: Run → FAIL** (new files unclassified).
- [ ] **Step 3: Implement (ADDITIVE — preserve existing rules, catch-all last):** add registry rules: `skills/diagnose/*.py` already covers `briefs.py`/`panel.py`/`containment.py` (extend `integrated-run` dimension; add `test-containment` dimension for `containment.py` → `tests/test_diagnose_containment.py`); `skills/_diagnose_common/*.py` already covers `canonical.py` (add `byte-form-determinism` dimension → `test_canonical_bytes_env_invariant`). Add manifest entries pointing at the real end-to-end tests. Exclude any new `*.json` constants. **MIGRATE the existing manifest's dimension-preserving-test references that named the now-replaced stub tests** (plan-panel P0-2: `load_bearing_components.json` named the `test_stubbed_*` / `test_evaluate_run_record_uses_real_dispatch_log...` tests — repoint them to the live-panel tests that replace them, or those references dangle).
- [ ] **Step 4: Run → PASS** + the full diagnose/steer/gate suites green.
- [ ] **Step 5: Commit** — `git commit -am "gate: onboard live-panel files (real dimension-preserving tests)"`

---

## Self-Review

**Spec coverage:** §1 trigger/traceback → Task 1; §2 two bases + canonical + run_record fields → Tasks 2,4,5pre; §3 recompute checks (+ submission consistency) → Task 6; §4 panel_constants → Task 3; §5 scribe verified → Task 7; §6 independent phase + partial/bridge fail-loud → Tasks 5a,5c,7; §7 containment (spike + five invariants + fail-closed) → Task 0,1; §7a capped-alternative confidence + both-property dogfood → Task 8; §7b consistency (submission↔bus) + threat-model scope → Tasks 5a,6; §8 dogfoods → in each task's tests; §9 steer delta + stub-test migration → Task 9; stub→live test migration → Tasks 5c,9; gate-onboard + manifest migration → Task 10. All sections covered.

**Placeholder scan:** the only deferred item is Task 0's sandbox MECHANISM, which is a spike with a concrete interface (`run_contained`) + pass-bar escape-tests — not a placeholder (the interface and fail-closed default are concrete). No "TBD"/"handle errors"/code-free steps.

**Type consistency:** `SealedBrief={role,model,brief,seal}`, `recorded_traceback={reproduced,window,traceback,blocking}`, `ContainedResult={reproduced,returncode,stdout,stderr,timed_out,contained}` used consistently across Tasks 0,1,4,5,6. `seal`/`canonical_bytes` signatures consistent (Task 2 → 4,6). Block-reason strings consistent with Global Constraints.
