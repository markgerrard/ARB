"""The exec-anchored DELIVERED oracle — probe design v4 §6.

A review turn is DELIVERED iff it terminated cleanly, mutated nothing, produced a
substantive citation-bearing review, AND instrument A observed a real `git` execve in
the same turn. The exec anchor is the load-bearing part: it is the only clause the
reply text cannot forge, because a `.git/logs/HEAD` read (the r3 bypass) leaves no
execve. "Quoted a SHA" is necessary but NOT sufficient.

This proves git RAN — not that kimi USED its output (recorded residual, v4 §9).
"""
import os
import re
from dataclasses import dataclass, field

_CITATION = re.compile(r"[\w/]+\.\w+:\d+")
MIN_TEXT = 200


@dataclass
class Turn:
    stop_reason: str | None
    text: str
    mutated: bool
    exec_trace: list[str] = field(default_factory=list)


def _git_was_executed(exec_trace: list[str]) -> bool:
    """True iff any exec-trace entry is a binary whose basename is exactly `git`."""
    return any(os.path.basename(p) == "git" for p in exec_trace)


def delivered(turn: Turn) -> bool:
    if turn.stop_reason != "end_turn":
        return False
    if turn.mutated:
        return False
    if len(turn.text) <= MIN_TEXT:
        return False
    if not _CITATION.search(turn.text):
        return False
    if not _git_was_executed(turn.exec_trace):
        return False
    return True
