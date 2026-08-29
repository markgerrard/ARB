# Mac Smoke Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Catch the class of macOS defect that is textually correct and behaviourally wrong, and bind that corpus to the scripts so new binary dependencies cannot land unnoticed.

**Architecture:** A Darwin-only pytest module holds behavioural assertions about the shell primitives the repo actually invokes. A shared non-test module holds the `COVERED` and `NON_STOCK` sets. The existing platform-independent lint imports those sets and enforces two static rules, so Linux contributors are protected without owning a Mac.

**Tech Stack:** Python 3.12, pytest, POSIX sh subprocesses. No bus, no credentials, no seats, no network.

**Design spec:** `docs/superpowers/specs/2026-08-12-mac-smoke-run-design.md`

## Global Constraints

- Run tests from the worktree: `PYTHONPATH=src .venv/bin/python -m pytest <path> -q`. `tests/conftest.py` enforces import provenance; running from another clone reports on a different tree.
- **Every mutation in a sidecar MUST change the target file's byte length.** A zero-delta mutation produced a gate that passed once then refused three times while the mutated code never reached the interpreter (`8eca6e4f`).
- Every changed test pair needs a `.mutations.json` sidecar, or `changed_test_mutation_gate` refuses with `DECL-MISSING`.
- A mutation target must not be on the pair's refused import surface (the test file itself, `conftest.py`, package `__init__.py`).
- Assert the **specific** failure, never a bare refusal (`docs/defect-classes/refusal-is-ambient-assert-the-code.md`).
- Do not edit historical review reports from this workstream or `.claude/wave1-evidence/**`. They are records of real runs.
- Commit trailers: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>` and the `Claude-Session:` line.
- Verify the gate by reading its **full output**, never `| tail` or `grep -c`. A count is not the thing counted.

## Deviation from the spec, and why

The spec says the corpus exports `COVERED`. Planning found that this makes the sidecar
impossible: a module testing *macOS itself* has no repo code to mutate, so it has no legal
mutation target, and the `no_legal_target` exemption is base-ref owner-authored — the gate
refuses a candidate-authored one (`DECL-INVALID — no_legal_target is candidate-authored`).

`COVERED` and `NON_STOCK` therefore live in a shared non-test module,
`tests/macos_primitives_covered.py`, imported by both test files. That module is a legal
mutation target for both pairs: deleting an entry turns the coverage guard red. No owner
action is required, and the coupling the spec wanted is unchanged.

## File Structure

| File | Responsibility |
|---|---|
| `tests/macos_primitives_covered.py` (create) | The `COVERED` and `NON_STOCK` sets. No logic. Legal mutation target for both pairs. |
| `tests/test_macos_primitives.py` (create) | Darwin-only behavioural assertions, three classes. |
| `tests/test_macos_primitives.mutations.json` (create) | Proves the corpus can fail. |
| `tests/test_script_portability.py` (modify) | Adds two static rules that consume the shared sets. |
| `tests/test_script_portability.mutations.json` (create) | Does not exist today; the gate demands it once the file changes. |
| `scripts/agent-inbox-watcher` (modify, Task 4) | Guard the bare `sha256sum` call. |
| `scripts/codex-inbox-once` (modify, Task 4) | Guard the bare `sha256sum` call. |

---

### Task 1: Shared sets, corpus skeleton, and Class 3 (shell resolution)

**Files:**
- Create: `tests/macos_primitives_covered.py`
- Create: `tests/test_macos_primitives.py`
- Create: `tests/test_macos_primitives.mutations.json`

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/macos_primitives_covered.py` exporting `COVERED: frozenset[str]` and `NON_STOCK: dict[str, str]`. Tasks 2, 3, 5 and 6 import these by those exact names.

- [ ] **Step 1: Write the shared sets module**

Create `tests/macos_primitives_covered.py`:

