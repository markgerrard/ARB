"""Completion gate — refuse to report an implementation turn "complete" when it
left uncommitted edits in the working tree.

Pure, subprocess-git helpers (mirrors ``git_branch`` in bridge.py). The gate is
applied in ``Bridge.process_request`` right after the engine returns, before any
terminal status/reply is published.

State model (computed from two INDEPENDENT dimensions — head movement AND tree
cleanliness, so a "commit then leave more dirt" turn is still bounced):

    committed_clean      HEAD advanced, tree clean        -> pass
    no_changes_clean     HEAD same,     tree clean        -> pass (read-only/Q&A)
    dirty_after_commit   HEAD advanced, tree dirty        -> BOUNCE
    dirty_uncommitted    HEAD same,     tree dirty        -> BOUNCE
    not_a_git_repo       cwd is not a git work tree       -> pass (inert)
    shared_cwd_unchecked parallel default-workdir dispatch -> pass (cannot attribute)
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

PASS_STATES = frozenset(
    {"committed_clean", "no_changes_clean", "not_a_git_repo", "shared_cwd_unchecked"}
)
BOUNCE_STATES = frozenset({"dirty_uncommitted", "dirty_after_commit"})


def _git(path: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_bytes(path: Path | str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        check=False,
    )


def is_git_repo(path: Path | str) -> bool:
    return _git(path, "rev-parse", "--is-inside-work-tree").returncode == 0


def git_head(path: Path | str) -> str | None:
    res = _git(path, "rev-parse", "HEAD")
    return res.stdout.strip() if res.returncode == 0 else None


def dirty_files(path: Path | str) -> list[str]:
    """Tracked/staged/unstaged + untracked-non-ignored paths. ``.gitignore``d
    files are NOT reported (default porcelain behaviour), so scratch artifacts
    don't trip the gate. ``--untracked-files=all`` defeats git's untracked-
    DIRECTORY compression: without it a file landing in a brand-new directory is
    reported as a single ``?? dir/`` entry, which then fails path-level
    allow-list membership in orchestrator-commit (the dir != the expected file)."""
    res = _git(path, "status", "--porcelain", "--untracked-files=all")
    if res.returncode != 0:
        return []
    return [line[3:] for line in res.stdout.splitlines() if len(line) > 3]


# Bridge-created worktrees live INSIDE the base checkout. Their churn (a sibling
# worktree task being created, written to, or removed mid-window) is bridge-owned
# by construction and must never read as a base-checkout escape.
WORKTREE_CONTAINER = ".claude/worktrees"
TRANSIENT_PATHS = (".claude/faba-current-round.json",)
SENTINEL_PATHS = (".claude/settings.local.json",)


def _is_bridge_worktree_path(rel: str) -> bool:
    return rel == WORKTREE_CONTAINER or rel.startswith(WORKTREE_CONTAINER + "/")


def _watched_hashes(base: Path) -> tuple[dict[str, str], bool]:
    watched: dict[str, str] = {}
    errors = False
    for rel in (*TRANSIENT_PATHS, *SENTINEL_PATHS):
        try:
            watched[rel] = hashlib.sha256((base / rel).read_bytes()).hexdigest()
        except FileNotFoundError:
            watched[rel] = "absent"
        except OSError:
            watched[rel] = "absent"
            errors = True
    return watched, errors


def checkout_snapshot(path: Path | str) -> dict[str, Any] | None:
    """Fingerprint every non-ignored base-checkout change, including content
    changes to paths that were already dirty before a worktree turn.

    ``dirty_files`` alone is insufficient: it records path presence, so editing
    the same pre-existing dirty path is invisible. A binary diff covers tracked
    staged/unstaged content and modes; explicit hashes cover untracked files.
    """
    if not is_git_repo(path):
        return None
    base = Path(path)
    digest = hashlib.sha256()
    watched, errors = _watched_hashes(base)
    # Probe failures must be distinguishable from probe answers: git_head()
    # returns None and dirty_files() returns [] on BOTH "empty answer" and
    # "git errored", and a one-sided error would fabricate (or mask) a change.
    head_res = _git(base, "rev-parse", "HEAD")
    if head_res.returncode != 0:
        errors = True
        head = None
    else:
        head = head_res.stdout.strip()
    digest.update((head or "").encode())
    status = _git(base, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        errors = True
        snapshot_dirty: list[str] = []
    else:
        snapshot_dirty = [
            line[3:]
            for line in status.stdout.splitlines()
            if len(line) > 3
            and not _is_bridge_worktree_path(line[3:])
            and line[3:] not in TRANSIENT_PATHS
        ]
    # Tracked diff is intentionally untouched. Excluding a tracked transient
    # would require a pathspec; that is deferred with the future env knob.
    tracked = _git_bytes(base, "diff", "--binary", "HEAD", "--")
    if tracked.returncode != 0:
        # A transient git failure (index.lock contention under parallel tasks,
        # etc.) on ONE side of a before/after pair must read as "unverifiable",
        # not as a change — a sentinel folded into the fingerprint fabricates
        # an escape whenever only one side errored.
        errors = True
    else:
        digest.update(tracked.stdout)
    untracked = _git_bytes(base, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode != 0:
        errors = True
    else:
        for raw in sorted(part for part in untracked.stdout.split(b"\0") if part):
            rel = os.fsdecode(raw)
            if _is_bridge_worktree_path(rel) or rel in TRANSIENT_PATHS:
                continue
            digest.update(b"\0path\0" + raw)
            candidate = base / rel
            try:
                if candidate.is_symlink():
                    digest.update(b"symlink\0" + os.fsencode(os.readlink(candidate)))
                elif candidate.is_dir():
                    # Nested repos surface as a bare directory entry; presence
                    # is all git tracks for them, so presence is all we hash.
                    digest.update(b"dir\0")
                else:
                    digest.update(candidate.read_bytes())
            except OSError:
                errors = True
    return {
        "fingerprint": digest.hexdigest(),
        "head": head,
        "dirty_files": snapshot_dirty,
        "watched": watched,
        "errors": errors,
    }


def compare_checkout_snapshot(
    path: Path | str,
    before: dict[str, Any] | None,
) -> dict[str, Any]:
    after = checkout_snapshot(path)
    if before is None or after is None:
        return {
            "state": "not_a_git_repo",
            "head_before": before.get("head") if before else None,
            "head_after": after.get("head") if after else None,
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": [],
            "sentinel_changed": [],
        }
    before_watched = before.get("watched", {})
    after_watched = after.get("watched", {})
    transient_changed = [
        rel for rel in TRANSIENT_PATHS if before_watched.get(rel) != after_watched.get(rel)
    ]
    sentinel_changed = [
        rel for rel in SENTINEL_PATHS if before_watched.get(rel) != after_watched.get(rel)
    ]
    if before.get("errors") or after.get("errors"):
        return {
            "state": "fingerprint_unverifiable",
            "head_before": before["head"],
            "head_after": after["head"],
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": transient_changed,
            "sentinel_changed": sentinel_changed,
        }
    changed = before["fingerprint"] != after["fingerprint"]
    preexisting = set(before["dirty_files"])
    return {
        "state": "base_checkout_changed" if changed else "no_changes_clean",
        "head_before": before["head"],
        "head_after": after["head"],
        "dirty_files": after["dirty_files"] if changed else [],
        # Paths that became dirty DURING the window — what an escape report may
        # name. Pre-existing dirt stays out: it was there before the turn.
        "new_dirty_files": (
            [f for f in after["dirty_files"] if f not in preexisting] if changed else []
        ),
        "transient_changed": transient_changed,
        "sentinel_changed": sentinel_changed,
        "fingerprint_before": before["fingerprint"],
        "fingerprint_after": after["fingerprint"],
    }


def evaluate(
    cwd: Path | str,
    head_before: str | None,
    dirty_before: list[str] | None = None,
) -> dict[str, Any]:
    """Classify the working tree after a turn. ``head_before`` / ``dirty_before``
    are the HEAD sha and porcelain dirty set captured BEFORE the turn. Only dirt
    INTRODUCED during the turn counts — a dispatch into an already-dirty shared
    workdir is not bounced for pre-existing files it never touched. A fresh
    bridge-created worktree starts clean, so ``dirty_before`` is empty there."""
    if head_before is None or not is_git_repo(cwd):
        return {"state": "not_a_git_repo", "head_before": head_before, "head_after": None, "dirty_files": []}

    head_after = git_head(cwd)
    before = set(dirty_before or [])
    new_dirty = [f for f in dirty_files(cwd) if f not in before]
    head_changed = head_after != head_before

    if new_dirty:
        state = "dirty_after_commit" if head_changed else "dirty_uncommitted"
    else:
        state = "committed_clean" if head_changed else "no_changes_clean"

    return {"state": state, "head_before": head_before, "head_after": head_after, "dirty_files": new_dirty}


def missing_artifacts(cwd: Path | str, expected: list[str]) -> list[str]:
    """Task-completeness check (orthogonal to git cleanliness): which of the
    dispatch's ``expected_artifacts`` do not yet exist under ``cwd``. The gate
    answers 'is the tree clean?'; this answers 'is the task done?' — a turn that
    commits file 1 of 3 leaves a clean tree but missing artifacts."""
    base = Path(cwd)
    return [rel for rel in expected if not (base / rel).exists()]


def shared_cwd_unchecked(head_before: str | None) -> dict[str, Any]:
    """Result for a parallel default-workdir dispatch where post-turn dirtiness
    cannot be attributed to this task (another concurrent task may share the
    tree). The gate does not enforce; enforced completion needs a worktree."""
    return {"state": "shared_cwd_unchecked", "head_before": head_before, "head_after": None, "dirty_files": []}
