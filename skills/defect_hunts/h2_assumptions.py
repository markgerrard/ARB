"""H2 environmental-assumption schema gate.

H2 is a deterministic forcing-function over the review brief's H2 section, not
a detector. Each row must dispose a candidate by ``candidate_id`` with one of
the supported dispositions: ``answered``, ``not_load_bearing``, or ``flag``.
Every disposition must carry evidence anchored to a spot-checkable artifact.

Named residual: vacuity-laundering through a real-but-irrelevant artifact. This
gate catches omitted fields, empty fields, evidence with no concrete anchor, and
anchors that do not resolve under ``repo_root``. It cannot prove that an
existing anchor is relevant to the stated assumption; the secondary code panel
owns that residual review.
"""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


H2Entry = Mapping[str, object]
H2Section = Mapping[str, object]

_NODE_ID_RE = re.compile(r"(?P<path>[\w./-]+\.py)::(?P<node>[\w.\[\]-]+)")
_PATH_RE = re.compile(r"(?P<path>(?:[\w.-]+/)+[\w.-]+|\w[\w.-]*\.(?:py|log|txt|json))")


@dataclass
class H2Record:
    run_id: str
    h2_mode: str
    derived: list[str]
    dispositions: list[dict]
    coverage_acknowledged: bool
    complete: bool


def validate_h2_section(section: H2Section, *, repo_root: str | Path) -> tuple[bool, str]:
    root = Path(repo_root).resolve()
    if not root.exists():
        return (False, "repo_root does not exist")

    if not isinstance(section, Mapping):
        return (False, "H2 section must be an object")

    coverage_acknowledgment = section.get("coverage_acknowledgment")
    if not isinstance(coverage_acknowledgment, Mapping):
        return (False, "H2 section missing coverage_acknowledgment")
    if not isinstance(coverage_acknowledgment.get("acknowledged"), bool):
        return (False, "coverage_acknowledgment.acknowledged must be bool")

    rows = section.get("rows")
    if not _is_row_list(rows):
        return (False, "H2 section rows must be a list")

    additional_assumptions = coverage_acknowledgment.get("additional_assumptions")
    if not _is_row_list(additional_assumptions):
        return (False, "coverage_acknowledgment.additional_assumptions must be a list")

    for index, row in enumerate(rows):
        ok, reason = _validate_disposition_row(row, index=index, repo_root=root)
        if not ok:
            return (ok, reason)

    for index, row in enumerate(additional_assumptions):
        ok, reason = _validate_disposition_row(row, index=index, repo_root=root)
        if not ok:
            return (ok, reason)

    return (True, "ok")


def is_complete(derived: list[str], section: dict, *, repo_root: str | Path) -> bool:
    coverage_acknowledgment = section.get("coverage_acknowledgment") if isinstance(section, Mapping) else None
    rows = section.get("rows") if isinstance(section, Mapping) else None
    return (
        isinstance(coverage_acknowledgment, Mapping)
        and coverage_acknowledgment.get("acknowledged") is True
        and len(derived) >= 1
        and validate_h2_section(section, repo_root=repo_root)[0]
        and _is_row_list(rows)
        and all(isinstance(row, Mapping) and row.get("candidate_id") in derived for row in rows)
        # One candidate, one disposition: a duplicate disposition row (two rows for
        # the same derived candidate) is malformed. Left unrejected it double-counts
        # in the graduation denominator (_disposition_counts counts rows), lowering
        # the FP rate and making graduation *easier* — a false-pass skew. The honest
        # case (a reviewer disposing the same candidate twice) is mistake-class and
        # in threat model; the *deliberate* duplicate-to-game-the-metric case is the
        # §9 adversarial-disposition non-goal. This clause closes the honest path.
        and len({row.get("candidate_id") for row in rows}) == len(rows)
        and all(
            any(isinstance(row, Mapping) and row.get("candidate_id") == candidate_id for row in rows)
            for candidate_id in derived
        )
    )


def _is_row_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str)


def _validate_disposition_row(row: object, *, index: int, repo_root: Path) -> tuple[bool, str]:
    if not isinstance(row, Mapping):
        return (False, f"row {index} must be a dict")

    if not _non_empty_string(row.get("candidate_id")):
        return (False, f"row {index} missing or empty candidate_id")

    disposition = row.get("disposition")
    if disposition == "answered":
        required_fields = ("violating_run", "evidence")
    elif disposition == "not_load_bearing":
        required_fields = ("reason", "evidence")
    elif disposition == "flag":
        required_fields = ("assumption", "evidence")
    else:
        return (False, f"row {index} disposition must be answered, not_load_bearing, or flag")

    for field in required_fields:
        if not _non_empty_string(row.get(field)):
            return (False, f"row {index} missing or empty {field}")

    evidence = row["evidence"]
    if not isinstance(evidence, str):
        return (False, f"row {index} missing or empty evidence")
    return _validate_evidence_anchor(evidence, index=index, repo_root=repo_root)


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_evidence_anchor(
    evidence: str,
    *,
    index: int,
    repo_root: Path,
) -> tuple[bool, str]:
    node_match = _NODE_ID_RE.search(evidence)
    if node_match is not None:
        candidate = _resolve_under_repo(node_match.group("path"), repo_root)
        if candidate is None:
            return (False, f"row {index} evidence anchor escapes repo_root")
        if not candidate.exists():
            return (False, f"row {index} evidence anchor does not exist: {node_match.group('path')}")
        return (True, "ok")

    command = _command_from_evidence(evidence)
    if command is not None:
        return _validate_command_anchor(command, index=index, repo_root=repo_root)

    for match in _PATH_RE.finditer(evidence):
        anchor = match.group("path")
        candidate = _resolve_under_repo(anchor, repo_root)
        if candidate is None:
            return (False, f"row {index} evidence anchor escapes repo_root")
        if candidate.exists():
            return (True, "ok")
        return (False, f"row {index} evidence anchor does not exist: {anchor}")

    return (False, f"row {index} evidence anchor is required")


def _command_from_evidence(evidence: str) -> str | None:
    stripped = evidence.strip()
    for prefix in ("$", "command:", "cmd:"):
        if stripped.startswith(prefix):
            command = stripped.removeprefix(prefix).strip()
            return command or None
    return None


def _validate_command_anchor(command: str, *, index: int, repo_root: Path) -> tuple[bool, str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return (False, f"row {index} evidence command is not parseable")
    if not parts:
        return (False, f"row {index} evidence command is empty")

    executable = parts[0]
    if "/" in executable:
        candidate = _resolve_under_repo(executable, repo_root)
        if candidate is None:
            return (False, f"row {index} evidence command escapes repo_root")
        if candidate.exists():
            return (True, "ok")
        return (False, f"row {index} evidence command does not exist: {executable}")

    if shutil.which(executable) is not None:
        return (True, "ok")
    return (False, f"row {index} evidence command is not on PATH: {executable}")


def _resolve_under_repo(anchor: str, repo_root: Path) -> Path | None:
    path = Path(anchor)
    candidate = path if path.is_absolute() else repo_root / path
    resolved = candidate.resolve(strict=False)
    if resolved == repo_root or repo_root in resolved.parents:
        return resolved
    return None


__all__ = ["H2Entry", "H2Record", "H2Section", "is_complete", "validate_h2_section"]
