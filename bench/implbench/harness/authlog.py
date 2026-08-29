"""Descriptor-safe authenticated append-only evidence logs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .records import AUTH_FIELDS, ENVELOPE_FIELDS, MAX_RECORD_BYTES, RecordError, canonical_json_bytes, parse_canonical_json, record_digest, validate_record


class AuthLogError(RecordError):
    """Raised when an authenticated log cannot be trusted or persisted."""


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AuthLog:
    """An append-only NDJSON log whose every row is chained and MACed."""

    def __init__(self, path: str | Path, key: bytes, *, run_id: str | None = None, cell_id: str | None = None, attempt_id: str | None = None):
        if not isinstance(key, bytes) or len(key) < 32:
            raise AuthLogError("authentication key must be at least 32 bytes")
        self.path = Path(path)
        self.key = key
        self.run_id = run_id
        self.cell_id = cell_id
        self.attempt_id = attempt_id
        self.state_path = self.path.with_name(self.path.name + ".state")
        self._prepare_parent()
        if self.path.exists() or self.state_path.exists():
            self.verify()

    def _prepare_parent(self) -> None:
        if self.path.parent.exists():
            if self.path.parent.is_symlink():
                raise AuthLogError("log parent may not be a symlink")
        else:
            self.path.parent.mkdir(parents=True, mode=0o700)
        if self.path.exists() and (self.path.is_symlink() or not stat.S_ISREG(self.path.stat().st_mode)):
            raise AuthLogError("log must be a regular non-symlink file")
        if self.path.exists() and stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            raise AuthLogError("log must be mode 0600")

    def _read_rows(self) -> tuple[list[dict[str, Any]], int]:
        if not self.path.exists():
            if self.state_path.exists():
                raise AuthLogError("state exists without log")
            return [], 0
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                data = b""
                while True:
                    chunk = os.read(fd, 131072)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_RECORD_BYTES * 1024:
                        raise AuthLogError("authenticated log is unbounded")
            finally:
                os.close(fd)
        except OSError as exc:
            raise AuthLogError(f"cannot open authenticated log: {exc}") from exc
        if not data:
            return [], 0
        lines = data.splitlines(keepends=True)
        if any(not line.endswith(b"\n") for line in lines):
            raise AuthLogError("truncated NDJSON record")
        rows: list[dict[str, Any]] = []
        for line in lines:
            if len(line) > MAX_RECORD_BYTES:
                raise AuthLogError("record exceeds maximum frame size")
            rows.append(parse_canonical_json(line[:-1]))
        return rows, len(data)

    def verify(self) -> int:
        rows, byte_length = self._read_rows()
        prior: str | None = None
        nonces: set[str] = set()
        for expected, row in enumerate(rows, start=1):
            try:
                validate_record(row)
            except RecordError as exc:
                raise AuthLogError(str(exc)) from exc
            for field, expected_value in (("run_id", self.run_id), ("cell_id", self.cell_id), ("attempt_id", self.attempt_id)):
                if expected_value is not None and row[field] != expected_value:
                    raise AuthLogError(f"authenticated log {field} mismatch")
            if not set(row) <= (ENVELOPE_FIELDS | AUTH_FIELDS):
                raise AuthLogError("invalid authenticated row fields")
            if row.get("sequence") != expected:
                raise AuthLogError("sequence replay or gap")
            nonce = row.get("nonce")
            if not isinstance(nonce, str) or nonce in nonces:
                raise AuthLogError("nonce replay")
            nonces.add(nonce)
            if row.get("prior_record_digest") != prior:
                raise AuthLogError("prior-record digest chain mismatch")
            mac = row.get("mac")
            unsigned = dict(row)
            unsigned.pop("mac", None)
            expected_mac = hmac.new(self.key, canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()
            if not isinstance(mac, str) or not hmac.compare_digest(mac, expected_mac):
                raise AuthLogError("record authentication failed")
            prior = record_digest(row)
        self._verify_state(byte_length, len(rows), prior)
        return len(rows)

    def _verify_state(self, byte_length: int, sequence: int, digest: str | None) -> None:
        if not self.state_path.exists():
            if sequence:
                raise AuthLogError("authenticated log state is missing")
            return
        if self.state_path.is_symlink() or not stat.S_ISREG(self.state_path.stat().st_mode) or stat.S_IMODE(self.state_path.stat().st_mode) != 0o600:
            raise AuthLogError("invalid authenticated log state file")
        try:
            state = parse_canonical_json(self.state_path.read_bytes())
        except (OSError, RecordError) as exc:
            raise AuthLogError("invalid authenticated log state") from exc
        if state != {"bytes": byte_length, "sequence": sequence, "digest": digest}:
            raise AuthLogError("log was truncated, replaced, or replayed")

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise AuthLogError("record must be a mutable object")
        self.verify()
        rows, _ = self._read_rows()
        previous = record_digest(rows[-1]) if rows else None
        sealed = dict(record)
        sealed["sequence"] = len(rows) + 1
        sealed["nonce"] = os.urandom(32).hex()
        sealed["prior_record_digest"] = previous
        sealed.pop("mac", None)
        validate_record(sealed)
        sealed["mac"] = hmac.new(self.key, canonical_json_bytes(sealed), hashlib.sha256).hexdigest()
        payload = canonical_json_bytes(sealed) + b"\n"
        if len(payload) > MAX_RECORD_BYTES:
            raise AuthLogError("record exceeds maximum frame size")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short authenticated append")
                view = view[written:]
            os.fsync(fd)
        except OSError as exc:
            raise AuthLogError(f"authenticated append failed: {exc}") from exc
        finally:
            os.close(fd)
        self._write_state(len(rows) + 1, record_digest(sealed), self.path.stat().st_size)
        return sealed

    def _write_state(self, sequence: int, digest: str, byte_length: int) -> None:
        state = canonical_json_bytes({"bytes": byte_length, "sequence": sequence, "digest": digest})
        fd, temporary = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(state)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            _fsync_directory(self.state_path.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def __enter__(self) -> "AuthLog":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class BoundedQuarantine:
    """Private diagnostic retention with a hard aggregate byte bound."""

    def __init__(self, path: str | Path, *, max_bytes: int = 64 * 1024):
        if max_bytes <= 0:
            raise AuthLogError("quarantine bound must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and (self.path.is_symlink() or not stat.S_ISREG(self.path.stat().st_mode)):
            raise AuthLogError("quarantine must be a regular file")

    def store(self, diagnostic: str | bytes) -> str:
        raw = diagnostic.encode("utf-8") if isinstance(diagnostic, str) else bytes(diagnostic)
        digest = hashlib.sha256(raw).hexdigest()
        encoded = base64.b64encode(raw).decode("ascii")
        row = json.dumps({"digest": digest, "diagnostic_b64": encoded}, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(row) > self.max_bytes:
            raise AuthLogError("quarantine bound exceeded")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, row)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.path.parent)
        return digest
