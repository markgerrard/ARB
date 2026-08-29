"""Git delta helper for the reopen consumer (ADR art-81438f2f5a5c4955 open
item #12). Kept out of faba_schema, which is pure/subprocess-free: this is the
launch/driver-layer seam that turns a prior-round basis ref into the set of
repo-relative paths changed since it, which reopened_finding_ids then matches
against each closed finding's reopen-if scope.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def changed_paths_since(basis: str, repo_root: Path | None = None) -> list[str]:
    """Repo-relative paths that differ between `basis` and HEAD (committed state
    only — the round verifies committed/snapshotted trees, per worktree hygiene).
    Raises CalledProcessError on an unknown ref so a bad basis fails the round
    loudly rather than silently reopening nothing."""
    argv = ["git"]
    if repo_root is not None:
        argv += ["-C", str(repo_root)]
    argv += ["diff", "--name-only", basis, "HEAD"]
    out = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]
