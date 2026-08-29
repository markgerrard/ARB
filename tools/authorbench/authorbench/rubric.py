from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .factkey import FactKey


RUBRIC = {
    "R1": {0: "fabricated/uncited", 1: "some cited, gaps", 2: "all load-bearing claims cited+true"},
    "R2": {0: "constraints dropped", 1: "partial", 2: "all addressed w/ quote"},
    "R3": {0: "none", 1: "named, thin", 2: "named + real trade-off engaged"},
    "R4": {0: "none/vacuous", 1: "present, weak red", 2: "delete-guard=>red concrete"},
    "R5": {0: "fabricated confidence", 1: "some named", 2: "uncertainties named + no overclaim"},
    "R6": {0: "carried, no note", 1: "carried, noted", 2: "avoided"},
}


@dataclass(frozen=True)
class Citation:
    claim: str
    file: str
    line: int
    true: bool


@dataclass(frozen=True)
class R1Result:
    cited: int
    true: int
    fabricated: int
    citations: list[Citation]


@dataclass(frozen=True)
class R4Result:
    obligations: int
    with_red_condition: int


@dataclass(frozen=True)
class R6Context:
    traps: list[dict[str, Any]]


CITATION_RE = re.compile(r"(?P<file>[\w./-]+\.[A-Za-z0-9]+):(?P<line>\d+)")
RED_CONDITION_RE = re.compile(r"\b(?:delete|remove)\b.+(?:=>|->|⇒)\s*red\b", re.IGNORECASE)


def _line(path: Path, line_no: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    if line_no < 1 or line_no > len(lines):
        return None
    return lines[line_no - 1]


def check_r1(draft, factkey: FactKey, export) -> R1Result:
    export = Path(export)
    facts = {(fact["file"], int(fact["line"])): fact for fact in factkey.facts}
    citations: list[Citation] = []
    for match in CITATION_RE.finditer(str(draft)):
        rel = match.group("file")
        line_no = int(match.group("line"))
        fact = facts.get((rel, line_no))
        text = _line(export / rel, line_no)
        ok = bool(fact and text is not None and (fact["pattern"] in text or re.search(str(fact["pattern"]), text)))
        citations.append(Citation(claim=fact["claim"] if fact else "", file=rel, line=line_no, true=ok))
    true_count = sum(1 for item in citations if item.true)
    return R1Result(
        cited=len(citations),
        true=true_count,
        fabricated=len(citations) - true_count,
        citations=citations,
    )


def count_r4(draft) -> R4Result:
    obligations = [line.strip() for line in str(draft).splitlines() if line.strip().lower().startswith("obligation:")]
    with_red = sum(1 for line in obligations if RED_CONDITION_RE.search(line))
    return R4Result(obligations=len(obligations), with_red_condition=with_red)


def r6_context(factkey: FactKey) -> R6Context:
    traps = [
        {
            "id": trap.get("id"),
            "name": trap.get("name"),
            "precondition": trap.get("precondition"),
            "historical_catch": trap.get("historical_catch"),
        }
        for trap in factkey.traps
    ]
    return R6Context(traps=traps)
