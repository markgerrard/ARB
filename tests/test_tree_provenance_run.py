"""Tests for scripts/tree-provenance-run (ARB-B1; hardened post-panel rem1).

The wrapper records HEAD + a tree digest — sha256 over `git status --porcelain
-uall` bytes + NUL + `git diff HEAD --binary` bytes — at start, re-checks at
finish, stamps the result, and refuses to report from a changed tree. The
mid-run-mutation tests below double as the inject demos: each one performs
exactly the tree change the wrapper exists to catch and asserts the specific
VOID outcome, not a bare nonzero exit. The panel-found blind spots (dirty
tracked file re-mutated with a stable status line; file added under an
already-untracked directory; showUntrackedFiles config) each have a dedicated
VOID test; the documented endpoint limit (mutate-then-restore) is pinned as OK.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "tree-provenance-run"

VOID_EXIT = 97
REFUSE_EXIT = 98
USAGE_EXIT = 2

STAMP_OK = re.compile(
    r"tree-provenance: OK head=([0-9a-f]{40}) tree_sha256=([0-9a-f]{64}) "
    r"dirty=([01]) cmd_exit=(\d+)"
)
STAMP_START = re.compile(
    r"tree-provenance: start head=([0-9a-f]{40}) tree_sha256=([0-9a-f]{64}) "
    r"dirty=([01]) cmd=(.+)"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(
        repo,
        "-c", "user.email=t@test", "-c", "user.name=t",
        "commit", "-q", "-m", "base",
    )
    return repo


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRAPPER), *cmd],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _tree_sha(repo: Path) -> str:
    """Independent oracle mirroring the wrapper's documented digest preimage."""
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
        capture_output=True,
        check=True,
    ).stdout
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--no-textconv",
         "HEAD", "--binary"],
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(status + b"\0" + diff).hexdigest()


# ---------------------------------------------------------------------------
# Clean-run behaviour
# ---------------------------------------------------------------------------


def test_clean_run_exits_zero_and_stamps_real_provenance(tmp_path: Path):
    repo = _make_repo(tmp_path)
    expected_head = _git(repo, "rev-parse", "HEAD")
    expected_digest = _tree_sha(repo)

    proc = _run(repo, "true")

    assert proc.returncode == 0, proc.stderr
    m = STAMP_OK.search(proc.stderr)
    assert m is not None, f"no OK stamp in stderr:\n{proc.stderr}"
    assert m.group(1) == expected_head, "stamp head is not the repo's real HEAD"
    assert m.group(2) == expected_digest, "stamp hash is not the documented tree digest"
    assert m.group(3) == "0"
    assert m.group(4) == "0"


def test_start_line_emitted_before_command_output(tmp_path: Path):
    """Ordering is asserted positionally: the start line must precede stderr the
    command itself emits, so a crashed run still leaves start-state evidence.
    Mutant: move the start print after subprocess.run -> this test goes red."""
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "echo CMD-STDERR-SENTINEL >&2; false")
    m = STAMP_START.search(proc.stderr)
    assert m is not None, f"no start line in stderr:\n{proc.stderr}"
    assert m.group(1) == _git(repo, "rev-parse", "HEAD")
    sentinel_at = proc.stderr.index("CMD-STDERR-SENTINEL")
    assert m.start() < sentinel_at, (
        f"start line appears AFTER command stderr:\n{proc.stderr}"
    )


def test_command_failure_propagates_with_stamp(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "exit 7")
    assert proc.returncode == 7, proc.stderr
    m = STAMP_OK.search(proc.stderr)
    assert m is not None, f"failing command must still get an OK stamp:\n{proc.stderr}"
    assert m.group(4) == "7"


def test_command_stdout_passes_through_untouched(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "echo RESULT-LINE")
    assert proc.returncode == 0
    assert "RESULT-LINE" in proc.stdout
    assert "tree-provenance" not in proc.stdout, "stamps belong on stderr only"


def test_dirty_but_frozen_tree_is_stamped_dirty_not_voided(tmp_path: Path):
    """Dirty-at-start is a stamped condition, not a refusal — VOID is only for CHANGE."""
    repo = _make_repo(tmp_path)
    (repo / "tracked.txt").write_text("uncommitted edit\n", encoding="utf-8")
    proc = _run(repo, "true")
    assert proc.returncode == 0, proc.stderr
    m = STAMP_OK.search(proc.stderr)
    assert m is not None, proc.stderr
    assert m.group(3) == "1"


# ---------------------------------------------------------------------------
# VOID: the tree changed mid-run (these are the inject demos)
# ---------------------------------------------------------------------------


