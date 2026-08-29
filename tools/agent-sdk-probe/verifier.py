"""Anti-false-PASS verifier for the agent-sdk mutation probe.

The verifier judges the model's implementation against a pristine contract and
a held-out oracle the model never saw, while enforcing that only wordwrap.py
changed relative to the baseline commit.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"


@dataclass
class Verdict:
    status: str
    reasons: list[str] = field(default_factory=list)


def _is_artifact(path: str) -> bool:
    # Python/pytest run artifacts the model creates when it runs the test as the
    # prompt instructs ("Run the test to confirm"). They are not evidence that the
    # model edited other SOURCE, so they must not trip the "only wordwrap.py" rule.
    # Match path COMPONENTS, not substrings (delta review P1): a file literally named
    # `not__pycache__helper.py` must NOT be treated as an artifact.
    parts = path.replace("\\", "/").split("/")
    if any(p in ("__pycache__", ".pytest_cache") for p in parts):
        return True
    return path.endswith(".pyc")


def _canon(out) -> str:
    """Canonical form for hashing held-out outputs (MUST match held_out/cases.py)."""
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _changed_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if _is_artifact(path):
            continue
        out.append(path)
    return out


def _pytest_ok(testfile: Path, cwd: Path) -> bool:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(testfile)],
        cwd=cwd,
        capture_output=True,
        check=False,
    ).returncode == 0


def verify(model_repo: Path) -> Verdict:
    impl = model_repo / "wordwrap.py"
    if not impl.exists():
        return Verdict("FAIL", ["no wordwrap.py"])
    if impl.read_text() == (FIXTURE / "wordwrap.py").read_text():
        return Verdict("FAIL", ["implementation unchanged (stub)"])

    model_contract = model_repo / "test_contract.py"
    if not model_contract.exists():
        return Verdict("FAIL", ["test_contract.py missing"])
    if model_contract.read_text() != (FIXTURE / "test_contract.py").read_text():
        return Verdict("FAIL", ["test_contract.py differs from pristine fixture"])

    changed = _changed_files(model_repo)
    if changed != ["wordwrap.py"]:
        return Verdict("FAIL", [f"files other than wordwrap.py changed: {changed}"])

    clean = Path(tempfile.mkdtemp())
    shutil.copy(FIXTURE / "test_contract.py", clean / "test_contract.py")
    shutil.copy(impl, clean / "wordwrap.py")
    if not _pytest_ok(clean / "test_contract.py", clean):
        return Verdict("PARTIAL", ["impl edited but contract test red"])

    iso = Path(tempfile.mkdtemp())
    shutil.copy(impl, iso / "wordwrap.py")
    from held_out.cases import CASES

    # SECURITY (delta review P0, agy): the held-out oracle ships only sha256(expected),
    # never the expected plaintext (see held_out/cases.py). The impl subprocess gets
    # ONLY the inputs (stdin) and prints its plaintext outputs; the PARENT hashes those
    # and compares to the stored digests. So neither argv/stdin NOR any file the impl can
    # read (cases.py, git history, rglob) contains a usable answer — a PASS requires
    # genuinely COMPUTING the correct output (hash preimage resistance). This also kills
    # the marker-spoof: a faked __RESULTS__ must contain hash-matching outputs = correct
    # outputs = a real implementation.
    inputs = [list(a) for a, _h in CASES]
    expected_hashes = [h for _a, h in CASES]
    driver = (
        "import json,sys\n"
        "from wordwrap import wrap\n"
        "inp=json.loads(sys.stdin.read())\n"
        "print('__RESULTS__'+json.dumps([wrap(*a) for a in inp], ensure_ascii=False))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(inputs),
        cwd=iso,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return Verdict("FAIL", ["held-out run errored (impl raised on held-out inputs)"])
    outputs = None
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULTS__"):
            try:
                outputs = json.loads(line[len("__RESULTS__"):])
            except json.JSONDecodeError:
                outputs = None
    if not isinstance(outputs, list) or len(outputs) != len(expected_hashes):
        return Verdict("FAIL", ["held-out produced no/!malformed results"])
    actual = [hashlib.sha256(_canon(o).encode()).hexdigest() for o in outputs]
    if actual != expected_hashes:
        return Verdict("FAIL", ["held-out hash mismatch - impl likely hardcoded to visible cases"])
    return Verdict("PASS", ["contract + held-out (hash) green; only wordwrap.py changed"])
