"""Standing hook: the production coverage mutation sweep must exit 0.

rem5 acceptance (b)/(e): call-deletion and the other machinery mutants are
REFUSED by the real catalogue when the suite pins them; this test shells the
real script against the real repo so that deleting the module-scope validator
call (or emptying the catalogue past the floor) becomes a standing
``tests/arb_memory/`` failure — not a manual-run-only catch.

rem6 P1s: (1) without ARB_MEMORY_DSN this hook SKIPs loudly (SECURITY_SKIP),
matching every other DSN-dependent tests/arb_memory file and the CREATEROLE
loud-skip precedent — it must not hard-fail the default suite. (2) the sweep
runs in a throwaway ``git worktree`` of HEAD, never the live working tree, so
untracked scratch and concurrent edits cannot fail the hook or be eaten by
``git checkout --``. Direct CLI runs still refuse dirty trees on their target.

Runtime: control runs unit+live once; most table mutants refuse at import
(~fast); machinery mutants refuse at unit. Marked ``slow`` for discoverability
but NOT deselected by default addopts (only ``live_bakeoff`` is excluded), so
``.venv/bin/python -m pytest tests/arb_memory/`` with the DSN genuinely
executes the sweep. The hook does **not** pass ``--no-live``: live control
stays on when the DSN is present (machinery refuses at unit, so live cost is
essentially one control run).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SWEEP = REPO_ROOT / "scripts" / "coverage_mutation_sweep.py"
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"

# Loud skip reason so pytest -rs prints why the standing catch is INERT without
# a DSN (same class as CREATEROLE SECURITY_SKIP in test_claim_resolver.py).
_DSN_SECURITY_SKIP = (
    "SECURITY_SKIP: ARB_MEMORY_DSN required — standing coverage mutation sweep "
    "control requires live suite; without DSN this hook is INERT (proves nothing)"
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARB_MEMORY_DSN"),
    reason=_DSN_SECURITY_SKIP,
)


@pytest.mark.slow
def test_coverage_mutation_sweep_exits_zero_on_clean_tree():
    """Shell the real sweep in a throwaway worktree of HEAD; exit 0 required.

    Isolates inject-revert from the caller's live tree. Dirty/untracked files
    in the parent checkout must not fail this hook; the dirty-tree refusal
    remains load-bearing for *direct* ``scripts/coverage_mutation_sweep.py``
    runs against their target.
    """
    assert SWEEP.is_file(), f"missing sweep script: {SWEEP}"
    py = str(VENV_PY if VENV_PY.is_file() else sys.executable)

    tmp_parent = Path(tempfile.mkdtemp(prefix="coverage-sweep-wt-"))
    wt = tmp_parent / "wt"
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert add.returncode == 0, (
            f"git worktree add failed (rc={add.returncode}):\n"
            f"stdout: {add.stdout}\nstderr: {add.stderr}"
        )

        env = os.environ.copy()
        # Force the throwaway worktree's src so ambient PYTHONPATH cannot mask it.
        env["PYTHONPATH"] = str(wt / "src")

        # Live stage stays ON (DSN present via module skipif). --no-live is for
        # callers that want a unit-only fast path; this standing hook does not.
        proc = subprocess.run(
            [py, str(SWEEP), str(wt)],
            cwd=str(wt),
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        assert proc.returncode == 0, (
            f"coverage_mutation_sweep exit {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout[-4000:]}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}"
        )
        assert "TREE RESTORED CLEAN" in proc.stdout
        assert "survivors" in proc.stdout.lower() or "0 survivors" in proc.stdout
        # Catalogue floor: must have applied the production-sized set, not an empty loop.
        assert " mutations, 0 survivors" in proc.stdout
    finally:
        # Tear down registration even if the assertion path failed mid-run.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(tmp_parent, ignore_errors=True)
