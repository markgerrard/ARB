# agent-sdk mutation probe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build a committed, secret-free probe that proves whether `claude-agent-sdk` can drive M3 / Kimi / GLM-5.2 through a genuine multi-step code mutation, judged by a held-out oracle the model never sees and cannot read.

**Architecture:** Deterministic harness units first (model config, secret-scrub, fixture, anti-false-PASS verifier — all TDD). Then a Stage-0 go/no-go spike that resolves the live SDK call pattern + per-vendor endpoints and gates the matrix. Then the per-model probe runner (own subprocess, scoped env) wired to the verifier. Then the live run + results doc.

**Tech Stack:** Python 3 (bridge `.venv`), `claude-agent-sdk` (+ Claude Code CLI it spawns), `pytest`, `git`, `subprocess`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-18-agent-sdk-mutation-probe-design.md` (authoritative).
- Build dir: `tools/agent-sdk-probe/` — committed, **secret-free** (no key value EVER in source/output/results).
- Keys: read by env-var name from gitignored `envs/agent-sdk-models-dev.env`: `AGENT_SDK_MINIMAX_KEY`, `AGENT_SDK_KIMI_KEY`, `AGENT_SDK_GLM_KEY`. Models: MiniMax-M3, Kimi, GLM-5.2. **Qwen dropped.**
- Approach **A only** (claude-agent-sdk; no raw-`anthropic` fallback). Each model runs in its **own subprocess** with scoped env.
- **Canonical unit-test command** (verified runnable — use this exact form everywhere): `cd tools/agent-sdk-probe && ../../.venv/bin/python3 -m unittest discover -s tests -v`. (Tests import `models`/`scrub`/`verifier` as top-level modules; `discover -s tests` run from the probe dir puts them on the path. Do NOT use `PYTHONPATH=src` or a dotted `tools.agent-sdk-probe...` module path — the hyphen makes that invalid.)
- A PASS green-lights an engine **build-spike**, not the engine; residual risks (bridge tool-mediation/D1, worktree, completion-gate, progress/steer) are out of scope.

---

### Task 0: Scaffold + test deps

**Files:** Create `tools/agent-sdk-probe/tests/__init__.py` (empty).

- [ ] **Step 1: Install pytest** (Tasks 3/4 shell out to it; must exist before them): `.venv/bin/python3 -m pip install pytest && .venv/bin/python3 -m pytest --version`
- [ ] **Step 2: Create dirs** `mkdir -p tools/agent-sdk-probe/tests tools/agent-sdk-probe/fixture tools/agent-sdk-probe/held_out && touch tools/agent-sdk-probe/tests/__init__.py`
- [ ] **Step 3: Commit** `git add tools/agent-sdk-probe/tests/__init__.py && git commit -m "chore(agent-sdk-probe): scaffold + pytest dep"`

---

### Task 1: Model config + env loader

**Files:** Create `tools/agent-sdk-probe/models.py`; Test `tools/agent-sdk-probe/tests/test_models.py`.

**Interfaces — Produces:** `ModelSpec(name, base_url, model_id, key_env, auth_style="x-api-key")` (frozen dataclass); `MODELS: list[ModelSpec]`; `load_key(spec) -> str` (raises `MissingKeyError` naming the var if unset).

- [ ] **Step 1: Failing test**
```python
# tools/agent-sdk-probe/tests/test_models.py
import os, unittest
from unittest.mock import patch
from models import ModelSpec, MODELS, load_key, MissingKeyError

