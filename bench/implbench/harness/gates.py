"""Non-scored parsing/data types retained for the ordinary CLI surface.

Scored G0-G7 decisions live in :mod:`classifier` and :mod:`controller`; this module intentionally
has no ordinary ``evaluate_gate`` compatibility adapter and never reads host Git state.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DispatchResult:
    status: str
    timed_out: bool = False
    structured: dict[str, Any] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True)
class BatteryResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    missing_artifacts: tuple[str, ...] = ()
    error_kind: str | None = None


@dataclass(frozen=True)
class GateCtx:
    """Ordinary caller data shape; it is not accepted by scored classification."""

    task: Any
    repo: Path
    worktree: Path
    fixture_sha: str
    head_after: str | None
    dispatch: DispatchResult
    battery: BatteryResult | None
    prior: dict[str, "GateResult"] = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    gate: str
    verdict: str
    evidence: dict[str, Any]
    reason: str | None = None
    error: str | None = None
    flags: tuple[str, ...] = ()


def parse_junit(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.attrib.get("tests", 0))
        failures += int(suite.attrib.get("failures", 0))
        errors += int(suite.attrib.get("errors", 0))
        skipped += int(suite.attrib.get("skipped", 0))
    return {"tests": tests, "failed": failures + errors, "errors": errors, "skipped": skipped, "passed": tests - failures - errors - skipped}