```python
"""Primitives the Mac seat depends on, and which of them are not stock macOS.

Deliberately NOT a test module. tests/test_macos_primitives.py asserts the
behaviour of everything named here; tests/test_script_portability.py enforces
the static rules that consume it. Keeping the sets in a third file gives both
pairs a legal mutation target — a module that tests the platform has no repo
code to mutate, and the no-legal-target exemption is owner-authored.
"""

from __future__ import annotations

# Every external binary a shell script in scripts/ may invoke in command
# position. Adding a dependency without a behavioural test here fails the
# coverage guard in tests/test_script_portability.py.
COVERED: frozenset[str] = frozenset({"grep"})

# Binaries NOT guaranteed by a stock macOS install, mapped to the stock
# equivalent. Every call site of one of these must be guarded with
# `command -v <name>`; scripts/arb-pi-orch:48 is the pattern.
#
# sha256sum is the proving case: it exists on mini-dev at /sbin/sha256sum,
# which is not part of a stock install, so "the binary is present" is exactly
# the check that passes here and fails on a clean Mac.
NON_STOCK: dict[str, str] = {"sha256sum": "shasum -a 256"}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_macos_primitives.py`:

```python
"""macOS behavioural corpus for the primitives the Mac seat depends on.

The companion to tests/test_script_portability.py, which lints for TEXTUAL
hazards on every platform. This module catches the class no lint can reach: a
string that is textually correct and behaves differently on macOS. The proving
case is the advice the docs carried until 65418f4c — `$(command -v grep)`,
which on a shell FUNCTION returns the function name, resolving to precisely
the thing it was written to bypass.

Each test asserts the REPO's assumption, not a general platform fact, so it
fails when the repo's dependence breaks rather than when Apple changes
something unused.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from macos_primitives_covered import COVERED

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="behavioural assertions about macOS; the textual class is covered "
    "on every platform by tests/test_script_portability.py",
)


def _sh(script: str) -> subprocess.CompletedProcess:
    """Run one line under /bin/sh, capturing both streams."""
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)


def test_covered_is_not_empty():
    """Guard the guard: an empty COVERED makes the coverage check vacuous."""
    assert COVERED, (
        "COVERED is empty — the coverage guard in test_script_portability.py "
        "would pass without asserting anything"
    )


def test_command_v_on_a_shell_function_returns_a_name_not_a_path():
    """The defect behind the broken advice: this is why $(command -v grep) fails."""
    result = _sh("grep() { :; }; command -v grep")
    assert result.stdout.strip() == "grep", (
        "expected the function NAME; a path here would mean the docs' old "
        f"$(command -v grep) form was safe after all. got {result.stdout.strip()!r}"
    )


def test_command_grep_bypasses_the_function_and_reaches_a_binary():
    result = _sh("grep() { echo SHADOWED; }; command grep --version")
    assert "SHADOWED" not in result.stdout, "command did not suppress function lookup"
    assert result.returncode == 0, result.stderr


def test_the_resolved_grep_supports_line_buffered():
    """Every Monitor invocation depends on this flag."""
    result = _sh("printf 'a\\nb\\n' | command grep --line-buffered -E 'b'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "b"
```

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONPATH=src:tests .venv/bin/python -m pytest tests/test_macos_primitives.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'macos_primitives_covered'` if `tests/` is not on the path. If it fails this way, change the import in `tests/test_macos_primitives.py` to `from tests.macos_primitives_covered import COVERED` and re-run; keep whichever form resolves, and use the same form in Tasks 5 and 6.

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py -q`
Expected: PASS, 4 tests. On Linux: 4 skipped — that is correct, not a failure.

- [ ] **Step 5: Write the mutation sidecar**

Create `tests/test_macos_primitives.mutations.json`. The replacement is shorter than the find string, satisfying the byte-length constraint:

```json
{"schema": 1, "mutations": [{"id": "M1", "kind": "defect", "label": "empty COVERED so the coverage guard becomes vacuous", "file": "tests/macos_primitives_covered.py", "find": "COVERED: frozenset[str] = frozenset({\"grep\"})", "replace": "COVERED: frozenset[str] = frozenset()", "expect_failed": ["tests/test_macos_primitives.py::test_covered_is_not_empty"]}]}
```

- [ ] **Step 6: Prove the mutation turns it red**