class ModelsTest(unittest.TestCase):
    def test_matrix_three_no_qwen(self):
        self.assertEqual({m.name for m in MODELS}, {"minimax-m3", "kimi", "glm-5.2"})
    def test_m3_known(self):
        m3 = next(m for m in MODELS if m.name == "minimax-m3")
        self.assertEqual((m3.base_url, m3.model_id, m3.key_env),
                         ("https://api.minimax.io/anthropic", "MiniMax-M3", "AGENT_SDK_MINIMAX_KEY"))
    def test_load_key_reads_env(self):
        with patch.dict(os.environ, {"T_KEY": "secret123"}, clear=False):
            self.assertEqual(load_key(ModelSpec("x","u","i","T_KEY")), "secret123")
    def test_load_key_missing_named(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ABSENT_KEY", None)
            with self.assertRaises(MissingKeyError) as c:
                load_key(ModelSpec("x","u","i","ABSENT_KEY"))
            self.assertIn("ABSENT_KEY", str(c.exception))
```
- [ ] **Step 2: Run, verify fail** — `cd tools/agent-sdk-probe && ../../.venv/bin/python3 -m unittest discover -s tests -v` → FAIL (no `models`).
- [ ] **Step 3: Implement**
```python
# tools/agent-sdk-probe/models.py
"""Model matrix for the probe. Secret-free: keys read from env by name."""
from __future__ import annotations
import os
from dataclasses import dataclass

class MissingKeyError(RuntimeError): pass

@dataclass(frozen=True)
class ModelSpec:
    name: str; base_url: str; model_id: str; key_env: str; auth_style: str = "x-api-key"

# Kimi/GLM values are best-guess pending Stage 0 (Task 5 corrects them). M3 known.
MODELS: list[ModelSpec] = [
    ModelSpec("minimax-m3", "https://api.minimax.io/anthropic", "MiniMax-M3", "AGENT_SDK_MINIMAX_KEY"),
    ModelSpec("kimi", "https://api.moonshot.ai/anthropic", "kimi-for-coding", "AGENT_SDK_KIMI_KEY"),
    ModelSpec("glm-5.2", "https://open.bigmodel.cn/api/anthropic", "glm-5.2", "AGENT_SDK_GLM_KEY"),
]

def load_key(spec: ModelSpec) -> str:
    val = os.environ.get(spec.key_env)
    if not val:
        raise MissingKeyError(f"env var {spec.key_env} is unset; source envs/agent-sdk-models-dev.env")
    return val
```
- [ ] **Step 4: Run, verify pass** (4 tests).
- [ ] **Step 5: Commit** — `git add tools/agent-sdk-probe/models.py tools/agent-sdk-probe/tests/test_models.py && git commit -m "feat(agent-sdk-probe): model matrix + env-name key loader"`

---

### Task 2: Secret scrubber (tested invariant)

**Files:** Create `tools/agent-sdk-probe/scrub.py`; Test `tools/agent-sdk-probe/tests/test_scrub.py`.

**Interfaces — Produces:** `scrub(text, secrets: list[str], var_names: list[str]) -> str` — replaces every exact occurrence of each secret value AND var name with `[REDACTED]`; ignores empty/whitespace secrets.

- [ ] **Step 1: Failing test**
```python
# tools/agent-sdk-probe/tests/test_scrub.py
import unittest
from scrub import scrub
class ScrubTest(unittest.TestCase):
    def test_redacts_value(self):
        self.assertNotIn("sk-abc", scrub("t=sk-abc done", ["sk-abc"], []))
    def test_redacts_var_name(self):
        self.assertNotIn("AGENT_SDK_KIMI_KEY", scrub("echo $AGENT_SDK_KIMI_KEY", [], ["AGENT_SDK_KIMI_KEY"]))
    def test_ignores_empty(self):
        self.assertEqual(scrub("hello", ["", "  "], []), "hello")
    def test_canary_absent(self):
        c = "CANARY-9f3a"
        self.assertNotIn(c, scrub(f"env -> {c}", [c], []))
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement**
```python
# tools/agent-sdk-probe/scrub.py
"""Scrub secrets from any captured stream before printing/persisting.
Defends against a model running `env`/`echo $KEY` via the Bash tool."""
from __future__ import annotations
REDACTED = "[REDACTED]"
def scrub(text: str, secrets: list[str], var_names: list[str]) -> str:
    out = text
    for s in list(secrets) + list(var_names):
        s = (s or "").strip()
        if s:
            out = out.replace(s, REDACTED)
    return out
```
- [ ] **Step 4: Run, verify pass** (4 tests).
- [ ] **Step 5: Commit** — `git add tools/agent-sdk-probe/scrub.py tools/agent-sdk-probe/tests/test_scrub.py && git commit -m "feat(agent-sdk-probe): secret scrubber + canary test"`

---

### Task 3: Fixture (stub + visible contract test) + held-out oracle DATA

**Files:** Create `tools/agent-sdk-probe/fixture/wordwrap.py`, `tools/agent-sdk-probe/fixture/test_contract.py`, `tools/agent-sdk-probe/held_out/cases.py`; Test `tools/agent-sdk-probe/tests/test_fixture.py`.

**Interfaces — Produces:** task `wrap(text, width) -> list[str]` (greedy word-wrap; never splits a word; collapses whitespace; `width<=0` raises `ValueError`). Stub raises `NotImplementedError`. `test_contract.py` = visible failing test. `held_out/cases.py` = `CASES: list[tuple[tuple, list[str]]]` — verifier-owned (args, expected) pairs, **never copied into the model's repo** (closes the readable-oracle path).

- [ ] **Step 1: Failing meta-test** (pins determinism: stub fails contract; reference impl satisfies contract AND every held-out case)
```python
# tools/agent-sdk-probe/tests/test_fixture.py
import shutil, subprocess, sys, tempfile, unittest, importlib.util
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
REFERENCE = '''
def wrap(text, width):
    if width <= 0: raise ValueError("width must be positive")
    words, lines, cur = text.split(), [], ""
    for w in words:
        if not cur: cur = w
        elif len(cur)+1+len(w) <= width: cur += " "+w
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines
'''
def _load_wrap(path):
    spec = importlib.util.spec_from_file_location("ww", path); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m); return m.wrap
class FixtureTest(unittest.TestCase):
    def test_stub_fails_contract(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shutil.copy(HERE/"fixture"/"wordwrap.py", d/"wordwrap.py")
            shutil.copy(HERE/"fixture"/"test_contract.py", d/"test_contract.py")
            rc = subprocess.run([sys.executable,"-m","pytest","-q",str(d/"test_contract.py")], cwd=d, capture_output=True).returncode
            self.assertNotEqual(0, rc)
    def test_reference_satisfies_contract_and_heldout(self):
        from held_out.cases import CASES
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); (d/"wordwrap.py").write_text(REFERENCE)
            shutil.copy(HERE/"fixture"/"test_contract.py", d/"test_contract.py")
            rc = subprocess.run([sys.executable,"-m","pytest","-q",str(d/"test_contract.py")], cwd=d, capture_output=True).returncode
            self.assertEqual(0, rc)
            wrap = _load_wrap(d/"wordwrap.py")
            for args, expected in CASES:
                self.assertEqual(wrap(*args), expected)
```
- [ ] **Step 2: Run, verify fail** (files missing).
- [ ] **Step 3: Create the files**
```python
# tools/agent-sdk-probe/fixture/wordwrap.py
def wrap(text, width):
    """Greedy word-wrap `text` into lines of at most `width` chars.
    - Never split a word (a word longer than width gets its own overflowing line).
    - Collapse whitespace runs to single spaces; ignore leading/trailing.
    - width <= 0 raises ValueError. Returns list[str], no empty/trailing-space lines.
    """
    raise NotImplementedError
```
```python
# tools/agent-sdk-probe/fixture/test_contract.py
import pytest
from wordwrap import wrap
def test_basic(): assert wrap("the quick brown fox", 9) == ["the quick", "brown fox"]
def test_single(): assert wrap("hello", 10) == ["hello"]
def test_invalid_width():
    with pytest.raises(ValueError): wrap("x", 0)
```
```python
# tools/agent-sdk-probe/held_out/cases.py
"""Held-out oracle: (args, expected) pairs the model NEVER sees. The verifier
calls the model's wrap() with these directly — they are never written into the
model's repo, so a gaming impl cannot read them at runtime."""
CASES = [
    (("aa supercalifragilistic bb", 5), ["aa", "supercalifragilistic", "bb"]),
    (("a   b    c", 5), ["a b c"]),
    (("abc de", 6), ["abc de"]),
    (("   hi there   ", 8), ["hi there"]),
]
```
Also create `tools/agent-sdk-probe/held_out/__init__.py` (empty) so `from held_out.cases import CASES` resolves from the probe dir.
- [ ] **Step 4: Run, verify pass** (2 tests).
- [ ] **Step 5: Commit** — `git add tools/agent-sdk-probe/fixture tools/agent-sdk-probe/held_out tools/agent-sdk-probe/tests/test_fixture.py && git commit -m "feat(agent-sdk-probe): wordwrap fixture + verifier-owned held-out cases"`

---

### Task 4: Anti-false-PASS verifier

**Files:** Create `tools/agent-sdk-probe/verifier.py`; Test `tools/agent-sdk-probe/tests/test_verifier.py`.

**Interfaces — Consumes:** `fixture/`, `held_out/cases.py`. **Produces:** `verify(model_repo: Path) -> Verdict{status: "PASS"|"PARTIAL"|"FAIL", reasons: list[str]}`.

**Logic (every clause from spec line 68-74):**
1. FAIL if `model_repo/wordwrap.py` missing or byte-identical to the stub (no edit).
2. FAIL if `model_repo` touched any file other than `wordwrap.py` — checked via `git status --porcelain` against the baseline commit Task 6 makes (covers added `conftest.py`/helpers, deleted/edited `test_contract.py`, edited `.gitignore`, etc.). This is the spec's "only the impl changed" rule.
3. Build a clean tempdir with ONLY the model's `wordwrap.py` + a pristine `test_contract.py`; run contract via pytest → red ⇒ PARTIAL.
4. Held-out: load the model's `wrap` and call it with `held_out.cases.CASES` from a driver whose **cwd is a separate dir containing only `wordwrap.py`** (no test/expected files on the impl's path) → any mismatch ⇒ FAIL ("hardcoded to visible cases").
5. All green ⇒ PASS.

