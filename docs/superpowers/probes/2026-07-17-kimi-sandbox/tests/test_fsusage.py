"""Tests for the fs_usage parser + pid-tree attribution + contamination gate (v4 §4).

The attribution problem (r2 sol P1-4, agy F4): fs_usage has NO PPID column, so a
name-filter on `git` catches EVERY git on the host, and a system-wide trace catches
parallel sessions. v4's answer is: reconstruct the pid tree from a ps snapshot, attribute
rows to kimi's tree, and FAIL CLOSED if unattributed foreign-named rows exceed a
threshold (default 0 under quiesce). These tests encode that with realistic fixture lines
captured from real fs_usage output on this host.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from fsusage import parse_exec_line, parse_filesys_line, attribute, ContaminationError  # noqa: E402

# Real fs_usage -f exec rows captured on this host (proc column is `name.pid`).
EXEC_LINES = [
    "11:17:09.537242  execve                                 /usr/bin/stat                    0.000217   .6890269",
    "11:17:09.611223  execve                 [  2]           /Users/<user>/.grok/bin/git         0.000036   Python.8351744",
    "11:17:09.611252  execve                 [  2]           /Users/<user>/.kimi-code/bin/git    0.000029   Python.8351744",
]

# Real fs_usage -f filesys rows (proc column is `name.pid`).
FILESYS_LINES = [
    "11:19:04.382  open              /Users/<user>/<workspace>/.git/HEAD          0.000024   git.6890353",
    "11:19:04.382  stat64            /opt/homebrew/bin/git                    0.000012   git.6890353",
]


def test_parse_exec_line_extracts_binary_and_pid():
    ev = parse_exec_line(EXEC_LINES[0])
    assert ev.binary == "/usr/bin/stat"
    assert ev.pid == 6890269
    # This row's proc column is `.6890269` — the process clobbered its argv, so the
    # NAME is genuinely blank. That is real fs_usage output, not a parse miss.
    assert ev.proc_name == ""


def test_parse_exec_line_recovers_pid_when_name_is_blank():
    # The stat row's proc column is `.6890269` (name clobbered) — pid must still parse.
    ev = parse_exec_line(EXEC_LINES[0])
    assert ev.pid == 6890269


def test_parse_filesys_line_extracts_path_and_pid():
    ev = parse_filesys_line(FILESYS_LINES[0])
    assert ev.path == "/Users/<user>/<workspace>/.git/HEAD"
    assert ev.pid == 6890353
    assert ev.proc_name == "git"


def test_parse_non_event_line_returns_none():
    assert parse_exec_line("=== not an event ===") is None
    assert parse_filesys_line("") is None


# ---- attribution against a pid tree ----

# ps snapshot: kimi(100) -> sh(200) -> git(300); an unrelated codex(900) tree.
PS_SNAPSHOT = {100: 0, 200: 100, 300: 200, 900: 0, 901: 900}


def test_row_from_kimi_descendant_is_attributed():
    events = [parse_filesys_line(
        "11:19:04  open  /x  0.0  git.300")]
    kept, contamination = attribute(events, ps_tree=PS_SNAPSHOT, kimi_root=100,
                                    discovered_names={"git", "sh", "kimi"})
    assert len(kept) == 1
    assert contamination == 0


def test_foreign_named_row_trips_the_gate():
    # codex (pid 900) is NOT in kimi's tree and its name is not discovered -> foreign.
    events = [parse_filesys_line("11:19:04  open  /y  0.0  codex.900")]
    with pytest.raises(ContaminationError):
        attribute(events, ps_tree=PS_SNAPSHOT, kimi_root=100,
                  discovered_names={"git", "sh", "kimi"}, contam_max=0)


def test_known_name_lost_lineage_is_attribution_lag_not_hard_fail():
    # A `git` row whose pid (777) is not in the ps snapshot (short-lived, raced the
    # sample) is a KNOWN name -> counts as attribution-lag, not a foreign-contamination
    # hard fail. Must NOT raise; must be counted separately.
    events = [parse_filesys_line("11:19:04  open  /z  0.0  git.777")]
    kept, lag = attribute(events, ps_tree=PS_SNAPSHOT, kimi_root=100,
                          discovered_names={"git", "sh", "kimi"}, contam_max=0,
                          return_lag=True)
    assert lag == 1  # recorded, not fatal


def test_gate_passes_when_foreign_rows_within_threshold():
    events = [parse_filesys_line("11:19:04  open  /y  0.0  codex.900")]
    kept, contamination = attribute(events, ps_tree=PS_SNAPSHOT, kimi_root=100,
                                    discovered_names={"git"}, contam_max=1)
    assert contamination == 1  # under threshold, no raise