Run:
```bash
.venv/bin/python -c "import json;json.dump(json.load(open('tests/test_macos_primitives.mutations.json'))['mutations'],open('/tmp/m1.json','w'))"
PYTHONPATH=src .venv/bin/python scripts/mutation_sweep.py --spec /tmp/m1.json \
  --test-cmd ".venv/bin/python -m pytest tests/test_macos_primitives.py -q -rA --no-header" --repo "$PWD"
```
Expected: `M1` shows `1 failed`, naming `test_covered_is_not_empty`; then `RESTORED BASELINE` and `tree after sweep: CLEAN`. The sweep refuses a dirty tree, so commit or stash before running it.

- [ ] **Step 7: Commit**

```bash
git add tests/macos_primitives_covered.py tests/test_macos_primitives.py tests/test_macos_primitives.mutations.json
git commit -m "test(macos): behavioural corpus skeleton and the shell-resolution class"
```

---

### Task 2: Class 2 — flag and behaviour divergence

**Files:**
- Modify: `tests/test_macos_primitives.py` (append)
- Modify: `tests/macos_primitives_covered.py` (extend `COVERED`)
- Modify: `tests/test_macos_primitives.mutations.json`

**Interfaces:**
- Consumes: `_sh` and `COVERED` from Task 1.
- Produces: `COVERED` additionally containing `chmod`, `sed`, `stat`, `date`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_macos_primitives.py`:

```python
def test_bsd_chmod_rejects_end_of_options(tmp_path):
    """The 2026-08-11 defect: `chmod 600 -- f` fails on BSD, succeeds on GNU."""
    target = tmp_path / "f"
    target.write_text("x")
    result = _sh(f"chmod 600 -- {target}")
    assert result.returncode != 0, "BSD chmod accepted `--`; the lint's premise is gone"
    assert "--" in result.stderr


def test_bsd_sed_in_place_requires_an_empty_suffix(tmp_path):
    target = tmp_path / "f"
    target.write_text("x\n")
    assert _sh(f"sed -i 's/x/y/' {target}").returncode != 0
    assert _sh(f"sed -i '' 's/x/y/' {target}").returncode == 0
    assert target.read_text() == "y\n"


def test_stat_uses_dash_f_not_dash_c(tmp_path):
    """scripts/claude-hooks/* already try -f first and fall back to -c."""
    target = tmp_path / "f"
    target.write_text("abc")
    assert _sh(f"stat -c '%s' {target}").returncode != 0
    result = _sh(f"stat -f%z {target}")
    assert result.returncode == 0
    assert result.stdout.strip() == "3"


def test_bsd_date_rejects_the_gnu_date_flag():
    assert _sh("date -d 2026-01-01").returncode != 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py -q`
Expected: PASS already — these assert measured macOS behaviour, so they pass on first write. That is expected for a corpus; Step 4's mutation is what proves they can fail.

- [ ] **Step 3: Extend COVERED**

In `tests/macos_primitives_covered.py`, replace the `COVERED` line with:

```python
COVERED: frozenset[str] = frozenset({"grep", "chmod", "sed", "stat", "date"})
```

- [ ] **Step 4: (REMOVED by owner ruling, 2026-08-12) — do not add a mutation here**

The mutation formerly specified here (M2: drop `chmod` from `COVERED`, expecting
`test_covered_is_not_empty` to fail) was **structurally incapable of failing**: that test
asserts `assert COVERED`, i.e. non-empty, so removing one of five entries leaves it truthy
and the mutation SURVIVES. The Task 2 implementer caught this and escalated rather than
building it; the gate confirmed RED with `SURVIVOR`.

Task 6 Step 4 already carries the equivalent mutation aimed at the coverage guard — the only
check that can observe it. Do NOT reintroduce a mutation at this step.

Class 2's fail-ability comes from M1, which empties `COVERED` entirely.

Note: M1's `find` string changed in Step 3, so update M1's `find` to the new five-element line and its `replace` to `frozenset()`.

- [ ] **Step 5: Run tests and the sweep**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py -q`
Expected: PASS, 8 tests.