- [ ] **Step 1: Failing adversarial tests**
```python
# tools/agent-sdk-probe/tests/test_verifier.py
import subprocess, tempfile, unittest
from pathlib import Path
from verifier import verify
HERE = Path(__file__).resolve().parent.parent
GENUINE = '''
def wrap(text, width):
    if width<=0: raise ValueError("w")
    words,lines,cur=text.split(),[],""
    for w in words:
        if not cur: cur=w
        elif len(cur)+1+len(w)<=width: cur+=" "+w
        else: lines.append(cur); cur=w
    if cur: lines.append(cur)
    return lines
'''
HARDCODED = '''
def wrap(text,width):
    if width<=0: raise ValueError("w")
    if text=="the quick brown fox" and width==9: return ["the quick","brown fox"]
    if text=="hello": return ["hello"]
    return [text]
'''
def _repo(impl, extra=None, drop_contract=False, tamper_contract=False):
    d = Path(tempfile.mkdtemp())
    (d/"wordwrap.py").write_text(impl)
    if not drop_contract:
        c = "def test_noop():\n    assert True\n" if tamper_contract else (HERE/"fixture"/"test_contract.py").read_text()
        (d/"test_contract.py").write_text(c)
    if extra: (d/extra).write_text("x=1\n")
    subprocess.run(["git","init","-q"], cwd=d); subprocess.run(["git","add","-A"], cwd=d)
    subprocess.run(["git","-c","user.email=t@t","-c","user.name=t","commit","-qm","base"], cwd=d)
    # mutate AFTER baseline so the diff reflects the "model's" changes:
    return d
class VerifierTest(unittest.TestCase):
    def _commit_then(self, impl, **kw):
        # baseline is the STUB repo; then overlay the candidate impl as the "model edit"
        d = _repo((HERE/"fixture"/"wordwrap.py").read_text(), **kw)
        (d/"wordwrap.py").write_text(impl)
        return d
    def test_genuine_passes(self):
        self.assertEqual(verify(self._commit_then(GENUINE)).status, "PASS")
    def test_stub_unchanged_fails(self):
        self.assertEqual(verify(self._commit_then((HERE/"fixture"/"wordwrap.py").read_text())).status, "FAIL")
    def test_hardcoded_fails_on_heldout(self):
        self.assertEqual(verify(self._commit_then(HARDCODED)).status, "FAIL")
    def test_side_channel_file_fails(self):
        d = self._commit_then(GENUINE); (d/"helper.py").write_text("x=1\n")
        self.assertEqual(verify(d).status, "FAIL")
    def test_deleted_contract_fails(self):
        d = self._commit_then(GENUINE); (d/"test_contract.py").unlink()
        self.assertEqual(verify(d).status, "FAIL")
    def test_tampered_contract_fails(self):
        self.assertEqual(verify(self._commit_then(GENUINE, tamper_contract=True)).status, "FAIL")
```
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement**
```python
# tools/agent-sdk-probe/verifier.py
"""Anti-false-PASS verifier: judge the model's impl against a pristine contract
AND a held-out oracle the model never saw — and enforce that ONLY wordwrap.py
changed (git diff vs the baseline). A PASS means 'correct on held-out inputs',
not 'pytest exited 0 in a repo the model could edit'."""
from __future__ import annotations
import json, shutil, subprocess, sys, tempfile
from dataclasses import dataclass, field
from pathlib import Path
HERE = Path(__file__).resolve().parent
FIXTURE = HERE/"fixture"; HELD_OUT = HERE/"held_out"

@dataclass
class Verdict:
    status: str; reasons: list[str] = field(default_factory=list)

def _changed_files(repo: Path) -> list[str]:
    r = subprocess.run(["git","status","--porcelain"], cwd=repo, capture_output=True, text=True)
    return [ln[3:].strip() for ln in r.stdout.splitlines() if ln.strip()]

def _pytest_ok(testfile: Path, cwd: Path) -> bool:
    return subprocess.run([sys.executable,"-m","pytest","-q",str(testfile)], cwd=cwd, capture_output=True).returncode == 0

def verify(model_repo: Path) -> Verdict:
    impl = model_repo/"wordwrap.py"
    if not impl.exists():
        return Verdict("FAIL", ["no wordwrap.py"])
    if impl.read_text() == (FIXTURE/"wordwrap.py").read_text():
        return Verdict("FAIL", ["implementation unchanged (stub)"])
    changed = _changed_files(model_repo)
    if changed != ["wordwrap.py"]:
        return Verdict("FAIL", [f"files other than wordwrap.py changed: {changed}"])
    # clean checkout: only the model's impl + pristine contract
    clean = Path(tempfile.mkdtemp())
    shutil.copy(FIXTURE/"test_contract.py", clean/"test_contract.py")
    shutil.copy(impl, clean/"wordwrap.py")
    if not _pytest_ok(clean/"test_contract.py", clean):
        return Verdict("PARTIAL", ["impl edited but contract test red"])
    # held-out: run from a dir with ONLY wordwrap.py; cases injected via the driver (never on disk near impl)
    iso = Path(tempfile.mkdtemp()); shutil.copy(impl, iso/"wordwrap.py")
    from held_out.cases import CASES
    driver = (
        "import json,sys; from wordwrap import wrap\n"
        "cases=json.loads(sys.argv[1])\n"
        "import sys\n"
        "[sys.exit(2) for a,e in cases if wrap(*a)!=e]\n"
    )
    rc = subprocess.run([sys.executable,"-c",driver, json.dumps(CASES)], cwd=iso, capture_output=True).returncode
    if rc != 0:
        return Verdict("FAIL", ["held-out mismatch — impl likely hardcoded to visible cases"])
    return Verdict("PASS", ["contract + held-out green; only wordwrap.py changed"])
```
- [ ] **Step 4: Run, verify pass** (6 adversarial tests: genuine→PASS; stub/hardcoded/side-channel/deleted-contract/tampered-contract→FAIL).
- [ ] **Step 5: Commit** — `git add tools/agent-sdk-probe/verifier.py tools/agent-sdk-probe/tests/test_verifier.py && git commit -m "feat(agent-sdk-probe): held-out + tree-diff anti-false-PASS verifier"`

