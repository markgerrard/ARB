"""Tests for the exec-anchored DELIVERED oracle (probe design v4 §6).

Ground truth = the 9 committed rows at da4c2d2 (review_results.jsonl) plus synthetic
exec traces for the exec-anchor clause the committed rows cannot exercise (fs_usage
was not running when they were captured).

The oracle's whole reason to exist: three panels caught the crude predicate
(end_turn AND len>200) false-greening a REFUSAL (R2-shell-requiring#1), and caught the
v3 proof-of-git being satisfiable by a plaintext .git/logs/HEAD read. v4 anchors it to
an observed git execve. These tests encode both.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from oracle import delivered, Turn  # noqa: E402

SHA = "da4c2d2"
SPIKE = "docs/superpowers/probes/2026-07-17-kimi-spike/review_results.jsonl"


def _committed_rows():
    out = subprocess.run(["git", "show", f"{SHA}:{SPIKE}"],
                         capture_output=True, text=True, cwd=_repo())
    return [json.loads(l) for l in out.stdout.splitlines() if l.strip()]


def _repo():
    return subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True,
                          cwd=HERE).stdout.strip()


def _turn(row, exec_trace=None):
    return Turn(stop_reason=row.get("stopReason"),
                text=row.get("text") or "",
                mutated=bool(row.get("mutated")),
                exec_trace=exec_trace or [])


# ---- reply-side gates against real committed rows ----

def test_genuine_r1_reviews_are_delivered_when_git_exec_observed():
    # R1 rows are inline read-only (no git needed) — but to isolate the reply-side
    # gates we grant a satisfying exec trace and require them DELIVERED.
    rows = [r for r in _committed_rows() if r["cell"].startswith("R1-inline")]
    assert len(rows) == 5
    for r in rows:
        assert delivered(_turn(r, exec_trace=["/opt/homebrew/bin/git"])) is True, r["cell"]


def test_the_committed_false_green_refusal_is_not_delivered():
    # R2-shell-requiring#1 (660s batch): end_turn, len>200, but 0 citations + refusal.
    # The crude predicate scored this delivered=True. The oracle MUST reject it,
    # even with a git exec present — the citation gate alone kills it.
    rows = [r for r in _committed_rows()
            if r["cell"] == "R2-shell-requiring#1" and r["text_len"] < 2000]
    assert rows, "expected the 660s-batch R2#1 refusal row"
    row = rows[0]
    assert delivered(_turn(row, exec_trace=["/opt/homebrew/bin/git"])) is False


def test_non_terminating_turn_is_not_delivered():
    rows = [r for r in _committed_rows() if r.get("stopReason") is None]
    assert rows, "expected at least one never-terminated row"
    assert delivered(_turn(rows[0], exec_trace=["/opt/homebrew/bin/git"])) is False


# ---- the exec-anchor clause (v4's fix), synthetic ----

GENUINE_REVIEW = ("Reviewed commit abc1234 (\"fix: the thing\"). "
                  "P1 in engines/_acp.py:42 — the fallback selects a deny option. " * 3)


def test_review_without_git_exec_is_not_delivered_even_if_it_quotes_a_sha():
    # THE .git/logs/HEAD BYPASS (r3, all three seats). Perfect review text, quotes a
    # SHA, has citations, terminated cleanly — but NO git execve in the trace, because
    # kimi read .git/logs/HEAD instead. MUST be rejected.
    t = Turn(stop_reason="end_turn", text=GENUINE_REVIEW, mutated=False, exec_trace=[])
    assert delivered(t) is False


def test_review_with_git_exec_is_delivered():
    t = Turn(stop_reason="end_turn", text=GENUINE_REVIEW, mutated=False,
             exec_trace=["/usr/bin/sw_vers", "/opt/homebrew/bin/git", "/bin/ls"])
    assert delivered(t) is True


def test_exec_anchor_matches_any_git_path_not_just_one():
    for gitpath in ("/usr/bin/git", "/Volumes/x/.venv/bin/git",
                    "/Applications/Xcode.app/Contents/Developer/usr/bin/git"):
        t = Turn(stop_reason="end_turn", text=GENUINE_REVIEW, mutated=False,
                 exec_trace=[gitpath])
        assert delivered(t) is True, gitpath


def test_exec_anchor_does_not_match_a_binary_merely_named_like_git():
    # A path whose basename is not exactly `git` must NOT satisfy the anchor
    # (e.g. `git-lfs`, `legit`, `digit`) — else the anchor is spoofable.
    for notgit in ("/usr/bin/git-lfs", "/usr/bin/legit", "/opt/bin/digit"):
        t = Turn(stop_reason="end_turn", text=GENUINE_REVIEW, mutated=False,
                 exec_trace=[notgit])
        assert delivered(t) is False, notgit


def test_mutation_is_never_delivered():
    t = Turn(stop_reason="end_turn", text=GENUINE_REVIEW, mutated=True,
             exec_trace=["/opt/homebrew/bin/git"])
    assert delivered(t) is False
