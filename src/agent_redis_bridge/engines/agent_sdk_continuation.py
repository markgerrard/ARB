from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path


class ContinuationWorkspaceError(Exception):
    """A saved agent-sdk continuation workspace cannot be safely used."""


@dataclass(frozen=True)
class ContinuationWorkspace:
    """The isolated worktree authorized for one persisted SDK session."""

    thread_id: str
    sender: str
    worktree_name: str


class ContinuationWorkspaceLease:
    """An exclusive, cross-process lock for one persisted continuation."""

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


class ContinuationWorkspaceStore:
    """Durably bind a session to its creator and persistent isolated worktree."""

    def __init__(self, root: Path, agent_id: str) -> None:
        self.root = root / _safe_part(agent_id) / "continuation-workspaces"

    def _path(self, thread_id: str) -> Path:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def record(self, *, thread_id: str, sender: str, worktree_name: str) -> None:
        record = ContinuationWorkspace(thread_id=thread_id, sender=sender, worktree_name=worktree_name)
        path = self._path(thread_id)
        body = json.dumps(record.__dict__, separators=(",", ":"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self.load(thread_id)
            if existing != record:
                raise ContinuationWorkspaceError("continuation-workspace-conflict")
            return
        except OSError as exc:
            raise ContinuationWorkspaceError("continuation-workspace-write-failed") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.write("\n")
        except OSError as exc:
            raise ContinuationWorkspaceError("continuation-workspace-write-failed") from exc

    def load(self, thread_id: str) -> ContinuationWorkspace | None:
        path = self._path(thread_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ContinuationWorkspaceError("continuation-workspace-corrupt") from exc
        if not isinstance(raw, dict):
            raise ContinuationWorkspaceError("continuation-workspace-corrupt")
        values = (raw.get("thread_id"), raw.get("sender"), raw.get("worktree_name"))
        if not all(isinstance(value, str) and value for value in values):
            raise ContinuationWorkspaceError("continuation-workspace-corrupt")
        if raw["thread_id"] != thread_id:
            raise ContinuationWorkspaceError("continuation-workspace-corrupt")
        return ContinuationWorkspace(
            thread_id=raw["thread_id"], sender=raw["sender"], worktree_name=raw["worktree_name"]
        )

    def acquire(self, thread_id: str) -> ContinuationWorkspaceLease:
        """Reserve this session until its resume turn has completed."""
        path = self._path(thread_id).with_suffix(".lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ContinuationWorkspaceError("continuation-workspace-lock-failed") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ContinuationWorkspaceError("continuation-worktree-busy") from exc
        except OSError as exc:
            os.close(fd)
            raise ContinuationWorkspaceError("continuation-workspace-lock-failed") from exc
        return ContinuationWorkspaceLease(fd)