---

### Task 5: Stage-0 spike — install SDK + resolve the live call (go/no-go)

**Files:** Create `tools/agent-sdk-probe/spike.py`, `tools/agent-sdk-probe/READING.txt`; Modify `tools/agent-sdk-probe/models.py` (correct Kimi/GLM from findings).

**Concrete SDK shape to implement against** (confirm exact names at install; this is the expected form):
```python
# inside a per-model subprocess driver string:
import asyncio, json
from claude_agent_sdk import query, ClaudeAgentOptions
async def go():
    used=False; text=""
    opts = ClaudeAgentOptions(cwd=CWD, allowed_tools=["Read"], permission_mode="default", model=MODEL_ID)
    async for msg in query(prompt="Read READING.txt and quote line 1.", options=opts):
        # inspect msg for tool use + final text
        ...
    print(json.dumps({"tool_used": used, "content_ok": "forty-two" in text}))
asyncio.run(go())
```
Each model = `subprocess.run([sys.executable,"-c",driver], env={**os.environ_scrubbed, "ANTHROPIC_BASE_URL":spec.base_url, AUTH_VAR:key}, capture_output=True, text=True)` — scoped env, never mutate the parent. `AUTH_VAR` is `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` per the resolved `auth_style`.

- [ ] **Step 1: Install** — `.venv/bin/python3 -m pip install claude-agent-sdk` then confirm the Claude Code CLI it needs (`which claude` / npm global). Record versions for the README. Create `READING.txt` = `the answer is forty-two`.
- [ ] **Step 2: Implement `spike.py`** — `run_spike(spec) -> {name, ok, tool_used, content_ok, error}` using the per-model-subprocess shape above; scrub stderr/stdout via `scrub(out, [key], [spec.key_env])` before printing or returning.
- [ ] **Step 3: Run M3 first, then Kimi, GLM** — `set -a; source envs/agent-sdk-models-dev.env; set +a; .venv/bin/python3 tools/agent-sdk-probe/spike.py | tee /tmp/agent-sdk-spike.out` (output already scrubbed by spike.py). **Gate:** M3 must `tool_used=true, content_ok=true` or stop and reassess. For Kimi/GLM auth/model errors, adjust `models.py` (base_url / model_id / auth_style) and re-run; persistent failure ⇒ record `FAIL(endpoint/auth)`.
- [ ] **Step 4: Record findings** in `models.py` (resolved Kimi/GLM) + note for the README: SDK entrypoint, per-vendor auth-header shape, any shim needed, and the **D1 observation** (does the SDK run tools internally / could a host mediate them?).
- [ ] **Step 5: Commit** — `git add tools/agent-sdk-probe/spike.py tools/agent-sdk-probe/READING.txt tools/agent-sdk-probe/models.py && git commit -m "feat(agent-sdk-probe): Stage-0 spike; resolve vendor endpoints"`

