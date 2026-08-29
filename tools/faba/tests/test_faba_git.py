"""Tests for the reopen consumer's git delta seam (faba_git)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
if str(FABA) not in sys.path:
    sys.path.insert(0, str(FABA))

from faba_git import changed_paths_since  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "pf3@example.invalid")
    _git(repo, "config", "user.name", "PF3")
    return repo


def test_changed_paths_between_basis_and_head(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("1\n", encoding="utf-8")
    (repo / "b.py").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.py", "b.py")
    _git(repo, "commit", "-m", "base")
    basis = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.py").write_text("2\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "c.py").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "a.py", "sub/c.py")
    _git(repo, "commit", "-m", "change")

    changed = changed_paths_since(basis, repo)
    assert sorted(changed) == ["a.py", "sub/c.py"]  # b.py untouched, absent


def test_no_change_returns_empty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "base")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert changed_paths_since(head, repo) == []


def test_unknown_basis_raises(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "base")
    try:
        changed_paths_since("0000000000000000000000000000000000000000", repo)
    except subprocess.CalledProcessError:
        return
    raise AssertionError("expected a bad basis ref to raise")