Then re-run the sweep command from Task 1 Step 6. Expected: both mutations red, baseline restored, tree clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_macos_primitives.py tests/macos_primitives_covered.py tests/test_macos_primitives.mutations.json
git commit -m "test(macos): flag and behaviour divergence between BSD and GNU"
```

---

### Task 3: Class 1 — availability, without asserting presence

**Files:**
- Modify: `tests/test_macos_primitives.py` (append)
- Modify: `tests/macos_primitives_covered.py`

**Interfaces:**
- Consumes: `_sh`, `NON_STOCK`.
- Produces: `COVERED` additionally containing `sha256sum`, `shasum`, `awk`.

- [ ] **Step 1: Write the failing test**

Add the import at the top of `tests/test_macos_primitives.py`, alongside the existing one:

```python
from macos_primitives_covered import COVERED, NON_STOCK
```

Append:

```python
def test_a_stock_equivalent_exists_for_every_non_stock_binary(tmp_path):
    """NOT `the binary is present` — that is the check this host passes falsely.

    mini-dev carries /sbin/sha256sum, which is not part of a stock macOS
    install, so presence here proves nothing about a clean Mac. Assert instead
    that the stock equivalent exists and agrees byte-for-byte.
    """
    fixture = tmp_path / "payload"
    fixture.write_bytes(b"the quick brown fox\n")

    for name, stock in NON_STOCK.items():
        stock_run = _sh(f"{stock} {fixture} | awk '{{print $1}}'")
        assert stock_run.returncode == 0, (
            f"{name} is not stock macOS and its stated equivalent {stock!r} "
            f"does not run here: {stock_run.stderr.strip()}"
        )
        digest = stock_run.stdout.strip()
        assert len(digest) == 64, f"expected a sha256 hex digest, got {digest!r}"

        present = _sh(f"command -v {name}")
        if present.returncode == 0:
            other = _sh(f"{name} {fixture} | awk '{{print $1}}'").stdout.strip()
            assert other == digest, (
                f"{name} and {stock} disagree on this platform: {other} != {digest}"
            )
```

- [ ] **Step 2: Run to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py::test_a_stock_equivalent_exists_for_every_non_stock_binary -q`
Expected: PASS. On mini-dev both branches execute because `/sbin/sha256sum` exists; on a stock Mac only the `shasum` branch runs and the test still passes.

- [ ] **Step 3: Extend COVERED**

```python
COVERED: frozenset[str] = frozenset(
    {"grep", "chmod", "sed", "stat", "date", "sha256sum", "shasum", "awk"}
)
```

Update M1's `find`/`replace` strings in `tests/test_macos_primitives.mutations.json` to match the new line. (There is no M2 in this file at this point — Task 2's M2 was removed by owner ruling; Step 4 below adds a NEW M2 targeting `NON_STOCK`.)

- [ ] **Step 4: Add the mutation that proves THIS test can fail** (added by owner ruling, 2026-08-12)

Without this the new test passes on first write and **nothing in the sidecar can make it fail** —
M1 targets `COVERED` and kills a different test (`test_covered_is_not_empty`). Under this plan's
own rule, a corpus test is allowed to pass on first write *because the sidecar supplies its
fail-ability*; for this test the sidecar supplied none.

Add to the `mutations` array in `tests/test_macos_primitives.mutations.json`:

```json
{"id": "M2", "kind": "defect", "label": "corrupt the stock equivalent so it emits the wrong digest length", "file": "tests/macos_primitives_covered.py", "find": "NON_STOCK: dict[str, str] = {\"sha256sum\": \"shasum -a 256\"}", "replace": "NON_STOCK: dict[str, str] = {\"sha256sum\": \"shasum -a 1\"}", "expect_failed": ["tests/test_macos_primitives.py::test_a_stock_equivalent_exists_for_every_non_stock_binary"]}
```

Do NOT instead empty `NON_STOCK`: an empty dict makes the test's `for` loop body never execute,
which is a vacuous pass and a SURVIVING mutation — the same defect class as the deleted M2.
Changing the algorithm keeps the loop live and trips the test's own `len(digest) == 64`
assertion with a named message (delta 58→56 bytes, nonzero; find string occurs exactly once).

