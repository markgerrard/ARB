"""Shared machinery for the mutation sweeps.

Not a test module (leading underscore); imported by `mutation_check_*.py`.

The two non-obvious rules encoded here, both learned the hard way on
2026-08-06 while building the first sweep:

1. STAGE THE WHOLE WORKTREE, not just `src/`. `tests/conftest.py` enforces
   import-provenance -- it compares where each module resolved from against
   this checkout and aborts if they differ. Staging only `src/` trips it, and
   pytest exits 4 (COLLECTION ERROR) having run nothing.

2. ASSERT THE SPECIFIC EXIT CODE. Because of (1), a sweep that checks
   `returncode != 0` reports every mutation as "caught" while none of them ran.
   Only exit 1 (TESTS_FAILED) is a catch; 4 is an invalid trial. This is
   `docs/defect-classes/refusal-is-ambient-assert-the-code.md` applied to a
   test harness: when failure is the ambient outcome, a bare non-zero proves
   nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST = [sys.executable, "-m", "pytest", "-q", "--no-header",
          "-p", "no:cacheprovider"]

EXIT_OK, EXIT_TESTS_FAILED = 0, 1


def stage_worktree(dest: Path) -> Path:
    shutil.copytree(REPO / "src", dest / "src", symlinks=True)
    shutil.copytree(REPO / "tests", dest / "tests", symlinks=True)
    for cfg in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        if (REPO / cfg).exists():
            shutil.copy2(REPO / cfg, dest / cfg)
    return dest


def run_suite(root: Path, test_file: str) -> tuple[int, str]:
    proc = subprocess.run(
        PYTEST + test_file.split(),
        cwd=root,
        env={"PATH": "/usr/bin:/bin",
             "PYTHONPATH": str(root / "src"),
             "HOME": str(Path.home())},
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def sweep(target: str, test_file: str, mutations) -> int:
    """Run `mutations` against `target`, each expected to kill a named test."""
    with tempfile.TemporaryDirectory() as tmp:
        root = stage_worktree(Path(tmp) / "wt")
        rc, out = run_suite(root, test_file)
        if rc != EXIT_OK:
            print(f"CONTROL FAILED (rc={rc}) - staged baseline is not green.")
            print("\n".join(out.splitlines()[-6:]))
            return 2
        print(f"control (staged, unmutated): PASS  "
              f"[{out.strip().splitlines()[-1]}]")

    failures = 0
    for label, find, replace, expect_test in mutations:
        with tempfile.TemporaryDirectory() as tmp:
            root = stage_worktree(Path(tmp) / "wt")
            path = root / "src" / target
            original = path.read_text()
            if find not in original:
                print(f"  SKIP (anchor not found): {label}")
                failures += 1
                continue
            path.write_text(original.replace(find, replace, 1))

            rc, out = run_suite(root, test_file)
            if rc == EXIT_OK:
                print(f"  NOT CAUGHT: {label}  <-- the test cannot fail")
                failures += 1
            elif rc != EXIT_TESTS_FAILED:
                print(f"  INVALID (rc={rc}, suite did not run): {label}")
                print("      " + (out.splitlines() or ["?"])[-1][:120])
                failures += 1
            elif expect_test not in out:
                print(f"  WRONG TEST caught it: {label}")
                print(f"      expected {expect_test} to fail; it did not")
                failures += 1
            else:
                print(f"  caught by {expect_test}: {label}")

    print()
    if failures:
        print(f"MUTATION SWEEP FAILED: {failures} of {len(mutations)} unproven")
        return 1
    print(f"MUTATION SWEEP PASSED: all {len(mutations)} mutations caught "
          f"by their named test")
    return 0
