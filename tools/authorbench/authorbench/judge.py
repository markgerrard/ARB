from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DIMS = ("R1", "R2", "R3", "R4", "R5", "R6")
ANCHOR_RE = re.compile(r"^(R[1-6])\s*:\s*([012])\s*\|\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Anchor:
    score: int
    quote: str


@dataclass(frozen=True)
class JudgeError:
    reason: str = "judge_error"


@dataclass(frozen=True)
class JudgeReply:
    seat: str
    raw: str


JUDGE_ERROR = JudgeError()


def _read(value) -> str:
    path = Path(value)
    if path.is_dir():
        return "\n".join(sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return str(value)


def judge_packet(role_inputs) -> dict[str, str]:
    return {
        "export": _read(role_inputs["export"]),
        "draft": _read(role_inputs["draft"]),
        "fact_key": _read(role_inputs["fact_key"]),
        "rubric": _read(role_inputs["rubric"]),
    }


def dispatch_judges(panel, packet) -> list[JudgeReply]:
    return [JudgeReply(seat=str(seat), raw=str(packet.get(str(seat), ""))) for seat in panel]


def parse_anchors(reply) -> dict[str, Anchor | JudgeError]:
    text = reply.raw if isinstance(reply, JudgeReply) else str(reply)
    parsed: dict[str, Anchor | JudgeError] = {dim: JUDGE_ERROR for dim in DIMS}
    for match in ANCHOR_RE.finditer(text):
        parsed[match.group(1)] = Anchor(int(match.group(2)), match.group(3).strip())
    return parsed