- [ ] **Step 5: Run tests and the sweep**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py -q`
Expected: PASS, 9 tests. Then re-run the Task 1 Step 6 sweep; expect BOTH M1 and M2 red and a
clean restore. Note the sweep tool refuses a dirty tree, so the sidecar must be committed before
the sweep runs.

- [ ] **Step 6: Commit**

```bash
git add tests/test_macos_primitives.py tests/macos_primitives_covered.py tests/test_macos_primitives.mutations.json
git commit -m "test(macos): assert a stock equivalent, never that a non-stock binary is present"
```

---

### Task 4: Prerequisite — guard the two unguarded `sha256sum` call sites

**Files:**
- Modify: `scripts/agent-inbox-watcher:84`
- Modify: `scripts/codex-inbox-once:40`

**ROUTING DECISION REQUIRED BEFORE STARTING.** These files are the peer's (`f5fe3d86`) and prod's (`cb1c109f`). The design spec records that this fix likely wants routing to them rather than being made unilaterally. Confirm with the orchestrator before editing; if routed, this task is done elsewhere and Task 5 waits on it landing.

**Interfaces:**
- Consumes: nothing.
- Produces: both scripts computing a digest without depending on a non-stock binary. Task 5's lint asserts this holds.

- [ ] **Step 1: Read the current call site**

Run: `sed -n '80,92p' scripts/agent-inbox-watcher` and `sed -n '36,48p' scripts/codex-inbox-once`
Both contain: `digest=$(sha256sum "${source}" | awk '{print $1}')`

- [ ] **Step 2: Apply the guarded form in both scripts**

Replace that single line in each file with:

```sh
    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(sha256sum "${source}" | awk '{print $1}')
    else
        digest=$(shasum -a 256 "${source}" | awk '{print $1}')
    fi
```

This is the pattern `scripts/arb-pi-orch:48-49` already uses.

- [ ] **Step 3: Verify both scripts still parse**

Run: `bash -n scripts/agent-inbox-watcher && bash -n scripts/codex-inbox-once && echo "both parse"`
Expected: `both parse`

- [ ] **Step 4: Verify the reject path still produces a digest**

Run:
```bash
T=$(mktemp); printf 'x\n' > "$T"
if command -v sha256sum >/dev/null 2>&1; then a=$(sha256sum "$T" | awk '{print $1}'); else a=""; fi
b=$(shasum -a 256 "$T" | awk '{print $1}')
echo "sha256sum=$a"; echo "shasum=$b"; [ -z "$a" ] || [ "$a" = "$b" ] && echo AGREE || echo DISAGREE
rm -f "$T"
```
Expected: `AGREE`

- [ ] **Step 5: Commit**

```bash
git add scripts/agent-inbox-watcher scripts/codex-inbox-once
git commit -m "fix(scripts): sha256sum is not stock macOS, so guard it like arb-pi-orch does"
```

---

### Task 5: Lint — a non-stock binary must be guarded at its call site

**Files:**
- Modify: `tests/test_script_portability.py` (append)
- Create: `tests/test_script_portability.mutations.json`

**Blocked by:** Task 4 — COMPLETE as of 2026-08-12 (both halves on `origin/dev`: `673776c2` peer, `4559ca1b` prod).

**Preconditions added 2026-08-12:**

1. **Merge `origin/dev` before starting.** This task's base file `tests/test_script_portability.py`
   changed twice on 2026-08-12. The merge must NOT happen while an implementer is live in this
   worktree (`docs/defect-classes/workdir-mutated-while-run-in-flight.md`).
2. **Delete the peer's narrow lint when this one lands.** `origin/dev` carries
   `test_sha256sum_is_guarded_because_macos_ships_shasum` plus its decay test in
   `tests/test_script_portability.py` (`673776c2`). The peer asked that this general lint delete
   them: "two lints for one class is how an exemption quietly ends up in the weaker one."
   Do NOT generalise their `SHA256SUM_ROUTED_TO_OWNER` set — it is now EMPTY, so that machinery
   would be built for a case that no longer exists. Re-add if a future routing case appears.
3. **The lint will come up CLEAN, and that is correct.** The tree is now guarded. Do NOT add a
   synthetic offender to prove the lint works — Step 5's mutation sidecar is the fail-ability
   proof, and a synthetic offender would dirty the tree.

**Interfaces:**
- Consumes: `NON_STOCK`, and the existing `_shell_scripts()` and `_code_lines()` helpers in `tests/test_script_portability.py`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Add at the top of `tests/test_script_portability.py`, after the existing imports:

```python
from macos_primitives_covered import COVERED, NON_STOCK
```

Append. **The call pattern is a single named constant** — the lint and the parity test below
must both use it, never their own copies (corrected by owner ruling, 2026-08-12; see the note
after the parity test for why):

```python
# One definition. The lint and its parity test both format this; a change here
# is a change both of them see.
NON_STOCK_CALL = r"(?<![\w-]){binary}\s"


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_non_stock_binaries_are_guarded_somewhere_in_their_script(script):
    """A binary absent from a stock macOS must never be called bare.

    sha256sum exists on some Macs (mini-dev has /sbin/sha256sum) and on every
    Linux box, so an unguarded call is green everywhere it is tested and red
    on a clean Mac. scripts/arb-pi-orch:48 shows the guarded form.
    """
    text = script.read_text()
    offenders = []
    for name in NON_STOCK:
        guarded = f"command -v {name}" in text
        for number, line in _code_lines(script):
            if re.search(NON_STOCK_CALL.format(binary=re.escape(name)), line) and not guarded:
                offenders.append(f"{script.name}:{number}: {line.strip()}")
    assert not offenders, (
        "these call a binary that is not stock macOS without a `command -v` "
        "guard; they pass on Linux and on any Mac that happens to have it.\n"
        "Use the three-way form from scripts/arb-pi-orch:46-52 "
        "(shasum, then sha256sum, then openssl dgst).\n"
        + "\n".join(offenders)
    )
