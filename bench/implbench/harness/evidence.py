"""Controller-owned immutable evidence packages for the bakeoff."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .records import RecordError, canonical_json_bytes, public_projection, validate_record


class EvidencePackageError(ValueError):
    """Raised when a package is malformed, unsafe, or already sealed."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        if path.read_bytes() != payload:
            raise EvidencePackageError(f"immutable file changed: {path.name}")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _ensure_empty_file(path: Path) -> None:
    """Create an append target only when it is absent; never overwrite prior rows."""

    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise EvidencePackageError(f"unsafe evidence journal: {path.name}")
        return
    _write_atomic(path, b"", overwrite=False)


def _canonical_json_line(value: Mapping[str, Any]) -> bytes:
    try:
        return canonical_json_bytes(value) + b"\n"
    except RecordError as exc:
        raise EvidencePackageError(str(exc)) from exc


def _forbidden_public_values(value: Any) -> None:
    forbidden = {"stdout", "stderr", "traceback", "assertion", "diagnostic", "battery", "secret", "credential"}
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in forbidden or str(key).lower().endswith(("_stdout", "_stderr", "_traceback")):
                raise EvidencePackageError(f"dynamic or secret evidence field: {key}")
            _forbidden_public_values(child)
    elif isinstance(value, list):
        for child in value:
            _forbidden_public_values(child)


def manifest_digest(root: str | Path) -> str:
    path = Path(root) / "manifest.json"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise EvidencePackageError("manifest is unreadable") from exc
    return hashlib.sha256(payload).hexdigest()


