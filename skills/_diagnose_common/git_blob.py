"""Git blob accessors for immutable repo-sha anchored artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path


def read_blob_at(repo: str | Path, repo_sha: str, relpath: str) -> bytes:
    """Read repo-relative file content from the immutable git blob at repo_sha."""
    result = subprocess.run(
        ["git", "show", f"{repo_sha}:{relpath}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def blob_exists_at(repo: str | Path, repo_sha: str, relpath: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{repo_sha}:{relpath}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0