```

**The lookbehind is `(?<![\w-])`, NOT `(?<![\w/-])`** (corrected by owner ruling, 2026-08-12).
Excluding `/` lets a non-stock binary invoked by ABSOLUTE PATH through:
`/opt/homebrew/bin/sha256sum foo` was flagged by the narrow lint this replaces and was silently
NOT flagged by the general one. A replacement lint must not be weaker than what it deletes, in
any dimension. `\w` already prevents the `mysha256sum` false positive; the `/` bought nothing.

The assertion message keeps the deleted lint's remediation pointer. A lint that says only "this
is wrong" costs the next reader the lookup that the previous lint had already done for them.

Also append this parity test, so the absolute-path case is PINNED rather than assumed — nothing
proved it either way, which is how the narrowing survived the review of the diff that introduced
it:

```python
def test_an_absolute_path_call_is_still_a_call():
    """A non-stock binary reached by absolute path is exactly this lint's case.

    /opt/homebrew/bin/sha256sum does not exist on a stock Mac, so an absolute-path
    invocation fails there just as a bare one does. The narrow lint this replaced
    caught it; the general lint must not regress that.
    """
    pattern = NON_STOCK_CALL.format(binary=re.escape("sha256sum"))
    assert re.search(pattern, "/opt/homebrew/bin/sha256sum foo"), (
        "absolute-path call not detected — the lookbehind is excluding '/' again"
    )
    assert not re.search(pattern, "mysha256sum foo"), (
        "over-matched a binary whose name merely ends in sha256sum"
    )
```

**Why the constant, and not a pattern written out twice** (owner ruling, 2026-08-12, correcting
this plan's own previous amendment): the first version of this parity test hard-coded its own
copy of the regex. Reintroducing the `/` into the LINT then left the whole suite green — 112
passed — because the test was asserting against its duplicate, not against the lint. It was a
check that could not fail for the thing it existed to protect, which is the defect class this
entire corpus is built to catch, reproduced inside the guard against it.

One definition removes the failure mode rather than testing around it.

**Verify the test can actually fail before believing it:** temporarily change `NON_STOCK_CALL`
to `r"(?<![\w/-]){binary}\s"`, run the file, and confirm
`test_an_absolute_path_call_is_still_a_call` goes RED. Restore, and confirm the tree is clean.
A parity test that stays green under that edit is decorative and must not be committed.

- [ ] **Step 2: Run to verify it fails if Task 4 is not done**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_script_portability.py -q`
Expected: PASS if Task 4 landed. If it FAILS naming `agent-inbox-watcher` or `codex-inbox-once`, Task 4 has not landed — stop and complete it first.

- [ ] **Step 3: Write the sidecar**

Create `tests/test_script_portability.mutations.json`. The target is a script, which is off the refused surface, and the replacement is shorter:

