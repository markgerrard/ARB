"""Bakeoff ref identity and fail-closed evidence-root protection."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Iterable


class RefProtectionError(RuntimeError):
    """Raised before mutation when ref protection cannot be proven."""


_RUN_ID = re.compile(r"^oi-pi-bakeoff-[A-Za-z0-9][A-Za-z0-9-]*$")
_CELL = re.compile(r"^cell-[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^attempt-[0-9a-f]{32,64}$")
_OID = re.compile(r"^[0-9a-f]{40}$")
_DATE = re.compile(r"(\d{8})T\d{6}Z")


def parse_bakeoff_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise RefProtectionError("invalid bakeoff run ID")
    return run_id


def parse_run_id(ref: str) -> str | None:
    """Total parser for the canonical run/result ref layout."""

    if not isinstance(ref, str):
        return None
    parts = ref.split("/")
    if len(parts) < 6 or parts[:3] not in (["refs", "implbench", "runs"], ["refs", "implbench", "results"]):
        return None
    run_id = parts[3]
    try:
        return parse_bakeoff_run_id(run_id)
    except RefProtectionError:
        return None


def bakeoff_ref(kind: str, run_id: str, cell_id: str, attempt_id: str) -> str:
    if kind not in {"runs", "results"}:
        raise RefProtectionError("ref kind must be runs or results")
    parse_bakeoff_run_id(run_id)
    if not isinstance(cell_id, str) or not _CELL.fullmatch(cell_id):
        raise RefProtectionError("invalid cell identity")
    if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
        raise RefProtectionError("invalid attempt identity")
    return f"refs/implbench/{kind}/{run_id}/{cell_id}/{attempt_id}"


def bakeoff_run_ref(run_id: str, cell_id: str, attempt_id: str) -> str:
    return bakeoff_ref("runs", run_id, cell_id, attempt_id)


def bakeoff_result_ref(run_id: str, cell_id: str, attempt_id: str) -> str:
    return bakeoff_ref("results", run_id, cell_id, attempt_id)


def write_bakeoff_ref(repo: str | Path, kind: str, run_id: str, cell_id: str, attempt_id: str, oid: str) -> str:
    """Write a controller-owned cell/attempt ref after validating its complete identity."""

    if not _OID.fullmatch(oid):
        raise RefProtectionError("invalid ref object ID")
    ref = bakeoff_ref(kind, run_id, cell_id, attempt_id)
    _git(Path(repo), "update-ref", ref, oid)
    return ref


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RefProtectionError(result.stderr.strip() or result.stdout.strip() or "git ref operation failed")
    return result.stdout.strip()


def _absolute_evidence_root(value: str | Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise RefProtectionError("evidence root must be an existing absolute directory")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise RefProtectionError("evidence root cannot be resolved") from exc


def _parse_final_index(text: str) -> tuple[set[str], str | None]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefProtectionError("invalid final ref index") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "manifest_digest", "journal_tail_digest", "refs"}:
        raise RefProtectionError("invalid final ref index")
    if value["schema_version"] != "git-refs-v1" or not isinstance(value["manifest_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["manifest_digest"]) or not isinstance(value["journal_tail_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["journal_tail_digest"]):
        raise RefProtectionError("invalid final ref index")
    expected = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    if text != expected or not isinstance(value["refs"], list):
        raise RefProtectionError("invalid final ref index")
    for row in value["refs"]:
        if not isinstance(row, dict) or set(row) != {"ref", "oid"} or not isinstance(row["ref"], str) or not row["ref"].startswith("refs/") or not isinstance(row["oid"], str) or not _OID.fullmatch(row["oid"]):
            raise RefProtectionError("invalid final ref index")
    return {row["ref"] for row in value["refs"]}, value["manifest_digest"]


def _refs_from_text(text: str) -> set[str]:
    return _parse_final_index(text)[0]


def _walk_files(root: Path) -> Iterable[Path]:
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise RefProtectionError("evidence root contains an unsafe entry")
            yield path


def _evidence_snapshot(root: Path) -> tuple[tuple[str, int, int, bytes], ...]:
    snapshot: list[tuple[str, int, int, bytes]] = []
    for path in _walk_files(root):
        try:
            stat_result = path.stat()
            snapshot.append((str(path.relative_to(root)), stat_result.st_ino, stat_result.st_size, path.read_bytes()))
        except OSError as exc:
            raise RefProtectionError("evidence root changed while being scanned") from exc
    return tuple(sorted(snapshot))


def _collect_manifest_refs(value: Any, protected: set[str], identities: list[tuple[str, str, str]]) -> None:
    if isinstance(value, dict):
        run_id = value.get("run_id")
        cell_id = value.get("cell_id")
        attempt_id = value.get("attempt_id")
        if isinstance(run_id, str) and isinstance(cell_id, str) and isinstance(attempt_id, str):
            try:
                protected.add(bakeoff_run_ref(run_id, cell_id, attempt_id))
                identities.append((run_id, cell_id, attempt_id))
            except RefProtectionError:
                pass
        for item in value.values():
            _collect_manifest_refs(item, protected, identities)
    elif isinstance(value, list):
        for item in value:
            _collect_manifest_refs(item, protected, identities)
    elif isinstance(value, str) and value.startswith("refs/"):
        protected.add(value)


def protected_refs(evidence_root: str | Path) -> set[str]:
    """Return the union of validated final-index and active-package refs."""

    root = _absolute_evidence_root(evidence_root)
    before_snapshot = _evidence_snapshot(root)
    protected: set[str] = set()
    manifests: list[Path] = []
    for path in _walk_files(root):
        if path.name == "git-refs.txt":
            refs, index_manifest_digest = _parse_final_index(path.read_text(encoding="utf-8"))
            protected.update(refs)
            if index_manifest_digest is not None:
                manifests_in_root = [candidate for candidate in _walk_files(root) if candidate.name == "manifest.json"]
                if not manifests_in_root or any(hashlib.sha256(candidate.read_bytes()).hexdigest() != index_manifest_digest for candidate in manifests_in_root):
                    raise RefProtectionError("final ref index is not bound to its manifest")
        if path.name == "manifest.json":
            manifests.append(path)
    if not manifests:
        # An evidence root may be a closed package represented by only its final index; that
        # remains valid. Missing roots are fatal, but a valid empty root has no active refs.
        if _evidence_snapshot(root) != before_snapshot:
            raise RefProtectionError("evidence root changed while being scanned")
        return protected
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RefProtectionError("invalid evidence manifest") from exc
        if not isinstance(data, dict) or data.get("schema_version") != "manifest-v2":
            raise RefProtectionError("unknown or invalid manifest schema")
        _collect_manifest_refs(data, protected, [])
    # Active journals are authoritative only for their exact bakeoff identity/ref strings;
    # malformed JSON is not silently treated as a valid protection record.
    for path in _walk_files(root):
        if path.suffix != ".ndjson" or path.name == "git-refs.txt":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                row = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RefProtectionError("invalid active evidence journal") from exc
            if isinstance(row, dict):
                _collect_manifest_refs(row, protected, [])
    if _evidence_snapshot(root) != before_snapshot:
        raise RefProtectionError("evidence root changed while being scanned")
    return protected


def _date_from_ref(ref: str) -> date | None:
    match = _DATE.search(ref)
    if not match:
        return None
    raw = match.group(1)
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def prune_protected_refs(repo: str | Path, before: str | date, *, evidence_root: str | Path | None = None) -> list[str]:
    """Delete only date-eligible unprotected refs after validating all protection inputs."""

    if evidence_root is None:
        raise RefProtectionError("evidence-root is mandatory for protected prune")
    repo_path = Path(repo)
    cutoff = date.fromisoformat(before) if isinstance(before, str) else before
    protected = protected_refs(evidence_root)
    refs = _git(repo_path, "for-each-ref", "--format=%(refname)").splitlines()
    eligible: list[str] = []
    for ref in refs:
        run_id = parse_run_id(ref)
        if run_id is not None or ref.startswith("refs/implbench/runs/oi-pi-bakeoff-") or ref.startswith("refs/implbench/results/oi-pi-bakeoff-"):
            protected.add(ref)
        stamp = _date_from_ref(ref)
        if stamp is not None and stamp < cutoff and ref not in protected:
            eligible.append(ref)
    for ref in eligible:
        _git(repo_path, "update-ref", "-d", ref)
    return eligible


protect_refs = protected_refs
