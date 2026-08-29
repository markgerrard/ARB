from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Callable


logger = logging.getLogger(__name__)


class WorktreeLeaseError(Exception):
    """A bridge-minted worktree lease cannot be safely created or used."""


@dataclass(frozen=True)
class WorktreeLeaseRecord:
    lease_id: str
    sender: str
    worktree_name: str
    repo_identity: str
    base_oid: str
    created_at: float
    expires_at: float
    state: str = "armed"
    tombstone_reason: str | None = None
    tombstoned_at: float | None = None
    # Slice 1d-ii: server-side lane and arm replay key. Defaults keep existing
    # on-disk JSON records loadable without a migration.
    lane: str = "gated"
    arm_request_id: str | None = None


class WorktreeLeaseLock:
    """Exclusive cross-process ownership of one lease operation/turn."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


def _safe_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


class WorktreeLeaseStore:
    """Durable bridge-owned registry for staged worktree leases."""

    def __init__(self, root: Path, agent_id: str) -> None:
        self.root = root / _safe_part(agent_id) / "worktree-leases"

    def _path(self, lease_id: str) -> Path:
        digest = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def _write_new(self, record: WorktreeLeaseRecord) -> None:
        path = self._path(record.lease_id)
        temporary = path.with_suffix(f".{os.getpid()}.{secrets.token_hex(8)}.tmp")
        body = (json.dumps(asdict(record), separators=(",", ":")) + "\n").encode("utf-8")
        fd = -1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short worktree lease write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # Publishing by hard link is atomic and refuses an existing final
            # path, preserving the original O_EXCL collision semantics without
            # ever exposing the partially written temporary file as a record.
            os.link(temporary, path)
        except FileExistsError as exc:
            if path.exists():
                raise WorktreeLeaseError("worktree-lease-id-collision") from exc
            raise WorktreeLeaseError("worktree-lease-write-failed") from exc
        except OSError as exc:
            raise WorktreeLeaseError("worktree-lease-write-failed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.error("[worktree-lease] temporary-cleanup-failed path=%s", temporary)

    def _replace(self, record: WorktreeLeaseRecord) -> None:
        path = self._path(record.lease_id)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(asdict(record), separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise WorktreeLeaseError("worktree-lease-write-failed") from exc

    @staticmethod
    def mint_lease_id() -> str:
        """Mint an unguessable lease id before lock acquisition / publication."""
        return secrets.token_urlsafe(32)

    def create(
        self,
        *,
        sender: str,
        worktree_name: str,
        repo_identity: str,
        base_oid: str,
        ttl: int,
        now: float | None = None,
        lease_id: str | None = None,
        lane: str = "gated",
        arm_request_id: str | None = None,
    ) -> WorktreeLeaseRecord:
        """Publish a durable filesystem lease record.

        When ``lease_id`` is supplied (the locked arm path), publish exactly that
        id. When omitted, mint with collision retries for callers that have not
        yet split mint from publication.
        """
        created_at = time.time() if now is None else now
        if lane not in {"gated", "exempt"}:
            raise WorktreeLeaseError("worktree-lease-lane-invalid")
        if lease_id is not None:
            record = WorktreeLeaseRecord(
                lease_id=lease_id,
                sender=sender,
                worktree_name=worktree_name,
                repo_identity=repo_identity,
                base_oid=base_oid,
                created_at=created_at,
                expires_at=created_at + ttl,
                lane=lane,
                arm_request_id=arm_request_id,
            )
            self._write_new(record)
            return record
        for _ in range(4):
            record = WorktreeLeaseRecord(
                lease_id=self.mint_lease_id(),
                sender=sender,
                worktree_name=worktree_name,
                repo_identity=repo_identity,
                base_oid=base_oid,
                created_at=created_at,
                expires_at=created_at + ttl,
                lane=lane,
                arm_request_id=arm_request_id,
            )
            try:
                self._write_new(record)
                return record
            except WorktreeLeaseError as exc:
                if str(exc) != "worktree-lease-id-collision":
                    raise
        raise WorktreeLeaseError("worktree-lease-id-collision")

    def find_by_arm_request_id(self, request_id: str) -> WorktreeLeaseRecord | None:
        """Return the newest record that was armed for this request id, if any."""
        if not isinstance(request_id, str) or not request_id:
            return None
        matches = [
            record for record in self.records() if record.arm_request_id == request_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def load(self, lease_id: str) -> WorktreeLeaseRecord | None:
        if not isinstance(lease_id, str) or not lease_id:
            return None
        try:
            raw = json.loads(self._path(lease_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorktreeLeaseError("worktree-lease-corrupt") from exc
        try:
            record = WorktreeLeaseRecord(**raw)
        except (TypeError, ValueError) as exc:
            raise WorktreeLeaseError("worktree-lease-corrupt") from exc
        if record.lease_id != lease_id or record.state not in {"armed", "tombstoned"}:
            raise WorktreeLeaseError("worktree-lease-corrupt")
        return record

    def records(self) -> list[WorktreeLeaseRecord]:
        if not self.root.exists():
            return []
        records: list[WorktreeLeaseRecord] = []
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = WorktreeLeaseRecord(**raw)
                if (
                    not isinstance(record.lease_id, str)
                    or not record.lease_id
                    or not isinstance(record.sender, str)
                    or not record.sender
                    or not isinstance(record.worktree_name, str)
                    or not record.worktree_name
                    or not isinstance(record.repo_identity, str)
                    or not record.repo_identity
                    or not isinstance(record.base_oid, str)
                    or not record.base_oid
                    or type(record.created_at) not in {int, float}
                    or type(record.expires_at) not in {int, float}
                    or (
                        record.tombstoned_at is not None
                        and type(record.tombstoned_at) not in {int, float}
                    )
                    or record.state not in {"armed", "tombstoned"}
                    or self._path(record.lease_id) != path
                ):
                    raise ValueError("record identity or state mismatch")
                records.append(record)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                quarantine = path.with_suffix(".quarantine")
                if quarantine.exists():
                    quarantine = path.with_suffix(f".{time.time_ns()}.quarantine")
                try:
                    path.replace(quarantine)
                    logger.error(
                        "[worktree-lease] corrupt-record-quarantined path=%s quarantine=%s error=%s",
                        path, quarantine, exc,
                    )
                except OSError as quarantine_exc:
                    logger.error(
                        "[worktree-lease] corrupt-record-quarantine-failed path=%s error=%s",
                        path, quarantine_exc,
                    )
        return records

    def acquire(self, lease_id: str) -> WorktreeLeaseLock:
        path = self._path(lease_id).with_suffix(".lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise WorktreeLeaseError("worktree-lease-lock-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WorktreeLeaseError("worktree-lease-busy") from exc
        except OSError as exc:
            os.close(fd)
            raise WorktreeLeaseError("worktree-lease-lock-failed") from exc
        return WorktreeLeaseLock(fd)

    def tombstone(
        self, record: WorktreeLeaseRecord, reason: str, *, now: float | None = None,
    ) -> WorktreeLeaseRecord:
        updated = replace(
            record, state="tombstoned", tombstone_reason=reason,
            tombstoned_at=time.time() if now is None else now,
        )
        self._replace(updated)
        return updated

    def active_counts(self) -> tuple[int, dict[str, int]]:
        active = [record for record in self.records() if record.state == "armed"]
        per_sender: dict[str, int] = {}
        for record in active:
            per_sender[record.sender] = per_sender.get(record.sender, 0) + 1
        return len(active), per_sender

    def reconcile(
        self, *, repo_identity: str, is_live: Callable[[WorktreeLeaseRecord], bool],
        remove: Callable[[WorktreeLeaseRecord], None], tombstone_ttl: int,
        now: float | None = None,
    ) -> list[tuple[str, str]]:
        """Tombstone missing registrations and sweep expired, unlocked leases."""
        current = time.time() if now is None else now
        actions: list[tuple[str, str]] = []
        for record in self.records():
            if record.state == "tombstoned":
                try:
                    legacy_tombstone_time = self._path(record.lease_id).stat().st_mtime
                except FileNotFoundError:
                    continue
                tombstoned_at = record.tombstoned_at or legacy_tombstone_time
                if tombstoned_at + tombstone_ttl <= current:
                    try:
                        self._path(record.lease_id).unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        self._path(record.lease_id).with_suffix(".lock").unlink()
                    except FileNotFoundError:
                        pass
                    actions.append((record.lease_id, "tombstone-gc"))
                continue
            if record.repo_identity != repo_identity:
                continue
            try:
                lock = self.acquire(record.lease_id)
            except WorktreeLeaseError as exc:
                if str(exc) == "worktree-lease-busy":
                    continue
                raise
            try:
                latest = self.load(record.lease_id)
                if latest is None or latest.state != "armed":
                    continue
                if not is_live(latest):
                    self.tombstone(latest, "missing-registration")
                    actions.append((latest.lease_id, "missing-registration"))
                elif latest.expires_at <= current:
                    remove(latest)
                    self.tombstone(latest, "expired")
                    actions.append((latest.lease_id, "expired"))
            finally:
                lock.release()
        return actions