```json
{"schema": 1, "mutations": [{"id": "M1", "kind": "defect", "label": "strip the command -v sha256sum guard arb-pi-orch already uses", "file": "scripts/arb-pi-orch", "find": "elif command -v sha256sum >/dev/null 2>&1; then", "replace": "elif true; then", "expect_failed": ["tests/test_script_portability.py::test_non_stock_binaries_are_guarded_somewhere_in_their_script[arb-pi-orch]"]}]}
```

- [ ] **Step 4: Confirm the find string matches exactly once**

Run: `grep -F -c 'elif command -v sha256sum >/dev/null 2>&1; then' scripts/arb-pi-orch`
Expected: `1`. If it is not 1, widen the string with surrounding context until it is — an ambiguous match mutates a line other than the one named.

- [ ] **Step 5: Prove the mutation turns it red**

Run:
```bash
.venv/bin/python -c "import json;json.dump(json.load(open('tests/test_script_portability.mutations.json'))['mutations'],open('/tmp/m2.json','w'))"
PYTHONPATH=src .venv/bin/python scripts/mutation_sweep.py --spec /tmp/m2.json \
  --test-cmd ".venv/bin/python -m pytest tests/test_script_portability.py -q -rA --no-header" --repo "$PWD"
```
Expected: `M1` shows the parametrised `[arb-pi-orch]` case failing, then a clean restore.

- [ ] **Step 6: Commit**

```bash
git add tests/test_script_portability.py tests/test_script_portability.mutations.json
git commit -m "test(portability): a non-stock binary must be guarded at its call site"
```

---

### Task 6: Lint — the coverage guard that binds scripts to COVERED

**Files:**
- Modify: `tests/test_script_portability.py` (append)
- Modify: `tests/test_script_portability.mutations.json`

**Interfaces:**
- Consumes: `COVERED`, `_shell_scripts()`, `_code_lines()`.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_script_portability.py`:

```python
# Words that appear in command position but are not external binaries whose
# macOS behaviour needs asserting. Each entry carries its reason; a false
# positive costs one line here, a false negative is silent.
#
# Honest limit: COMMAND_POSITION below requires trailing whitespace, so a
# command at end-of-line or inside some substitutions is NOT matched. The
# allowlist is therefore loud about NEW names it does see, but the regex
# itself is conservative and can miss forms. Step 3 of this task checks it
# against the one false-positive shape already measured; it does not prove
# the extractor is complete, and no claim is made that it is.
COMMAND_POSITION_ALLOWLIST: dict[str, str] = {
    "if": "shell keyword", "then": "shell keyword", "else": "shell keyword",
    "elif": "shell keyword", "fi": "shell keyword", "for": "shell keyword",
    "while": "shell keyword", "do": "shell keyword", "done": "shell keyword",
    "case": "shell keyword", "esac": "shell keyword", "return": "shell builtin",
    "exit": "shell builtin", "echo": "shell builtin", "printf": "shell builtin",
    "cd": "shell builtin", "set": "shell builtin", "export": "shell builtin",
    "local": "shell builtin", "read": "shell builtin", "shift": "shell builtin",
    "trap": "shell builtin", "eval": "shell builtin", "exec": "shell builtin",
    "source": "shell builtin", "command": "shell builtin", "test": "shell builtin",
    "true": "shell builtin", "false": "shell builtin", "wait": "shell builtin",
    "python3": "interpreter, not a coreutil whose flags differ",
    "redis-cli": "vendored client, same binary on both platforms",
    "git": "same flags on both platforms",
    "jq": "same flags on both platforms",
}

# A word in command position: line start, or after | || && ; ( or $(
COMMAND_POSITION = re.compile(r"(?:^|[|;&(]|\$\()\s*([a-z][\w.-]*)\s")


def _binaries_in_command_position(script: Path) -> set[str]:
    text = script.read_text()
    defined = set(re.findall(r"^\s*([\w.-]+)\s*\(\)\s*\{", text, re.M))
    names = set()
    for _number, line in _code_lines(script):
        for match in COMMAND_POSITION.finditer(line):
            names.add(match.group(1))
    return names - defined