---

### Task 6: Probe runner (per-model mutation, gated, scrubbed)

**Files:** Create `tools/agent-sdk-probe/probe.py`; Test `tools/agent-sdk-probe/tests/test_probe_wiring.py`.

**Interfaces — Consumes:** Tasks 1-5. **Produces:** `run_model(spec) -> dict`; `main()`.

- [ ] **Step 1: Implement `run_model(spec)`** — (a) `run_spike(spec)` first; if not `ok`, return `{name, status:"FAIL", reasons:["endpoint/auth: "+err]}` (Stage-0 GATE). (b) fresh tempdir ← `shutil.copytree(fixture)`; `git init` + `git add -A` + baseline commit BEFORE the model runs. (c) SDK subprocess with scoped env (key via `load_key`), `allowed_tools=["Read","Write","Edit","Bash"]`, `permission_mode="acceptEdits"`, `cwd`=tempdir, per-model `timeout=`; **capture stdout AND stderr** (`capture_output=True, text=True`). (d) `trace = scrub(stdout+"\n"+stderr, [load_key(spec) for all MODELS], [m.key_env for m in MODELS])` — scrub ALL keys+names, not just this model's; also wrap in try/except and scrub exception text. (e) `verify(tempdir)`. Return `{name, status, reasons, trace_excerpt(scrubbed)}`.
- [ ] **Step 2: Implement `main()`** — iterate `MODELS`, collect verdicts, print a table, write scrubbed `docs/agent-sdk-probe-results.md`. Per-model exceptions → `FAIL(error)` (scrubbed), continue.
- [ ] **Step 3: Offline wiring test** (no network)
```python
# tools/agent-sdk-probe/tests/test_probe_wiring.py
import unittest
from unittest.mock import patch
import probe
from models import ModelSpec
GENUINE = open(__file__).read()  # placeholder; replace with the GENUINE impl string from Task 4
class WiringTest(unittest.TestCase):
    def test_genuine_passes_and_trace_scrubbed(self):
        spec = ModelSpec("minimax-m3","u","i","AGENT_SDK_MINIMAX_KEY")
        canary = "CANARY-KEY-7e1"
        def fake_sdk(tempdir, *a, **k):
            (tempdir/"wordwrap.py").write_text(  # genuine impl
                "def wrap(t,w):\n import sys\n words,l,c=t.split(),[],''\n"
                " \n"  # (use the Task-4 GENUINE body here verbatim)
            )
            return ("ran ok "+canary, "")  # stdout, stderr containing a secret
        with patch.object(probe, "run_spike", lambda s: {"ok": True}), \
             patch.object(probe, "_sdk_mutation", fake_sdk), \
             patch.dict("os.environ", {"AGENT_SDK_MINIMAX_KEY": canary}, clear=False):
            r = probe.run_model(spec)
        self.assertEqual(r["status"], "PASS")
        self.assertNotIn(canary, r["trace_excerpt"])
```
(Implementer: factor the SDK call into `probe._sdk_mutation(tempdir, spec, key) -> (stdout, stderr)` so the test can monkeypatch it; paste the Task-4 GENUINE body into the fake. The point: `run_model` returns PASS on a genuine impl AND the canary secret is scrubbed from the trace.)
- [ ] **Step 4: Run wiring test, verify pass.** Commit — `git add tools/agent-sdk-probe/probe.py tools/agent-sdk-probe/tests/test_probe_wiring.py && git commit -m "feat(agent-sdk-probe): gated per-model runner + offline wiring/scrub test"`