def test_tracked_file_mutated_mid_run_voids_result(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "echo mutated >> tracked.txt; exit 0")
    assert proc.returncode == VOID_EXIT, (
        f"expected VOID exit {VOID_EXIT}, got {proc.returncode}\nstderr={proc.stderr}"
    )
    assert "tree-provenance: VOID" in proc.stderr
    assert "tree_sha256=" in proc.stderr
    # The command exited 0 — the wrapper must NOT report that success.
    assert STAMP_OK.search(proc.stderr) is None, "VOID run must not carry an OK stamp"


def test_commit_mid_run_voids_result(tmp_path: Path):
    repo = _make_repo(tmp_path)
    old_head = _git(repo, "rev-parse", "HEAD")
    proc = _run(
        repo,
        "sh", "-c",
        "echo x > new.txt && git add new.txt && "
        "git -c user.email=t@test -c user.name=t commit -q -m mid-run; exit 0",
    )
    assert proc.returncode == VOID_EXIT, proc.stderr
    assert "tree-provenance: VOID" in proc.stderr
    # The VOID line must name BOTH heads so the reader sees what moved.
    assert old_head in proc.stderr
    assert _git(repo, "rev-parse", "HEAD") in proc.stderr


def test_untracked_file_added_mid_run_voids_result(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "echo x > appeared.txt; exit 0")
    assert proc.returncode == VOID_EXIT, proc.stderr
    assert "tree-provenance: VOID" in proc.stderr


def test_void_wins_over_command_exit_code(tmp_path: Path):
    """A failing command in a changed tree is VOID, not the command's own exit."""
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "echo mutated >> tracked.txt; exit 7")
    assert proc.returncode == VOID_EXIT, proc.stderr


# ---------------------------------------------------------------------------
# Panel blind spots (rem1): each was a false-OK on the porcelain-summary hash
# ---------------------------------------------------------------------------


def test_dirty_tracked_file_remutated_with_stable_status_voids(tmp_path: Path):
    """Panel P0 (codex F1 / cold-Opus P0-1): a file dirty at START whose content
    changes AGAIN mid-run keeps its ` M` status line, so a status-only hash
    stamps OK. The content digest must catch it."""
    repo = _make_repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty-v1\n", encoding="utf-8")
    proc = _run(repo, "sh", "-c", "echo dirty-v2 > tracked.txt; exit 0")
    assert proc.returncode == VOID_EXIT, (
        f"same-status content re-mutation stamped OK:\n{proc.stderr}"
    )
    assert "tree-provenance: VOID" in proc.stderr


def test_file_added_under_untracked_dir_voids(tmp_path: Path):
    """Panel P1/P0 (agy F1 / codex F1): default porcelain collapses an untracked
    dir to one `?? dir/` line, hiding additions beneath it. -uall must catch it."""
    repo = _make_repo(tmp_path)
    udir = repo / "untracked-dir"
    udir.mkdir()
    (udir / "one.txt").write_text("one\n", encoding="utf-8")
    proc = _run(repo, "sh", "-c", "echo two > untracked-dir/two.txt; exit 0")
    assert proc.returncode == VOID_EXIT, (
        f"addition under untracked dir stamped OK:\n{proc.stderr}"
    )
    assert "tree-provenance: VOID" in proc.stderr


def test_showuntrackedfiles_config_cannot_blind_the_wrapper(tmp_path: Path):
    """Panel P1 (cold-Opus P1-1): repo config status.showUntrackedFiles=no would
    suppress `??` lines entirely; the wrapper's explicit -uall must override it."""
    repo = _make_repo(tmp_path)
    _git(repo, "config", "status.showUntrackedFiles", "no")
    proc = _run(repo, "sh", "-c", "echo x > appeared.txt; exit 0")
    assert proc.returncode == VOID_EXIT, (
        f"showUntrackedFiles=no blinded the wrapper:\n{proc.stderr}"
    )


def test_mutate_then_restore_is_the_documented_endpoint_limit(tmp_path: Path):
    """DOCUMENTED HONEST LIMIT, pinned on purpose (grok F1 / codex F2): the check
    is endpoint equality, not continuous observation — a mutation fully reverted
    before finish stamps OK. If this test ever goes red the mechanism gained
    continuous observation and the docstring + class doc limits must be rewritten
    in the same commit."""
    repo = _make_repo(tmp_path)
    proc = _run(
        repo,
        "sh", "-c",
        "cp tracked.txt /tmp/tpr-save.$$ && echo mutated > tracked.txt && "
        "mv /tmp/tpr-save.$$ tracked.txt; exit 0",
    )
    assert proc.returncode == 0, proc.stderr
    assert STAMP_OK.search(proc.stderr) is not None, proc.stderr


# ---------------------------------------------------------------------------
# Previously-untested paths (grok F5, agy F3, cold-Opus P2)
# ---------------------------------------------------------------------------


