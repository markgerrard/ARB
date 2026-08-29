"""Receipt-only completion sealing and descriptor-held materialization digests."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .records import RecordError, canonical_json_bytes, make_identity, record_digest, validate_record


class CompletionError(ValueError):
    """Raised when a completion cannot be safely sealed or verified."""


FINAL_TREE_DIGEST_VERSION = "final-tree-v1"


def _name_bytes(name: str) -> bytes:
    try:
        raw = name.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CompletionError("materialization contains invalid UTF-8") from exc
    import unicodedata

    if unicodedata.normalize("NFC", name) != name or name in {"", ".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise CompletionError("materialization contains a non-canonical path component")
    return raw


def _update(h: Any, kind: bytes, path: bytes, metadata: Mapping[str, Any]) -> None:
    header = canonical_json_bytes({"kind": kind.decode(), "metadata": dict(metadata), "path": path.decode("utf-8")})
    h.update(len(header).to_bytes(8, "big"))
    h.update(header)


def materialization_digest(root: str | Path, *, version: str = FINAL_TREE_DIGEST_VERSION, exclude_git_metadata: bool = True) -> str:
    """Hash the complete fixed worktree without following directory links."""

    if version != FINAL_TREE_DIGEST_VERSION:
        raise CompletionError("unknown final tree digest version")
    root_path = Path(root)
    if not root_path.is_absolute() or root_path.is_symlink() or not root_path.is_dir():
        raise CompletionError("materialization root must be a real absolute directory")
    root_real = root_path.resolve(strict=True)
    root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    digest = hashlib.sha256(b"implbench-final-tree-v1\0")

    def walk(directory_fd: int, prefix: str, *, is_root: bool) -> None:
        try:
            entries = list(os.scandir(directory_fd))
        except OSError as exc:
            raise CompletionError("cannot enumerate materialization directory") from exc
        entries.sort(key=lambda entry: _name_bytes(entry.name))
        for entry in entries:
            name_bytes = _name_bytes(entry.name)
            if exclude_git_metadata and is_root and entry.name == ".git":
                continue
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                info = os.lstat(entry.name, dir_fd=directory_fd)
            except OSError as exc:
                raise CompletionError("materialization entry disappeared") from exc
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(entry.name, dir_fd=directory_fd).encode("utf-8", "strict")
                target_path = Path(os.fsdecode(target))
                resolved = (root_path / prefix / entry.name).resolve(strict=False) if not target_path.is_absolute() else target_path.resolve(strict=False)
                try:
                    resolved.relative_to(root_real)
                except ValueError as exc:
                    raise CompletionError("escaping symlink is forbidden") from exc
                try:
                    target_info = os.lstat(resolved)
                except OSError as exc:
                    raise CompletionError("dangling symlink is forbidden") from exc
                if stat.S_ISDIR(target_info.st_mode):
                    raise CompletionError("symlinked directory component is forbidden")
                _update(digest, b"symlink", path.encode("utf-8"), {"mode": stat.S_IMODE(info.st_mode), "size": len(target)})
                digest.update(len(target).to_bytes(8, "big")); digest.update(target)
                continue
            if stat.S_ISDIR(info.st_mode):
                _update(digest, b"directory", path.encode("utf-8"), {"mode": stat.S_IMODE(info.st_mode)})
                child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                before = os.fstat(child_fd)
                try:
                    walk(child_fd, path, is_root=False)
                    after = os.fstat(child_fd)
                finally:
                    os.close(child_fd)
                if (before.st_ino, before.st_dev, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_mtime_ns):
                    raise CompletionError("directory changed during materialization")
                continue
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise CompletionError("hardlinked materialization is forbidden")
                fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                before = os.fstat(fd)
                _update(digest, b"file", path.encode("utf-8"), {"mode": stat.S_IMODE(info.st_mode), "executable": bool(info.st_mode & stat.S_IXUSR), "size": before.st_size})
                try:
                    while True:
                        chunk = os.read(fd, 131072)
                        if not chunk:
                            break
                        digest.update(len(chunk).to_bytes(8, "big")); digest.update(chunk)
                    after = os.fstat(fd)
                finally:
                    os.close(fd)
                if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
                    raise CompletionError("file changed during materialization")
                continue
            raise CompletionError("unsupported materialization file type")

    try:
        walk(root_fd, "", is_root=True)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


@dataclass(frozen=True)
class CompletionResult:
    decision: str
    reason: str
    payload: dict[str, Any]


def _status(status: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"head", "dirty", "final_tree_digest", "final_tree_digest_version"}
    if not isinstance(status, Mapping) or set(status) != expected or not isinstance(status["dirty"], bool):
        raise CompletionError("Git service status payload is not closed")
    if status["final_tree_digest_version"] != FINAL_TREE_DIGEST_VERSION:
        raise CompletionError("Git service status version is not pinned")
    if not isinstance(status["final_tree_digest"], str) or len(status["final_tree_digest"]) != 64:
        raise CompletionError("Git service status digest is invalid")
    return dict(status)


class CompletionVerifier:
    def __init__(self, key: bytes, *, identity: Mapping[str, Any], fixture_root_oid: str):
        self.key = key
        self.identity = dict(identity)
        self.fixture_root_oid = fixture_root_oid

    def final_status(self, getter: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        try:
            return _status(getter())
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "worktree-setup"}

    def _receipt_payloads(self, receipts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in receipts:
            if not isinstance(row, Mapping):
                raise CompletionError("scored completion receipt is not an authenticated record")
            if row.get("record_type") != "git-receipt":
                raise CompletionError("scored completion accepts authenticated git-receipt records only")
            try:
                validate_record(row)
            except RecordError as exc:
                raise CompletionError(str(exc)) from exc
            if not {"sequence", "nonce", "mac", "prior_record_digest"} <= set(row):
                raise CompletionError("receipt authentication is incomplete")
            unsigned = dict(row)
            mac = unsigned.pop("mac")
            expected_mac = hmac.new(self.key, canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()
            if not isinstance(mac, str) or not hmac.compare_digest(mac, expected_mac):
                raise CompletionError("receipt authentication failed")
            if row.get("cell_id") != self.identity.get("cell_id") or row.get("attempt_id") != self.identity.get("attempt_id"):
                raise CompletionError("receipt identity mismatch")
            payload = dict(row["payload"])
            if payload.get("tree_digest_version") != FINAL_TREE_DIGEST_VERSION:
                raise CompletionError("receipt tree digest version is not pinned")
            result.append(payload)
        return result

    def verify(self, receipts: list[Mapping[str, Any]], status: Mapping[str, Any], worktree: str | Path) -> CompletionResult:
        final_status = _status(status)
        final_digest = materialization_digest(worktree, version=final_status["final_tree_digest_version"])
        if final_digest != final_status["final_tree_digest"]:
            raise CompletionError("final status digest does not bind materialization")
        payloads = self._receipt_payloads(receipts)
        payload = {"cell_id": self.identity["cell_id"], "attempt_id": self.identity["attempt_id"], "fixture_root": self.fixture_root_oid, "receipts": payloads, "head": final_status["head"], "dirty": final_status["dirty"], "final_tree_digest": final_digest, "final_tree_digest_version": FINAL_TREE_DIGEST_VERSION}
        if not payloads:
            if final_status["head"] != self.fixture_root_oid:
                raise CompletionError("empty receipt completion does not bind the fixture root HEAD")
            payload["receipts"] = []
            reason = "dirty-empty-receipts" if final_status["dirty"] else "empty-receipts"
            return CompletionResult("not-delivered", reason, payload)
        expected_parent = self.fixture_root_oid
        for receipt in payloads:
            if receipt["fixture_root_oid"] != self.fixture_root_oid or receipt["ordered_parent_oids"] != [expected_parent]:
                return CompletionResult("not-delivered", "receipt-chain", payload)
            expected_parent = receipt["commit_oid"]
        if final_status["dirty"] or final_status["head"] != payloads[-1]["commit_oid"] or final_digest != payloads[-1]["tree_digest"]:
            return CompletionResult("not-delivered", "dirty-or-digest-mismatch", payload)
        return CompletionResult("agent-delivered", "verified", payload)


def seal_completion(receipt_chain: Any, verifier: CompletionVerifier, *, status: Mapping[str, Any], worktree: str | Path) -> CompletionResult:
    rows = receipt_chain._rows()
    receipts = [row for row in rows if row.get("record_type") == "git-receipt"]
    result = verifier.verify(receipts, status, worktree)
    record = make_identity(verifier.identity, record_type="completion", payload=result.payload)
    try:
        validate_record(record)
        sealed = receipt_chain.log.append(record)
    except (RecordError, Exception) as exc:
        if isinstance(exc, CompletionError):
            raise
        raise CompletionError(str(exc)) from exc
    return CompletionResult(result.decision, result.reason, sealed["payload"])


def verify_post_g4_attestation(record: Mapping[str, Any], *, identity: Mapping[str, Any], expected: Mapping[str, Any], seen_digests: set[str]) -> bool:
    try:
        validate_record(record)
    except RecordError as exc:
        raise CompletionError(str(exc)) from exc
    if record.get("record_type") != "post-g4-attestation" or any(record.get(field) != identity.get(field) for field in ("run_id", "cell_id", "attempt_id")):
        raise CompletionError("post-G4 identity pin mismatch")
    digest = record_digest(record)
    if digest in seen_digests or record.get("mac") in seen_digests:
        raise CompletionError("post-G4 attestation replay")
    if dict(record["payload"]) != dict(expected):
        raise CompletionError("post-G4 attestation digest mismatch")
    return True