---

### Task 7: README + full unit suite

**Files:** Create `tools/agent-sdk-probe/README.md`.

- [ ] **Step 1: README** — what a PASS means (scoped per spec), how to run (`source envs/...`; `spike.py` then `probe.py`), SDK/CLI versions, secret handling (gitignored env, scrub, **rotate-after-test**), resolved per-vendor endpoints/auth + the D1 observation. Note: an unmodified Kimi/GLM placeholder producing a 401 is `FAIL(endpoint/auth)`, not acceptance.
- [ ] **Step 2: Full suite green** — `cd tools/agent-sdk-probe && ../../.venv/bin/python3 -m unittest discover -s tests -v` → all green.
- [ ] **Step 3: Commit** — `git add tools/agent-sdk-probe/README.md && git commit -m "docs(agent-sdk-probe): README + run/secret instructions"`

---

### Task 8: THE RUN (the "test" stage)

- [ ] **Step 1: Pre-flight** — `set -a; source envs/agent-sdk-models-dev.env; set +a`.
- [ ] **Step 2: Run** — `.venv/bin/python3 tools/agent-sdk-probe/probe.py 2>&1 | tee /tmp/agent-sdk-probe-run.out` (probe output is pre-scrubbed; the tee file is a backstop).
- [ ] **Step 3: Confirm secret-free** — for each NON-empty key var, grep value AND name across the results doc, the run-out, and the spike-out; any hit ⇒ scrub gap, fix before committing:
```bash
for v in AGENT_SDK_MINIMAX_KEY AGENT_SDK_KIMI_KEY AGENT_SDK_GLM_KEY; do
  val="${(P)v}"; [ -z "$val" ] && continue
  grep -F -e "$val" -e "$v" docs/agent-sdk-probe-results.md /tmp/agent-sdk-probe-run.out /tmp/agent-sdk-spike.out && echo "LEAK in $v" || echo "$v clean"
done
```
- [ ] **Step 4: Commit results** — `git add docs/agent-sdk-probe-results.md && git commit -m "docs(agent-sdk-probe): per-model verdict — M3/Kimi/GLM-5.2"`. Orchestrator records the green-light/kill decision for the engine build-spike.
