"""Unit tests for completion_gate (pure git-state classification)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_redis_bridge import completion_gate as cg


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> str:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / ".gitignore").write_text("*.log\nscratch/\n")
    (path / "a.txt").write_text("one")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return cg.git_head(path)  # type: ignore[return-value]


def test_no_changes_clean(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "no_changes_clean"
    assert out["dirty_files"] == []


def test_committed_clean(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    (tmp_path / "b.txt").write_text("two")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "more")
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "committed_clean"
    assert out["head_after"] != head


def test_dirty_uncommitted(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    (tmp_path / "b.txt").write_text("uncommitted")
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "dirty_uncommitted"
    assert "b.txt" in out["dirty_files"]


def test_dirty_after_commit_is_bounced(tmp_path: Path) -> None:
    """Committed AND then left more dirt — must NOT pass as committed_clean."""
    head = _init_repo(tmp_path)
    (tmp_path / "b.txt").write_text("two")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "more")
    (tmp_path / "c.txt").write_text("leftover")  # dirty after the commit
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "dirty_after_commit"
    assert out["state"] in cg.BOUNCE_STATES


def test_gitignored_file_does_not_trip(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    (tmp_path / "debug.log").write_text("noise")  # matches *.log
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "x").write_text("noise")
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "no_changes_clean"


def test_untracked_non_ignored_trips(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    (tmp_path / "temp_results.json").write_text("{}")
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "dirty_uncommitted"
    assert "temp_results.json" in out["dirty_files"]


def test_not_a_git_repo(tmp_path: Path) -> None:
    out = cg.evaluate(tmp_path, None)
    assert out["state"] == "not_a_git_repo"
    out2 = cg.evaluate(tmp_path, "deadbeef")  # head_before set but cwd not a repo
    assert out2["state"] == "not_a_git_repo"


def test_preexisting_dirt_is_not_counted(tmp_path: Path) -> None:
    """A dispatch into an already-dirty shared workdir is NOT bounced for files
    it never touched — only dirt introduced during the turn counts."""
    head = _init_repo(tmp_path)
    (tmp_path / "preexisting.txt").write_text("was here before")  # untracked before the turn
    before = cg.dirty_files(tmp_path)
    assert "preexisting.txt" in before

    # Turn made NO new changes -> pass, despite the pre-existing dirt.
    out = cg.evaluate(tmp_path, head, before)
    assert out["state"] == "no_changes_clean"
    assert out["dirty_files"] == []


def test_new_dirt_on_top_of_preexisting_is_bounced(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    (tmp_path / "preexisting.txt").write_text("before")
    before = cg.dirty_files(tmp_path)
    (tmp_path / "agent-new.txt").write_text("introduced during the turn")
    out = cg.evaluate(tmp_path, head, before)
    assert out["state"] == "dirty_uncommitted"
    assert out["dirty_files"] == ["agent-new.txt"]  # only the new file, not preexisting


def test_file_in_new_directory_is_listed_individually(tmp_path: Path) -> None:
    """git compresses an all-untracked NEW directory to a single ``?? dir/``
    entry; ``dirty_files`` must defeat that (``-uall``) so orchestrator-commit's
    path-level allow-list matches the expected FILE, not a parent dir. This is
    the exact PA-bench-2b failure: a test landing in a brand-new
    ``tests/Unit/Services/Corpus/`` dir bounced because the dir != the file."""
    head = _init_repo(tmp_path)
    nested = tmp_path / "tests" / "Unit" / "Services" / "Corpus"
    nested.mkdir(parents=True)
    (nested / "AuthorityResolverTest.php").write_text("<?php")
    out = cg.evaluate(tmp_path, head)
    assert out["state"] == "dirty_uncommitted"
    assert out["dirty_files"] == ["tests/Unit/Services/Corpus/AuthorityResolverTest.php"]


def test_pass_and_bounce_sets_partition() -> None:
    assert cg.PASS_STATES.isdisjoint(cg.BOUNCE_STATES)
    assert "shared_cwd_unchecked" in cg.PASS_STATES


def test_checkout_fingerprint_detects_edit_to_preexisting_dirty_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    dirty = tmp_path / "a.txt"
    dirty.write_text("dirty before")
    before = cg.checkout_snapshot(tmp_path)

    dirty.write_text("escaped edit")
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "base_checkout_changed"
    assert "a.txt" in change["dirty_files"]


def test_checkout_fingerprint_ignores_unchanged_preexisting_dirt(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("dirty before")
    before = cg.checkout_snapshot(tmp_path)

    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "no_changes_clean"


def test_checkout_fingerprint_reports_only_new_dirt_as_new(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("dirty before")
    before = cg.checkout_snapshot(tmp_path)

    (tmp_path / "fresh.txt").write_text("written during the window")
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "base_checkout_changed"
    assert change["new_dirty_files"] == ["fresh.txt"]
    assert "a.txt" in change["dirty_files"]


def test_one_sided_git_error_is_unverifiable_not_a_change(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    before = cg.checkout_snapshot(tmp_path)

    real = cg._git_bytes

    def failing_diff(path, *args):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(args=["git"], returncode=128, stdout=b"", stderr=b"index.lock")
        return real(path, *args)

    monkeypatch.setattr(cg, "_git_bytes", failing_diff)
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "fingerprint_unverifiable"
    assert change["new_dirty_files"] == []


def test_sentinel_change_survives_git_probe_error(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    rel = cg.SENTINEL_PATHS[0]
    before = cg.checkout_snapshot(tmp_path)
    _set_watched(tmp_path, rel, "changed")

    real = cg._git_bytes

    def failing_diff(path, *args):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(
                args=["git"], returncode=128, stdout=b"", stderr=b"index.lock"
            )
        return real(path, *args)

    monkeypatch.setattr(cg, "_git_bytes", failing_diff)
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "fingerprint_unverifiable"
    assert change["sentinel_changed"] == [rel]


def test_bridge_worktree_container_churn_is_not_a_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = cg.checkout_snapshot(tmp_path)

    sibling = tmp_path / ".claude" / "worktrees" / "sibling"
    sibling.mkdir(parents=True)
    (sibling / "x.txt").write_text("a parallel worktree task's churn")
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "no_changes_clean"


def test_head_probe_failure_is_unverifiable_not_a_change(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    before = cg.checkout_snapshot(tmp_path)

    real = cg._git

    def failing_head(path, *args):
        if args and args[0] == "rev-parse" and "HEAD" in args:
            return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="lock")
        return real(path, *args)

    monkeypatch.setattr(cg, "_git", failing_head)
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "fingerprint_unverifiable"


def _set_watched(path: Path, rel: str, content: str | None) -> None:
    watched = path / rel
    if content is None:
        watched.unlink(missing_ok=True)
        return
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("before_content", "after_content"),
    [(None, "created"), ("before", "modified"), ("before", None)],
)
def test_transient_watched_change_keeps_fingerprint_stable(
    tmp_path: Path, before_content: str | None, after_content: str | None
) -> None:
    _init_repo(tmp_path)
    rel = cg.TRANSIENT_PATHS[0]
    _set_watched(tmp_path, rel, before_content)
    before = cg.checkout_snapshot(tmp_path)

    _set_watched(tmp_path, rel, after_content)
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["state"] == "no_changes_clean"
    assert change["fingerprint_before"] == change["fingerprint_after"]
    assert change["transient_changed"] == [rel]
    assert change["sentinel_changed"] == []


@pytest.mark.parametrize(
    ("before_content", "after_content"),
    [(None, "created"), ("before", "modified"), ("before", None)],
)
def test_sentinel_watched_change_is_seen_while_git_ignored(
    tmp_path: Path, before_content: str | None, after_content: str | None
) -> None:
    _init_repo(tmp_path)
    rel = cg.SENTINEL_PATHS[0]
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + f"\n/{rel}\n", encoding="utf-8")
    _set_watched(tmp_path, rel, before_content)
    before = cg.checkout_snapshot(tmp_path)

    _set_watched(tmp_path, rel, after_content)
    change = cg.compare_checkout_snapshot(tmp_path, before)

    ignored = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "-q", rel],
        check=False,
    )
    assert ignored.returncode == 0
    assert change["state"] == "no_changes_clean"
    assert change["sentinel_changed"] == [rel]
    assert change["transient_changed"] == []


def test_both_watched_changes_are_attributed_independently(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = cg.checkout_snapshot(tmp_path)

    for rel in (*cg.TRANSIENT_PATHS, *cg.SENTINEL_PATHS):
        _set_watched(tmp_path, rel, "changed")
    change = cg.compare_checkout_snapshot(tmp_path, before)

    assert change["transient_changed"] == list(cg.TRANSIENT_PATHS)
    assert change["sentinel_changed"] == list(cg.SENTINEL_PATHS)