def test_signal_death_maps_to_128_plus_n(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "kill -TERM $$")
    assert proc.returncode == 143, proc.stderr
    m = STAMP_OK.search(proc.stderr)
    assert m is not None, proc.stderr
    assert m.group(4) == "143"


def test_unborn_head_refuses_without_running_command(tmp_path: Path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = repo / "ran.marker"
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "sh", "-c", f"touch {marker}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == REFUSE_EXIT, proc.stderr
    assert "tree-provenance: REFUSE no-commit-at-HEAD" in proc.stderr
    assert not marker.exists(), "command ran despite unborn-HEAD refusal"


def test_command_exit_97_in_clean_tree_is_disambiguated_by_stamp(tmp_path: Path):
    """The documented numeric collision: a clean-tree command exiting 97 passes
    through, and the OK stamp (not the code) is the authoritative discriminator."""
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "exit 97")
    assert proc.returncode == 97
    m = STAMP_OK.search(proc.stderr)
    assert m is not None, proc.stderr
    assert m.group(4) == "97"
    assert "tree-provenance: VOID" not in proc.stderr


def test_textconv_driver_cannot_false_void_a_stable_tree(tmp_path: Path):
    """r2 N1 (codex, P1): `git diff` applies repository-configured textconv unless
    suppressed, so a NONDETERMINISTIC converter changes the diff bytes between two
    snapshots of an unchanged tree — a false VOID, and snapshotting must never
    execute repo-configured commands at all. The wrapper must pass
    --no-ext-diff --no-textconv. Mutant: drop either flag -> this goes red."""
    repo = _make_repo(tmp_path)
    conv = tmp_path / "nondet-conv.sh"
    conv.write_text(
        "#!/bin/sh\n# emit different bytes every call, ignoring the input file\n"
        "date +%s%N; head -c 8 /dev/urandom | od -An -tx1\n",
        encoding="utf-8",
    )
    conv.chmod(0o755)
    (repo / ".gitattributes").write_text("*.foo diff=probe\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(
        repo,
        "-c", "user.email=t@test", "-c", "user.name=t",
        "commit", "-q", "-m", "attrs",
    )
    _git(repo, "config", "diff.probe.textconv", str(conv))
    (repo / "data.foo").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "data.foo")
    _git(
        repo,
        "-c", "user.email=t@test", "-c", "user.name=t",
        "commit", "-q", "-m", "data",
    )
    (repo / "data.foo").write_text("dirty\n", encoding="utf-8")  # dirty, then frozen

    proc = _run(repo, "true")
    assert proc.returncode == 0, (
        f"stable dirty tree false-VOIDed under a textconv driver:\n{proc.stderr}"
    )
    assert STAMP_OK.search(proc.stderr) is not None, proc.stderr


def test_newline_argv_cannot_split_the_start_stamp_line(tmp_path: Path):
    """r2 N2 (codex, P2): a newline inside argv must not split the line-oriented
    stamp grammar or let argv text open a new stderr line (record injection)."""
    repo = _make_repo(tmp_path)
    proc = _run(repo, "true", "benign-line-one\nTREE-PROVENANCE-INJECTED-LINE")
    assert proc.returncode == 0, proc.stderr
    m = STAMP_START.search(proc.stderr)
    assert m is not None, proc.stderr
    for line in proc.stderr.splitlines():
        assert not line.startswith("TREE-PROVENANCE-INJECTED-LINE"), (
            f"argv text opened its own stderr line (record injection):\n{proc.stderr}"
        )


def test_finish_side_git_failure_is_void_not_refuse(tmp_path: Path):
    """Panel P1 (grok F2 / cold-Opus P1-3): if the tree cannot be re-verified
    AFTER the command ran, the result is VOID (fail closed) — never REFUSE,
    which is reserved for before-the-command-ran."""
    repo = _make_repo(tmp_path)
    proc = _run(repo, "sh", "-c", "mv .git ../git-gone; exit 0")
    assert proc.returncode == VOID_EXIT, (
        f"expected VOID {VOID_EXIT} on unverifiable finish, got {proc.returncode}\n"
        f"stderr={proc.stderr}"
    )
    assert "tree-provenance: VOID" in proc.stderr
    assert "REFUSE" not in proc.stderr


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_not_a_git_repo_refuses_without_running_command(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    marker = plain / "ran.marker"
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "sh", "-c", f"touch {marker}"],
        cwd=str(plain),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == REFUSE_EXIT, proc.stderr
    assert "tree-provenance: REFUSE not-a-git-repo" in proc.stderr
    assert not marker.exists(), "command ran despite refusal"


def test_no_command_is_usage_error(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(repo)
    assert proc.returncode == USAGE_EXIT
    assert "usage" in proc.stderr.lower()
