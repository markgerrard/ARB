"""Quarantined Git export and complete ref/object census primitives."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from .records import census_evidence_digest as _record_census_digest


class QuarantineError(RuntimeError):
    """Raised when an export or clone cannot be proven to be the expected graph."""


class CensusViolation(str, Enum):
    EXTRA_REF = "EXTRA_REF"
    MISSING_REF = "MISSING_REF"
    EXTRA_OBJECT = "EXTRA_OBJECT"
    MISSING_OBJECT = "MISSING_OBJECT"
    OBJECT_SET_MISMATCH = "OBJECT_SET_MISMATCH"
    INVALID_OBJECT_TYPE = "INVALID_OBJECT_TYPE"


@dataclass(frozen=True)
class Census:
    expected_refs: Mapping[str, str]
    refs: Mapping[str, str]
    expected_objects: frozenset[str]
    objects: frozenset[str]
    expected_ref_digest: str
    observed_ref_digest: str
    expected_object_digest: str
    observed_object_digest: str
    violation: CensusViolation | None = None
    census_evidence_digest: str | None = None

    @property
    def ref_count(self) -> int:
        return len(self.refs)

    @property
    def object_count(self) -> int:
        return len(self.objects)


@dataclass(frozen=True)
class QuarantineExport:
    path: Path
    refs: Mapping[str, str]
    census: Census


EXPORT_REFS = {
    "refs/arb-export/base",
    "refs/arb-export/fixture",
}
_OID = re.compile(r"^[0-9a-f]{40}$")


def _run(repo: Path, *args: str, check: bool = True) -> str:
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
        check=False, env={**os.environ, **env},
    )
    if check and result.returncode:
        raise QuarantineError(result.stderr.strip() or result.stdout.strip() or "git operation failed")
    return result.stdout.strip()


def _absolute_repo(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_absolute() or value.is_symlink():
        raise QuarantineError("repository path must be an absolute non-symlink path")
    try:
        value = value.resolve(strict=True)
    except OSError as exc:
        raise QuarantineError("repository path is unavailable") from exc
    if not value.is_dir():
        raise QuarantineError("repository path is not a directory")
    return value


def _ref_backend(repo: Path) -> str:
    value = _run(repo, "rev-parse", "--show-ref-format", check=False)
    if value in {"files", "reftable"}:
        return value
    configured = _run(repo, "config", "--get", "extensions.refStorage", check=False)
    return configured or "files"


def _require_files_backend(repo: Path) -> None:
    if _ref_backend(repo) != "files":
        raise QuarantineError("reftable ref backend is forbidden")


def _validate_oid(value: str, where: str) -> None:
    if not _OID.fullmatch(value):
        raise QuarantineError(f"{where} is not a Git object ID")


def _refs(repo: Path) -> dict[str, str]:
    output = _run(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    result: dict[str, str] = {}
    for line in output.splitlines():
        ref, oid = line.split(" ", 1)
        result[ref] = oid
    return result


def _all_objects(repo: Path) -> frozenset[str]:
    output = _run(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    objects: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not _OID.fullmatch(parts[0]) or parts[1] not in {"commit", "tree", "blob", "tag"}:
            raise QuarantineError("invalid object census entry")
        objects.add(parts[0])
    return frozenset(objects)


def _reachable_objects(repo: Path, tips: Iterable[str]) -> frozenset[str]:
    tips = tuple(tips)
    if not tips:
        return frozenset()
    output = _run(repo, "rev-list", "--objects", *tips)
    return frozenset(line.split()[0] for line in output.splitlines() if line)


def _digest(values: Iterable[str]) -> str:
    return hashlib.sha256("".join(f"{value}\n" for value in sorted(values)).encode("ascii")).hexdigest()


def _ref_digest(refs: Mapping[str, str]) -> str:
    return hashlib.sha256("".join(f"{ref}\0{refs[ref]}\n" for ref in sorted(refs)).encode("ascii")).hexdigest()


def _violation(expected_refs: Mapping[str, str], refs: Mapping[str, str], expected_objects: frozenset[str], objects: frozenset[str]) -> CensusViolation | None:
    if set(refs) - set(expected_refs):
        return CensusViolation.EXTRA_REF
    if set(expected_refs) - set(refs):
        return CensusViolation.MISSING_REF
    if any(refs[ref] != expected_refs[ref] for ref in expected_refs):
        return CensusViolation.OBJECT_SET_MISMATCH
    if objects - expected_objects:
        return CensusViolation.EXTRA_OBJECT
    if expected_objects - objects:
        return CensusViolation.MISSING_OBJECT
    return None


def census_repository(repo: str | Path, *, expected_refs: Mapping[str, str], phase: str = "export", gate_id: str = "G12") -> Census:
    """Compare every ref and every ODB object with the exact expected closure."""

    path = _absolute_repo(repo)
    _require_files_backend(path)
    for oid in expected_refs.values():
        _validate_oid(oid, "expected ref")
    observed_refs = _refs(path)
    expected_objects = _reachable_objects(path, expected_refs.values())
    observed_objects = _all_objects(path)
    violation = _violation(expected_refs, observed_refs, expected_objects, observed_objects)
    evidence = None
    if violation is not None:
        payload = {
            "phase": phase,
            "gate_id": gate_id,
            "expected_ref_digest": _ref_digest(expected_refs),
            "observed_ref_digest": _ref_digest(observed_refs),
            "expected_object_digest": _digest(expected_objects),
            "observed_object_digest": _digest(observed_objects),
            "expected_ref_count": len(expected_refs),
            "observed_ref_count": len(observed_refs),
            "expected_object_count": len(expected_objects),
            "observed_object_count": len(observed_objects),
            "violation": violation.value,
        }
        evidence = _record_census_digest(payload)
    return Census(
        expected_refs=dict(expected_refs), refs=observed_refs,
        expected_objects=expected_objects, objects=observed_objects,
        expected_ref_digest=_ref_digest(expected_refs), observed_ref_digest=_ref_digest(observed_refs),
        expected_object_digest=_digest(expected_objects), observed_object_digest=_digest(observed_objects),
        violation=violation, census_evidence_digest=evidence,
    )


def census_evidence_digest(payload: Mapping[str, object]) -> str:
    """Validate and hash the private canonical census payload."""

    return _record_census_digest(payload)


def export_quarantine(source_repo: str | Path, destination: str | Path, *, base_oid: str, fixture_oid: str) -> QuarantineExport:
    """Create a two-tip, files-backend export only after a complete source census."""

    source = _absolute_repo(source_repo)
    for oid, name in ((base_oid, "base_oid"), (fixture_oid, "fixture_oid")):
        _validate_oid(oid, name)
    expected = {
        "refs/arb-export/base": base_oid,
        "refs/arb-export/fixture": fixture_oid,
    }
    source_refs = _refs(source)
    if any(ref.startswith("refs/implbench/") for ref in source_refs):
        raise QuarantineError("export census failed: EXTRA_REF")
    expected_objects = _reachable_objects(source, (base_oid, fixture_oid))
    observed_objects = _all_objects(source)
    if observed_objects != expected_objects:
        raise QuarantineError("export census failed: EXTRA_OBJECT")
    target = Path(destination)
    if target.exists():
        raise QuarantineError("export destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.mkdir()
        _run(target, "init", "--bare", "-q", "--ref-format=files")
        _run(target, "fetch", "--no-tags", str(source), f"{base_oid}:refs/arb-export/base", f"{fixture_oid}:refs/arb-export/fixture")
        # Fetch can create a symref or remote-tracking ref depending on Git version; no
        # unlisted reference may survive the quarantine boundary.
        for ref in _refs(target):
            if ref not in expected:
                _run(target, "update-ref", "-d", ref)
        result = census_repository(target, expected_refs=expected, phase="export")
        if result.violation is not None:
            raise QuarantineError(f"quarantine export census failed: {result.violation.value}")
        return QuarantineExport(target, expected, result)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def clone_quarantine(export_repo: str | Path, destination: str | Path) -> Census:
    """Clone an export with no local hardlinks/tags and an exact two-ref fetch."""

    source = _absolute_repo(export_repo)
    _require_files_backend(source)
    expected = _refs(source)
    if set(expected) != EXPORT_REFS:
        raise QuarantineError("export does not contain the exact quarantine refs")
    target = Path(destination)
    if target.exists():
        raise QuarantineError("cell destination already exists")
    try:
        subprocess.run(
            ["git", "clone", "--no-local", "--no-tags", "--no-checkout", "--ref-format=files", str(source), str(target)],
            text=True, capture_output=True, check=True,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        # Make the transfer contract explicit and refresh only the two allowlisted refs.
        _run(target, "config", "remote.origin.fetch", "+refs/arb-export/base:refs/arb-export/base")
        _run(target, "config", "--add", "remote.origin.fetch", "+refs/arb-export/fixture:refs/arb-export/fixture")
        _run(target, "fetch", "--no-tags", "origin", "+refs/arb-export/base:refs/arb-export/base", "+refs/arb-export/fixture:refs/arb-export/fixture")
        for ref in _refs(target):
            if ref not in expected:
                _run(target, "update-ref", "-d", ref)
        result = census_repository(target, expected_refs=expected, phase="cell")
        if result.violation is not None:
            raise QuarantineError(f"cell census failed: {result.violation.value}")
        return result
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


# Explicit aliases make the boundary names discoverable to the controller without offering a
# task-only or mutable-source shortcut.
export_cell = export_quarantine
clone_cell = clone_quarantine
complete_census = census_repository
