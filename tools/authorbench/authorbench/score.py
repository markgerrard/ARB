from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .judge import Anchor, JudgeError


@dataclass(frozen=True)
class Verdict:
    label: str
    value: int | None
    rerunnable: bool = False

    def __str__(self) -> str:
        return self.label


NOT_MET = Verdict("NOT-MET", 0)
PARTIAL = Verdict("PARTIAL", 1)
MET = Verdict("MET", 2)
UNKNOWN_JUDGE_ERROR = Verdict("UNKNOWN(judge_error)", None, rerunnable=True)
_BY_VALUE = {0: NOT_MET, 1: PARTIAL, 2: MET}


def _cap(verdict: Verdict, cap: Verdict) -> Verdict:
    if verdict.value is None or cap.value is None:
        return verdict
    return _BY_VALUE[min(verdict.value, cap.value)]


def cell(dim, judge_anchors, mech) -> Verdict:
    anchors = [item for item in judge_anchors if isinstance(item, Anchor)]
    if not anchors:
        return UNKNOWN_JUDGE_ERROR
    value = int(median([anchor.score for anchor in anchors]))
    verdict = _BY_VALUE[value]
    if dim == "R1" and mech and mech.get("r1_cap"):
        verdict = _cap(verdict, mech["r1_cap"])
    return verdict
