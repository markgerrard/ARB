"""Pure data contract shared by defect hunts and their deterministic gates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


HuntKind = Literal["H1", "H2"]
Decision = Literal["FLAG", "CLEAR"]


@dataclass(frozen=True)
class Finding:
    subject: str
    kind: HuntKind
    decision: Decision
    evidence: str

    def __post_init__(self) -> None:
        _require_non_empty_string("subject", self.subject)
        _require_non_empty_string("kind", self.kind)
        _require_non_empty_string("decision", self.decision)
        _require_non_empty_string("evidence", self.evidence)
        if self.kind not in {"H1", "H2"}:
            raise ValueError("kind must be exactly 'H1' or 'H2'")
        if self.decision not in {"FLAG", "CLEAR"}:
            raise ValueError("decision must be exactly 'FLAG' or 'CLEAR'")


@dataclass(frozen=True)
class Verdict:
    findings: list[Finding]

    def __post_init__(self) -> None:
        if not isinstance(self.findings, list):
            raise TypeError("findings must be a list of Finding objects")
        for finding in self.findings:
            if not isinstance(finding, Finding):
                raise TypeError("findings must contain only Finding objects")


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    files: dict[str, str]
    diff: str

    def __post_init__(self) -> None:
        _require_non_empty_string("scenario_id", self.scenario_id)
        _require_non_empty_string("diff", self.diff)
        if not isinstance(self.files, dict):
            raise TypeError("files must be a dict[str, str]")
        for path, content in self.files.items():
            _require_non_empty_string("files key", path)
            if not isinstance(content, str):
                raise TypeError("files values must be strings")


Hunt = Callable[[Scenario], Verdict]


def _require_non_empty_string(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} is required")


__all__ = [
    "Decision",
    "Finding",
    "Hunt",
    "HuntKind",
    "Scenario",
    "Verdict",
]