def final_ref_index(
    manifest_sha: str,
    journal_tail_sha: str,
    refs: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    if not isinstance(manifest_sha, str) or len(manifest_sha) != 64 or not all(c in "0123456789abcdef" for c in manifest_sha):
        raise EvidencePackageError("invalid manifest digest")
    if not isinstance(journal_tail_sha, str) or len(journal_tail_sha) != 64 or not all(c in "0123456789abcdef" for c in journal_tail_sha):
        raise EvidencePackageError("invalid journal digest")
    rows = [{"ref": ref, "oid": oid} for ref, oid in refs]
    if any(not isinstance(row["ref"], str) or not row["ref"].startswith("refs/") or not isinstance(row["oid"], str) or len(row["oid"]) != 40 or any(c not in "0123456789abcdef" for c in row["oid"]) for row in rows):
        raise EvidencePackageError("invalid final ref")
    rows.sort(key=lambda row: row["ref"].encode("utf-8"))
    if len({row["ref"] for row in rows}) != len(rows):
        raise EvidencePackageError("duplicate final ref")
    return {"schema_version": "git-refs-v1", "manifest_digest": manifest_sha, "journal_tail_digest": journal_tail_sha, "refs": rows}


def _validate_final_index(value: Any, expected_manifest: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "manifest_digest", "journal_tail_digest", "refs"}:
        raise EvidencePackageError("final ref index fields mismatch")
    if value["schema_version"] != "git-refs-v1" or value["manifest_digest"] != expected_manifest:
        raise EvidencePackageError("final ref index version or manifest mismatch")
    actual = final_ref_index(value["manifest_digest"], value["journal_tail_digest"], ((row["ref"], row["oid"]) for row in value["refs"]))
    if dict(value) != actual:
        raise EvidencePackageError("final ref index is not canonical")
    return dict(value)


@dataclass(frozen=True)
class EvidencePackage:
    root: Path
    manifest_digest: str

    @classmethod
    def create(cls, root: str | Path, manifest: Mapping[str, Any]) -> "EvidencePackage":
        path = Path(root)
        if not path.is_absolute():
            raise EvidencePackageError("evidence root must be absolute")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        manifest_payload = _canonical_json_line(manifest)
        _write_atomic(path / "manifest.json", manifest_payload)
        for name in ("results", "reports", "seat-config-redacted", "preflight"):
            (path / name).mkdir(mode=0o700, exist_ok=True)
            os.chmod(path / name, 0o700)
        _write_atomic(path / "preflight" / "private-digests.ndjson", b"", overwrite=False)
        run_id = manifest.get("run_id")
        if isinstance(run_id, str) and run_id:
            _write_atomic(path / "results" / f"{run_id}.ndjson", b"", overwrite=False)
        _write_atomic(path / "cells.ndjson", b"", overwrite=False)
        return cls(path, hashlib.sha256(manifest_payload).hexdigest())

    @classmethod
    def open(cls, root: str | Path) -> "EvidencePackage":
        path = Path(root)
        if not path.is_absolute() or not path.is_dir():
            raise EvidencePackageError("evidence root must be an absolute directory")
        if not (path / "manifest.json").is_file():
            raise EvidencePackageError("manifest.json is missing")
        return cls(path, manifest_digest(path))

    @property
    def is_sealed(self) -> bool:
        return (self.root / "git-refs.txt").exists()

    @property
    def journal_tail_digest(self) -> str:
        journal = self.root / "cells.ndjson"
        return hashlib.sha256(journal.read_bytes() if journal.exists() else b"").hexdigest()

    def _assert_mutable(self) -> None:
        if self.is_sealed:
            raise EvidencePackageError("sealed evidence package is immutable")

    def append_public(self, value: Mapping[str, Any]) -> None:
        self._assert_mutable()
        _forbidden_public_values(value)
        payload = _canonical_json_line(value)
        path = self.root / "cells.ndjson"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(path.parent)

    def append_record(self, record: Mapping[str, Any]) -> None:
        """Append only an authenticated record's value-free projection to results."""
        self._assert_mutable()
        try:
            validated = validate_record(record)
        except RecordError as exc:
            raise EvidencePackageError(str(exc)) from exc
        if not {"sequence", "nonce", "mac"} <= set(validated):
            raise EvidencePackageError("result record must be authenticated")
        run_id = validated["run_id"]
        if not isinstance(run_id, str) or "/" in run_id or "\\" in run_id:
            raise EvidencePackageError("invalid result run ID")
        path = self.root / "results" / f"{run_id}.ndjson"
        _ensure_empty_file(path)
        self._append_bytes(path, _canonical_json_line(public_projection(validated)))

    def _append_bytes(self, path: Path, payload: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(path.parent)

    def append_private_digest(self, diagnostic: str | bytes) -> str:
        """Retain only a bounded digest for private diagnostics; raw text never enters the package."""
        self._assert_mutable()
        raw = diagnostic.encode("utf-8") if isinstance(diagnostic, str) else bytes(diagnostic)
        digest = hashlib.sha256(raw).hexdigest()
        self._append_bytes(self.root / "preflight" / "private-digests.ndjson", _canonical_json_line({"digest": digest}))
        return digest

    def seal(self, refs: Iterable[tuple[str, str]]) -> None:
        self._assert_mutable()
        index = final_ref_index(self.manifest_digest, self.journal_tail_digest, refs)
        for name in ("worktree-accounting.txt", "final-comparison.md"):
            _write_atomic(self.root / name, b"")
        _write_atomic(self.root / "git-refs.txt", _canonical_json_line(index))

    def validate(self, *, require_sealed: bool = False) -> None:
        if not self.root.is_absolute() or self.root.is_symlink() or stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise EvidencePackageError("unsafe evidence root")
        manifest = self.root / "manifest.json"
        if manifest.is_symlink() or stat.S_IMODE(manifest.stat().st_mode) != 0o600:
            raise EvidencePackageError("manifest must be mode 0600")
        for name in ("cells.ndjson", "results", "reports", "seat-config-redacted", "preflight"):
            entry = self.root / name
            if not entry.exists() or entry.is_symlink():
                raise EvidencePackageError(f"package entry is missing or unsafe: {name}")
        raw = manifest.read_bytes()
        try:
            parsed = json.loads(raw[:-1].decode("utf-8")) if raw.endswith(b"\n") else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidencePackageError("manifest is not valid JSON") from exc
        if not isinstance(parsed, Mapping) or _canonical_json_line(parsed) != raw:
            raise EvidencePackageError("manifest is not canonical")
        for path in (self.root / "cells.ndjson", *sorted((self.root / "results").glob("*.ndjson"))):
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise EvidencePackageError(f"unsafe evidence journal: {path.name}")
            for line in path.read_bytes().splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    raise EvidencePackageError("truncated evidence journal")
                try:
                    value = json.loads(line[:-1].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EvidencePackageError("invalid evidence journal") from exc
                if isinstance(value, Mapping):
                    _forbidden_public_values(value)
        if require_sealed and not self.is_sealed:
            raise EvidencePackageError("closed-run operation requires sealed package")
        if self.is_sealed:
            index_path = self.root / "git-refs.txt"
            if stat.S_IMODE(index_path.stat().st_mode) != 0o600:
                raise EvidencePackageError("final ref index must be mode 0600")
            try:
                value = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidencePackageError("invalid final ref index") from exc
            _validate_final_index(value, self.manifest_digest)


def validate_evidence_package(root: str | Path, *, require_sealed: bool = False) -> EvidencePackage:
    package = EvidencePackage.open(root)
    package.validate(require_sealed=require_sealed)
    return package


def census_legacy_adapters(root: str | Path) -> tuple[tuple[str, int, str], ...]:
    """Find every Recorder/no-root-prune definition, import, and call in the full tree."""
    symbols = {"Recorder", "prune_refs"}
    hits: list[tuple[str, int, str]] = []
    for path in sorted(Path(root).rglob("*.py"), key=lambda item: item.as_posix()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            symbol: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in symbols:
                symbol = node.name
            elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in symbols:
                symbol = node.name.rsplit(".", 1)[-1]
            elif isinstance(node, ast.Name) and node.id in symbols:
                symbol = node.id
            elif isinstance(node, ast.Attribute) and node.attr in symbols:
                symbol = node.attr
            if symbol is not None:
                hits.append((str(path), node.lineno, symbol))
    return tuple(hits)


EvidencePackageError.__module__ = __name__