def test_every_invoked_binary_has_a_macos_behaviour_test():
    """Bind the corpus to the tree so a new dependency cannot land unnoticed.

    Static, and deliberately in THIS file rather than the Darwin-only corpus:
    the contributors who introduce these bugs work on Linux, and a check that
    only fires on a Mac would never reach them.
    """
    uncovered: dict[str, str] = {}
    for script in _shell_scripts():
        for name in _binaries_in_command_position(script):
            if name in COVERED or name in COMMAND_POSITION_ALLOWLIST:
                continue
            uncovered.setdefault(name, script.name)
    assert not uncovered, (
        "these binaries are invoked by shell scripts but have no macOS "
        "behaviour test in tests/test_macos_primitives.py. Add one and extend "
        "COVERED, or add an allowlist entry with a reason:\n"
        + "\n".join(f"  {n} (first seen in {s})" for n, s in sorted(uncovered.items()))
    )
```

- [ ] **Step 2: Run it and triage the output**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_script_portability.py::test_every_invoked_binary_has_a_macos_behaviour_test -q`
Expected: FAIL, listing binaries. This first run is a triage step, not a defect. For each name reported, decide once:
- a coreutil whose macOS behaviour could differ → add a behavioural test in Task 2's style and add it to `COVERED`
- anything else → add to `COMMAND_POSITION_ALLOWLIST` **with a reason string**

Re-run until it passes. Do not silence a name without a reason; the reason is the artefact a reviewer checks.

- [ ] **Step 3: Verify the extractor is not fooled by the known false-positive shapes**

Run: `PYTHONPATH=src .venv/bin/python -c "
import sys; sys.path.insert(0, 'tests')
from pathlib import Path
import test_script_portability as t
print(sorted(t._binaries_in_command_position(Path('scripts/agent-dispatch'))))
"`
Expected: `timeout` does NOT appear. A word-frequency harvest reported 31 `timeout` invocations in `scripts/` when the true count of bare `timeout` commands is zero — comments, `--turn-timeout` flags, `TURN_TIMEOUT` variables. If `timeout` appears, the extractor is matching outside command position; tighten `COMMAND_POSITION` before continuing.

- [ ] **Step 4: Add a mutation that reaches the coverage guard**

Add to `tests/test_script_portability.mutations.json`:

```json
{"id": "M2", "kind": "defect", "label": "drop grep from COVERED so an invoked binary loses its behaviour test", "file": "tests/macos_primitives_covered.py", "find": "\"grep\", \"chmod\"", "replace": "\"chmod\"", "expect_failed": ["tests/test_script_portability.py::test_every_invoked_binary_has_a_macos_behaviour_test"]}
```

- [ ] **Step 5: Run the full gate and read its complete output**

Run:
```bash
PYTHONPATH=src .venv/bin/python scripts/changed_test_mutation_gate.py --base origin/dev --repo "$PWD" > /tmp/gate.txt 2>&1
echo "exit=$?"; cat /tmp/gate.txt
```
Expected: exit 0, one `PASS[pair:...]` line per changed pair, no `FAIL` line. Read the whole file — a `tail` or a `grep -c` hid a `SURVIVOR` line on 2026-08-11 and a green reading was reported that the gate did not support.

- [ ] **Step 6: Run the gate twice more to check stability**

Run the Step 5 command twice more. Expected: exit 0 each time. A gate that passes once and refuses later is the zero-delta failure shape; if it appears, check every mutation's `len(replace) != len(find)`.

- [ ] **Step 7: Commit**

```bash
git add tests/test_script_portability.py tests/test_script_portability.mutations.json tests/macos_primitives_covered.py
git commit -m "test(portability): bind invoked binaries to the macOS behaviour corpus"
```

---

## Verification of the whole plan

- [ ] `PYTHONPATH=src .venv/bin/python -m pytest tests/test_macos_primitives.py tests/test_script_portability.py -q` — all pass on macOS; the corpus skips on Linux.
- [ ] `changed_test_mutation_gate --base origin/dev` exits 0 across three consecutive runs, full output read each time.
- [ ] `.venv/bin/python scripts/check-doc-index` exits 0.
- [ ] `git diff --numstat` on the branch shows no deletions in historical review reports or `.claude/wave1-evidence/**`.
