"""Closed, bounded Git RPC service for scored implementation cells."""

from __future__ import annotations

import os
import json
import socket
import hashlib
import tempfile
import re
import shutil
import stat
import subprocess
import threading
import time
import unicodedata
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Mapping

from .manifest import GIT_RPC
from .records import RecordError, canonical_json_bytes
from .sandbox import LaunchSpec, spawn_child


GIT_RPC_CONSTANTS = dict(GIT_RPC)


class GitRPCError(ValueError):
    """Raised for an invalid or refused Git RPC."""


class GitBudgetExceeded(GitRPCError):
    """Raised when an operation exceeds its service-owned bound."""


class AttemptGitServiceServer:
    """Controller-owned, attempt-scoped Unix RPC edge for a scored GitService.

    The capability is minted once by the controller and travels only in the signed
    dispatch envelope.  It is not an inherited environment secret and the socket is
    removed as part of terminal close.
    """
    def __init__(self, service: "GitService", *, root: str | Path, attempt_id: str, tool_gid: int, peer_uids: tuple[int, ...]):
        if not attempt_id.startswith("attempt-"):
            raise GitRPCError("remote Git service requires an attempt identity")
        self.service = service
        self.root = Path(root)
        # Darwin's AF_UNIX sockaddr is only 104 bytes; retain attempt scoping in a
        # fixed-width digest rather than embedding the full cell/run pathname.
        self.endpoint = Path(tempfile.gettempdir()) / ("implbench-g-" + hashlib.sha256((str(self.root) + attempt_id).encode()).hexdigest()[:24] + ".sock")
        self.capability = os.urandom(32).hex()
        if isinstance(tool_gid, bool) or not isinstance(tool_gid, int) or tool_gid <= 0:
            raise GitRPCError("attempt Git RPC requires a provisioned tool group")
        if not peer_uids or any(isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 for uid in peer_uids):
            raise GitRPCError("attempt Git RPC requires dedicated bridge peer identities")
        self.tool_gid = tool_gid
        self.peer_uids = frozenset(peer_uids)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> dict[str, str]:
        self.endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.endpoint.exists() or self.endpoint.is_symlink():
            raise GitRPCError("attempt Git RPC endpoint already exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.endpoint))
            # Group access is assigned to the provisioned tool plane.  A controller that
            # cannot make that assignment cannot safely expose this socket.
            # A real git-service child starts with the provisioned tool group.
            # Avoid a needless privileged chown in that normal case; a mismatched
            # group is still corrected only when the launcher has authority.
            if os.getgid() != self.tool_gid:
                os.chown(self.endpoint, -1, self.tool_gid)
            os.chmod(self.endpoint, 0o660)
            listener.listen(8)
            listener.settimeout(0.1)
        except Exception:
            listener.close()
            self.endpoint.unlink(missing_ok=True)
            raise
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="implbench-attempt-git", daemon=True)
        self._thread.start()
        return {"endpoint": str(self.endpoint), "capability": self.capability}

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    if self._peer_credentials(connection) not in self.peer_uids:
                        raise GitRPCError("attempt Git RPC peer identity is not authorized")
                    with connection.makefile("rb") as stream:
                        request = FrameReader(stream).read()
                    if request.get("capability") != self.capability or set(request) != {"capability", "request"}:
                        raise GitRPCError("attempt Git RPC authentication failed")
                    reply: Mapping[str, Any] = {"ok": True, "value": self.service.handle(request["request"], actor="tool")}
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                try:
                    connection.sendall(encode_frame(reply))
                except OSError:
                    pass

    @staticmethod
    def _peer_credentials(connection: socket.socket) -> int | None:
        try:
            if hasattr(socket, "SO_PEERCRED"):
                return int.from_bytes(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)[4:8], "little")
            if hasattr(socket, "LOCAL_PEERCRED"):
                # Darwin's LOCAL_PEERCRED returns struct xucred (version, uid, ...).
                return int.from_bytes(connection.getsockopt(0, socket.LOCAL_PEERCRED, 128)[4:8], "little")
            return connection.getpeereid()[0]  # type: ignore[attr-defined]
        except OSError:
            return None

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.endpoint.unlink(missing_ok=True)


class RemoteGitService:
    """Bridge-side client for the controller-owned attempt Git service."""
    def __init__(self, *, endpoint: str, capability: str, tool_gid: int):
        if not os.path.isabs(endpoint) or not re.fullmatch(r"[0-9a-f]{64}", capability):
            raise GitRPCError("remote Git service binding is malformed")
        self.endpoint, self.capability, self.tool_gid = endpoint, capability, tool_gid
        self.receipt_chain = object()  # authentication is the one-shot envelope capability.

    def handle(self, request: Mapping[str, Any], *, actor: str = "tool") -> dict[str, Any]:
        if actor != "tool":
            raise GitRPCError("remote Git service actor is invalid")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(self.endpoint)
            connection.sendall(encode_frame({"capability": self.capability, "request": dict(request)}))
            with connection.makefile("rb") as stream:
                reply = FrameReader(stream).read()
        if reply.get("ok") is not True or not isinstance(reply.get("value"), dict):
            raise GitRPCError(str(reply.get("error", "remote Git service failed")))
        return dict(reply["value"])

    def completion_projection(self) -> dict[str, Any]:
        # Completion is fetched from the controller after the turn, never supplied by the model.
        return {"mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": [], "dirty": False,
                "seal_complete": False, "receipts_authenticated": True, "infrastructure_failure": "awaiting-controller-close"}


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise GitRPCError("truncated RPC frame")
        chunk = bytes(chunk)
        if len(chunk) > remaining:
            raise GitRPCError("RPC reader returned excess bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_frame(value: Mapping[str, Any]) -> bytes:
    try:
        payload = canonical_json_bytes(value)
    except RecordError as exc:
        raise GitRPCError(str(exc)) from exc
    if len(payload) > GIT_RPC_CONSTANTS["max_frame_bytes"]:
        raise GitRPCError("RPC frame exceeds max_frame_bytes")
    return len(payload).to_bytes(4, "big") + payload


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, Any]:
    data = bytes(frame)
    if len(data) < 4:
        raise GitRPCError("RPC frame is missing its four-byte length")
    size = int.from_bytes(data[:4], "big")
    if size > GIT_RPC_CONSTANTS["max_frame_bytes"]:
        raise GitRPCError("RPC frame exceeds max_frame_bytes")
    if len(data) != size + 4:
        raise GitRPCError("RPC frame has an inexact body")
    try:
        from .records import parse_canonical_json

        return parse_canonical_json(data[4:])
    except RecordError as exc:
        raise GitRPCError(str(exc)) from exc


class FrameReader:
    """Read exact-length canonical frames without reading beyond a request."""

    def __init__(self, stream: Any, *, bucket: "TokenBucket | None" = None):
        self.stream = stream
        self.bucket = bucket

    def read(self) -> dict[str, Any]:
        if self.bucket is not None and not self.bucket.take():
            raise GitBudgetExceeded("status rate limit exceeded")
        header = _read_exact(self.stream, 4)
        size = int.from_bytes(header, "big")
        if size > GIT_RPC_CONSTANTS["max_frame_bytes"]:
            raise GitRPCError("RPC frame exceeds max_frame_bytes")
        return decode_frame(header + _read_exact(self.stream, size))


class TokenBucket:
    def __init__(self, *, rate: float, burst: int, clock: Callable[[], float] = time.monotonic):
        if rate <= 0 or burst <= 0:
            raise GitRPCError("token bucket bounds must be positive")
        self.rate = float(rate)
        self.burst = int(burst)
        self.clock = clock
        self.tokens = float(burst)
        self.updated = clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def validate_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise GitRPCError("path must be a non-empty string")
    if unicodedata.normalize("NFC", path) != path:
        raise GitRPCError("path must be NFC-normalized")
    if "\\" in path or path.startswith("/") or "//" in path:
        raise GitRPCError("path has a forbidden separator")
    encoded = path.encode("utf-8", "strict")
    if len(encoded) > GIT_RPC_CONSTANTS["max_path_bytes"]:
        raise GitRPCError("path exceeds max_path_bytes")
    components = path.split("/")
    if len(components) > GIT_RPC_CONSTANTS["max_components_per_path"]:
        raise GitRPCError("path exceeds component count")
    for component in components:
        component_bytes = component.encode("utf-8")
        if not component or component in {".", ".."} or len(component_bytes) > GIT_RPC_CONSTANTS["max_component_bytes"]:
            raise GitRPCError("path contains an invalid component")
        if any(ord(char) < 32 or ord(char) == 127 for char in component):
            raise GitRPCError("path contains a control character")
    return path


def _validate_paths(paths: Any) -> tuple[str, ...]:
    if not isinstance(paths, list) or len(paths) > GIT_RPC_CONSTANTS["max_paths_per_request"]:
        raise GitRPCError("paths exceed max_paths_per_request")
    return tuple(validate_path(path) for path in paths)


@dataclass(frozen=True)
class StatusEntry:
    kind: str
    path: str
    original_path: str | None = None


@dataclass(frozen=True)
class PorcelainStatus:
    entries: tuple[StatusEntry, ...]

    @property
    def changed_paths(self) -> tuple[str, ...]:
        values: list[str] = []
        for entry in self.entries:
            values.append(entry.path)
            if entry.original_path is not None:
                values.append(entry.original_path)
        return tuple(values)

    @property
    def dirty(self) -> bool:
        return bool(self.entries)


def _path_from_bytes(value: bytes) -> str:
    try:
        return validate_path(value.decode("utf-8", "strict"))
    except (UnicodeDecodeError, GitRPCError) as exc:
        raise GitRPCError("porcelain path is not a canonical UTF-8 path") from exc


def parse_porcelain_v2(raw: bytes) -> PorcelainStatus:
    data = bytes(raw)
    if not data:
        return PorcelainStatus(())
    if not data.endswith(b"\0"):
        raise GitRPCError("porcelain-v2 response is not NUL terminated")
    fields = data[:-1].split(b"\0")
    entries: list[StatusEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            raise GitRPCError("empty porcelain-v2 record")
        kind = chr(record[0])
        if kind == "?" or kind == "!":
            if len(record) < 3 or record[1:2] != b" ":
                raise GitRPCError("malformed porcelain-v2 untracked record")
            entries.append(StatusEntry(kind, _path_from_bytes(record[2:])))
            continue
        if kind in {"1", "u"}:
            parts = record.split(b" ", 8 if kind == "1" else 10)
            expected = 9 if kind == "1" else 11
            if len(parts) != expected:
                raise GitRPCError("malformed porcelain-v2 ordinary record")
            entries.append(StatusEntry(kind, _path_from_bytes(parts[-1])))
            continue
        if kind == "2":
            parts = record.split(b" ", 9)
            if len(parts) != 10 or index >= len(fields):
                raise GitRPCError("malformed porcelain-v2 rename record")
            path = _path_from_bytes(parts[-1])
            original = _path_from_bytes(fields[index])
            index += 1
            entries.append(StatusEntry(kind, path, original))
            continue
        raise GitRPCError("unknown porcelain-v2 record")
    return PorcelainStatus(tuple(entries))


def _allowlisted(path: str, allowlist: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in allowlist)


def _fixed_environment(git_dir: Path, worktree: Path) -> dict[str, str]:
    git = shutil.which("git") or "/usr/bin/git"
    return {
        "PATH": str(Path(git).parent),
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(worktree),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "false",
        "HOME": str(worktree),
        "LC_ALL": "C",
        "GIT_CONFIG_COUNT": "5",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": os.devnull,
        "GIT_CONFIG_KEY_2": "core.attributesfile",
        "GIT_CONFIG_VALUE_2": os.devnull,
        "GIT_CONFIG_KEY_3": "core.excludesfile",
        "GIT_CONFIG_VALUE_3": os.devnull,
        "GIT_CONFIG_KEY_4": "core.sshCommand",
        "GIT_CONFIG_VALUE_4": "false",
    }


class GitService:
    CLOSED_OPERATIONS = frozenset({"status", "add", "hash", "stage", "tree", "commit"})

    def __init__(
        self,
        repo: str | Path,
        *,
        fixture_root_oid: str,
        allowed_paths: tuple[str, ...] | list[str],
        git_dir: str | Path | None = None,
        worktree: str | Path | None = None,
        completion_provider: Callable[[], Mapping[str, Any]] | None = None,
        receipt_chain: Any = None,
        scored: bool = False,
        tool_gid: int | None = None,
        receipt_authorizer: Callable[[Mapping[str, Any]], None] | None = None,
        infrastructure_authorizer: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        self.repo = Path(repo)
        self.worktree = Path(worktree) if worktree is not None else self.repo
        self.git_dir = Path(git_dir) if git_dir is not None else self.repo / ".git"
        if not self.repo.is_absolute() or not self.worktree.is_absolute() or not self.git_dir.is_absolute():
            raise GitRPCError("Git service paths must be absolute")
        if not self.worktree.is_dir() or self.worktree.is_symlink():
            raise GitRPCError("worktree must be a real directory")
        if not isinstance(fixture_root_oid, str) or len(fixture_root_oid) != 40:
            raise GitRPCError("fixture root must be a Git OID")
        self.fixture_root_oid = fixture_root_oid
        self.allowed_paths = tuple(allowed_paths)
        if completion_provider is not None and not callable(completion_provider):
            raise GitRPCError("completion provider must be callable")
        self._completion_provider = completion_provider
        self._infrastructure_failure: str | None = None
        self.receipt_chain = receipt_chain
        if tool_gid is not None and (
            isinstance(tool_gid, bool) or not isinstance(tool_gid, int) or tool_gid <= 0
        ):
            raise GitRPCError("tool-plane GID is invalid")
        self.tool_gid = tool_gid
        self._receipt_authorizer = receipt_authorizer
        self._infrastructure_authorizer = infrastructure_authorizer
        if scored and (receipt_chain is None or completion_provider is None):
            raise GitRPCError("scored Git service requires receipt chain and completion provider")
        if scored and tool_gid is None:
            raise GitRPCError("scored Git service requires the provisioned tool-plane GID")
        self._status_bucket = TokenBucket(rate=GIT_RPC_CONSTANTS["status_rate_per_second"], burst=GIT_RPC_CONSTANTS["status_burst"])
        self._in_flight = threading.BoundedSemaphore(GIT_RPC_CONSTANTS["max_in_flight"])

    @classmethod
    def is_closed_operation(cls, operation: str) -> bool:
        return operation in cls.CLOSED_OPERATIONS

    @classmethod
    def authorize_request(cls, request: Mapping[str, Any], *, actor: str = "tool") -> str:
        if not isinstance(request, Mapping) or set(request) - {"op", "paths", "message", "argv"}:
            raise GitRPCError("RPC request fields are closed")
        operation = request.get("op")
        if not isinstance(operation, str) or operation not in cls.CLOSED_OPERATIONS:
            raise GitRPCError("RPC operation is not allowlisted")
        argv = request.get("argv", [])
        if not isinstance(argv, list) or any(isinstance(item, str) and item.startswith("-") for item in argv):
            raise GitRPCError("Git options are forbidden")
        if actor in {"tool", "ingress"} and operation not in {"status", "add", "commit"}:
            raise GitRPCError("actor cannot call internal Git operation")
        if operation == "add":
            if set(request) - {"op", "paths"}:
                raise GitRPCError("add request fields are closed")
            _validate_paths(request.get("paths"))
        elif operation == "commit":
            if set(request) - {"op", "message"} or not isinstance(request.get("message"), str) or not request["message"]:
                raise GitRPCError("commit request is invalid")
        elif operation == "status" and set(request) != {"op"}:
            raise GitRPCError("status request fields are closed")
        elif operation in {"hash", "stage", "tree"} and set(request) != {"op"}:
            raise GitRPCError("internal RPC request fields are closed")
        return operation

    @classmethod
    def authorize_budget_candidate(cls, candidate: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, Mapping) or candidate.get("operation") not in {"status", "hash", "stage", "tree", "commit"}:
            raise GitRPCError("budget candidate operation is not service-owned")
        required = {"operation", "reason", "budget_dimension", "limit", "observed"}
        if set(candidate) != required or candidate["reason"] != "MODEL_BUDGET_EXCEEDED":
            raise GitRPCError("budget candidate fields are invalid")
        if not all(isinstance(candidate[name], int) and not isinstance(candidate[name], bool) and candidate[name] >= 0 for name in ("limit", "observed")) or candidate["limit"] == 0:
            raise GitRPCError("budget candidate bounds are invalid")
        return dict(candidate)

    def _run(self, *args: str, input_data: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "--work-tree", str(self.worktree), *args],
            input=input_data,
            capture_output=True,
            check=False,
            env=_fixed_environment(self.git_dir, self.worktree),
        )
        if result.returncode:
            raise GitRPCError(result.stderr.decode("utf-8", "replace").strip() or "Git plumbing failed")
        return bytes(result.stdout)

    def _open_regular(self, path: str) -> tuple[int, os.stat_result]:
        components = validate_path(path).split("/")
        root_fd = os.open(self.worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent_fd = root_fd
        try:
            for component in components[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                if parent_fd != root_fd:
                    os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                raise GitRPCError("staging only accepts regular files")
            if info.st_nlink != 1:
                os.close(fd)
                raise GitRPCError("hardlink staging is forbidden")
            return fd, info
        except OSError as exc:
            raise GitRPCError(f"cannot open path without following links: {path}") from exc
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)
            os.close(root_fd)

    def _stage_paths(self, paths: tuple[str, ...]) -> list[str]:
        """Stage every path with two Git invocations total, not two per path.

        A maximum-size add is 1,024 paths; two subprocesses per path cannot
        complete inside the tool-plane RPC timeout, so hashing is batched into
        one ``hash-object --stdin-paths`` and indexing into one ``update-index
        -z --index-info`` fed over stdin — per-path argv expansion would exceed
        ``ARG_MAX`` for a legal request of 1,024 near-limit paths.  Git must
        never reopen the attacker-controlled worktree pathnames: each file is
        streamed once through the no-follow descriptor into a private 0600
        copy (looping until every byte of every chunk is written, since a
        POSIX ``write`` may be short), and Git hashes only those copies.  Each
        returned oid is checked against the digest computed from the same
        guarded stream, so a mid-staging file swap still fails and no outside
        bytes enter the store.
        """
        guarded: dict[str, tuple[str, str, str]] = {}  # path -> (blob digest, index mode, private copy)
        with tempfile.TemporaryDirectory(prefix="implbench-stage-") as stage_dir:
            for index, path in enumerate(dict.fromkeys(paths)):
                if not _allowlisted(path, self.allowed_paths):
                    raise GitRPCError("path is outside the immutable task allowlist")
                private = os.path.join(stage_dir, f"{index:06d}.blob")
                fd, before = self._open_regular(path)
                try:
                    digest = hashlib.sha1(b"blob %d\0" % before.st_size)
                    copy_fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        total = 0
                        while True:
                            chunk = os.read(fd, 131072)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > 16 * 1024 * 1024:
                                raise GitBudgetExceeded("stage_bytes limit exceeded")
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(copy_fd, view):]
                        os.fsync(copy_fd)
                    finally:
                        os.close(copy_fd)
                    after = os.fstat(fd)
                    if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
                        raise GitRPCError("file changed while staging")
                    mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
                    guarded[path] = (digest.hexdigest(), mode, private)
                finally:
                    os.close(fd)
            if not guarded:
                return []
            unique_paths = list(guarded)
            digests = list(dict.fromkeys(row[0] for row in guarded.values()))
            private_for = {row[0]: row[2] for row in guarded.values()}
            listed = self._run(
                "hash-object", "-w", "--stdin-paths",
                input_data=("\n".join(private_for[digest] for digest in digests) + "\n").encode("utf-8"),
            ).decode("utf-8").splitlines()
            if len(listed) != len(digests):
                raise GitRPCError("Git hashing did not cover every staged path")
            oids: dict[str, str] = {}
            for digest, oid in zip(digests, listed, strict=True):
                if oid != digest:
                    raise GitRPCError("file changed while staging")
                oids[digest] = oid
            self._run(
                "update-index", "-z", "--index-info",
                input_data=b"".join(
                    f"{guarded[path][1]} {oids[guarded[path][0]]}\t{path}\0".encode("utf-8")
                    for path in unique_paths
                ),
            )
            return [oids[guarded[path][0]] for path in paths]

    def _status(self) -> dict[str, Any]:
        if not self._status_bucket.take():
            raise GitBudgetExceeded("status rate limit exceeded")
        raw = self._run("status", "--porcelain=v2", "-z", "--untracked-files=all", "--no-renames")
        parsed = parse_porcelain_v2(raw)
        head_raw = self._run("rev-parse", "--verify", "HEAD").decode().strip()
        from .completion import materialization_digest

        return {
            "head": head_raw or None,
            "dirty": parsed.dirty,
            "final_tree_digest": materialization_digest(self.worktree),
            "final_tree_digest_version": "final-tree-v1",
        }

    def _handle(self, request: Mapping[str, Any], *, actor: str = "tool") -> dict[str, Any]:
        operation = self.authorize_request(request, actor=actor)
        if operation == "status":
            return self._status()
        if operation == "add":
            paths = _validate_paths(request["paths"])
            return {"paths": list(paths), "object_oids": self._stage_paths(paths)}
        if operation == "hash":
            if actor != "service":
                raise GitRPCError("hash is service-only")
            return {"operation": "hash", "tree_oid": self._run("write-tree").decode().strip()}
        if operation == "stage":
            if actor != "service":
                raise GitRPCError("stage is service-only")
            return {"operation": "stage", "tree_oid": self._run("write-tree").decode().strip()}
        if operation == "tree":
            if actor != "service":
                raise GitRPCError("tree is service-only")
            return {"tree_oid": self._run("write-tree").decode().strip()}
        message = request["message"].encode("utf-8")
        if b"\0" in message or len(message) > 64 * 1024:
            raise GitRPCError("commit message is invalid")
        tree_oid = self._run("write-tree").decode().strip()
        parent = self._run("rev-parse", "--verify", "HEAD").decode().strip()
        if not parent or parent != self.fixture_root_oid and not len(parent) == 40:
            raise GitRPCError("invalid first parent")
        env = _fixed_environment(self.git_dir, self.worktree)
        env.update({"GIT_AUTHOR_NAME": "implbench", "GIT_AUTHOR_EMAIL": "implbench@localhost", "GIT_COMMITTER_NAME": "implbench", "GIT_COMMITTER_EMAIL": "implbench@localhost", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"})
        result = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "--work-tree", str(self.worktree), "commit-tree", tree_oid, "-p", parent],
            input=message + b"\n",
            capture_output=True,
            check=False,
            env=env,
        )
        if result.returncode:
            raise GitRPCError(result.stderr.decode("utf-8", "replace").strip() or "commit failed")
        commit_oid = result.stdout.decode().strip()
        raw_paths = self._run(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames", parent, commit_oid
        )
        if raw_paths and not raw_paths.endswith(b"\0"):
            raise GitRPCError("commit changed-path response is not NUL terminated")
        paths = tuple(_path_from_bytes(path) for path in raw_paths.rstrip(b"\0").split(b"\0") if path)
        from .completion import materialization_digest

        tree_digest = materialization_digest(self.worktree)
        result = {"fixture_root_oid": self.fixture_root_oid, "ordered_parent_oids": [parent], "commit_oid": commit_oid, "tree_oid": tree_oid, "changed_paths": list(paths), "tree_digest": tree_digest, "head_oid": commit_oid, "dirty": False}
        if self._receipt_authorizer is not None:
            self._receipt_authorizer(result)
        elif self.receipt_chain is not None:
            from .receipts import make_git_receipt

            self.receipt_chain.append(
                make_git_receipt(
                    cell_id=self.receipt_chain.identity["cell_id"],
                    attempt_id=self.receipt_chain.identity["attempt_id"],
                    fixture_root_oid=self.fixture_root_oid,
                    ordered_parent_oids=[parent],
                    commit_oid=commit_oid,
                    tree_oid=tree_oid,
                    changed_paths=list(paths),
                    tree_digest=tree_digest,
                    head_oid=commit_oid,
                    dirty=False,
                    controller_sequence=None,
                )
            )
        # Receipt append/fsync is the commit's durable admission point.  Do not make the
        # candidate visible through HEAD until the authenticated receipt has succeeded.
        try:
            self._run("update-ref", "HEAD", commit_oid, parent)
        except Exception as exc:
            self._infrastructure_failure = "update-ref"
            if self.receipt_chain is not None:
                try:
                    self.receipt_chain.append_infrastructure_failure(
                        operation="update-ref",
                        reason="UPDATE_REF_FAILED",
                        parent_oid=parent,
                        commit_oid=commit_oid,
                    )
                except Exception as compensation_exc:
                    self._infrastructure_failure = "update-ref-compensation"
                    raise GitRPCError("Git HEAD update failed and compensation was not durable") from compensation_exc
            elif self._infrastructure_authorizer is not None:
                try:
                    self._infrastructure_authorizer({
                        "operation": "update-ref", "reason": "UPDATE_REF_FAILED",
                        "parent_oid": parent, "commit_oid": commit_oid,
                    })
                except Exception as compensation_exc:
                    self._infrastructure_failure = "update-ref-compensation"
                    raise GitRPCError("Git HEAD update failed and compensation was not durable") from compensation_exc
            raise GitRPCError("Git HEAD update failed after receipt admission") from exc
        return result

    def handle(self, request: Mapping[str, Any], *, actor: str = "tool") -> dict[str, Any]:
        if not self._in_flight.acquire(blocking=False):
            raise GitBudgetExceeded("max_in_flight exceeded")
        try:
            return self._handle(request, actor=actor)
        finally:
            self._in_flight.release()

    def completion_projection(self) -> dict[str, Any]:
        if self._completion_provider is None:
            raise GitRPCError("completion projection is not bound")
        value = self._completion_provider()
        if not isinstance(value, Mapping):
            raise GitRPCError("completion projection must be an object")
        projected = dict(value)
        failure = self._infrastructure_failure
        if failure is None and self.receipt_chain is not None:
            rows = getattr(self.receipt_chain, "_rows", lambda: ())()
            if any(row.get("record_type") == "infrastructure-failure" for row in rows if isinstance(row, Mapping)):
                failure = "update-ref"
        if failure is not None:
            projected["infrastructure_failure"] = failure
        return projected


class ChildAttemptGitServiceServer:
    """Production Git listener hosted in a dedicated child process.

    The child owns the tool-facing socket and Git operations.  Candidate receipts
    cross a small authenticated control socket; the controller appends/fsyncs them
    before it sends the one-byte commit acknowledgement back to the child.
    """
    def __init__(self, service: GitService, *, root: str | Path, attempt_id: str, tool_gid: int,
                 peer_uids: tuple[int, ...], launch_spec: LaunchSpec, receipt_chain: Any,
                 allow_unprofiled_test: bool = False,
                 child_spawner: Callable[[LaunchSpec, tuple[int, ...]], subprocess.Popen[bytes]] | None = None,
                 census_uid: Callable[[int], set[int]] | None = None,
                 structural_identity: bool = False):
        self.service, self.root, self.attempt_id = service, Path(root), attempt_id
        self.tool_gid, self.peer_uids, self.launch_spec, self.receipt_chain = tool_gid, peer_uids, launch_spec, receipt_chain
        self.allow_unprofiled_test, self.child_spawner, self.census_uid = allow_unprofiled_test, child_spawner, census_uid
        self.structural_identity = structural_identity
        endpoint = launch_spec.env.get("GIT_SERVICE_SOCKET")
        if not isinstance(endpoint, str) or not os.path.isabs(endpoint):
            raise GitRPCError("child Git service endpoint is not launch-bound")
        self.endpoint = Path(endpoint)
        self.capability = os.urandom(32).hex()
        self._process: subprocess.Popen[bytes] | None = None
        self._control: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self.launch_evidence: dict[str, Any] | None = None

    def start(self) -> dict[str, str]:
        if (self.launch_spec.plane != "git-service" or self.endpoint.exists()
                or self.endpoint.is_symlink() or not self.root.is_absolute()):
            raise GitRPCError("child Git service launch binding is invalid")
        self.endpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent, child = socket.socketpair()
        request_read, request_write = os.pipe()
        try:
            identity = getattr(self.receipt_chain, "identity", None)
            if not isinstance(identity, Mapping):
                raise GitRPCError("controller receipt identity is unavailable")
            run_id, cell_id = identity.get("run_id"), identity.get("cell_id")
            if (not isinstance(run_id, str) or not isinstance(cell_id, str)
                    or identity.get("attempt_id") != self.attempt_id):
                raise GitRPCError("controller receipt identity is not attempt-bound")
            nonce = os.urandom(32).hex()
            request = {
                "version": "implbench-git-child-v1", "run_id": run_id, "cell_id": cell_id,
                "attempt_id": self.attempt_id, "root": str(self.root), "nonce": nonce,
                "repo": str(self.service.repo), "git_dir": str(self.service.git_dir),
                "worktree": str(self.service.worktree), "fixture_root_oid": self.service.fixture_root_oid,
                "allowed_paths": list(self.service.allowed_paths), "endpoint": str(self.endpoint),
                "capability": self.capability, "tool_gid": self.tool_gid, "peer_uids": list(self.peer_uids),
                "effective_tool_gid": os.getgid() if self.structural_identity else self.tool_gid,
                "control_fd": child.fileno(), "profile_digest": self.launch_spec.profile_digest,
                "template_digest": self.launch_spec.template_digest, "expected_uid": self.launch_spec.uid,
                "expected_gid": self.launch_spec.gid, "structural_identity": self.structural_identity,
            }
            raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > 65536:
                raise GitRPCError("child Git service request exceeds its bound")
            spec = LaunchSpec(**{**self.launch_spec.__dict__, "argv":(sys.executable,"-u","-m","implbench.harness.git_service_child","--request-fd",str(request_read)),
                                 "inherited_fds":tuple(sorted(set((*self.launch_spec.inherited_fds, request_read, child.fileno()))) )})
            if self.child_spawner is None:
                self._process = spawn_child(spec, pass_fds=spec.inherited_fds, allow_unprofiled_test=self.allow_unprofiled_test)
            else:
                if self.allow_unprofiled_test:
                    raise GitRPCError("production child spawner cannot use the test seam")
                self._process = self.child_spawner(spec, spec.inherited_fds)
            child.close(); os.close(request_read); request_read = -1
            os.write(request_write, raw); os.close(request_write); request_write = -1
            ready = _read_startup_line(self._process, limit=65536, timeout=10)
            if not ready:
                raise GitRPCError("child Git service failed before readiness")
            value = json.loads(ready)
            expected = {
                "version": "implbench-git-child-v1", "ok": True, "pid": self._process.pid,
                "uid": os.getuid() if self.structural_identity else self.launch_spec.uid,
                "gid": os.getgid() if self.structural_identity else self.launch_spec.gid, "run_id": run_id,
                "cell_id": cell_id, "attempt_id": self.attempt_id, "root": str(self.root), "nonce": nonce,
                "repo": str(self.service.repo), "git_dir": str(self.service.git_dir),
                "worktree": str(self.service.worktree), "endpoint": str(self.endpoint),
                "profile_digest": self.launch_spec.profile_digest,
                "template_digest": self.launch_spec.template_digest,
            }
            if value != expected or value["pid"] == os.getpid():
                raise GitRPCError("child Git service evidence mismatch")
            if (not self.structural_identity and self.census_uid is not None
                    and self._process.pid not in self.census_uid(self.launch_spec.uid)):
                raise GitRPCError("independent child Git service census mismatch")
            self.launch_evidence = dict(value)
            self._control = parent
            self._thread = threading.Thread(target=self._receipt_loop, daemon=True); self._thread.start()
            return {"endpoint":str(self.endpoint), "capability":self.capability}
        except Exception:
            parent.close(); child.close()
            if self._process is not None and self._process.poll() is None:
                self._process.kill(); self._process.wait()
            self.endpoint.unlink(missing_ok=True)
            raise
        finally:
            if request_read >= 0: os.close(request_read)
            if request_write >= 0: os.close(request_write)

    def _receipt_loop(self) -> None:
        assert self._control is not None
        stream = self._control.makefile("rb")

        def acknowledge(value: bytes) -> None:
            # The child can exit after submitting its candidate but before the
            # controller's durable append returns.  A broken acknowledgement pipe
            # is not a controller crash and must not strand this receipt thread.
            try:
                self._control.sendall(value)
            except OSError:
                return

        try:
            while True:
                try:
                    message = FrameReader(stream).read()
                except GitRPCError:
                    return
                try:
                    if set(message) != {"kind", "payload"} or not isinstance(message.get("kind"), str) or not isinstance(message.get("payload"), dict):
                        raise GitRPCError("child controller message is not closed")
                    kind, candidate = message["kind"], message["payload"]
                    required = {
                        "cell_id", "attempt_id", "fixture_root_oid", "ordered_parent_oids", "commit_oid",
                        "tree_oid", "changed_paths", "tree_digest", "tree_digest_version", "head_oid",
                        "dirty", "controller_sequence", "nonce",
                    }
                    if kind == "git-receipt":
                        if set(candidate) != required or candidate.get("controller_sequence") is not None:
                            raise GitRPCError("candidate receipt is not closed")
                        # ReceiptChain validates the exact cell/attempt/path/parent binding,
                        # then assigns sequence + nonce and fsyncs before this acknowledgement.
                        self.receipt_chain.append(candidate)
                    elif kind == "infrastructure-failure":
                        if set(candidate) != {"operation", "reason", "parent_oid", "commit_oid"}:
                            raise GitRPCError("infrastructure candidate is not closed")
                        self.receipt_chain.append_infrastructure_failure(**candidate)
                        self.service._infrastructure_failure = str(candidate["operation"])
                    else:
                        raise GitRPCError("child controller message kind is invalid")
                    acknowledge(b"1")
                except Exception:
                    acknowledge(b"0")
        finally:
            stream.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try: self._process.wait(timeout=2)
            except subprocess.TimeoutExpired: self._process.kill(); self._process.wait()
        if self._control is not None:
            self._control.close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.endpoint.unlink(missing_ok=True)
        if self._process is not None and self._process.poll() is None:
            raise GitRPCError("child Git service was not reaped")
        if self._process is not None:
            if self._process.stdout is not None:
                self._process.stdout.close()
            if self._process.stderr is not None:
                self._process.stderr.close()
        if not self.allow_unprofiled_test and self.census_uid is not None and self.census_uid(self.launch_spec.uid):
            raise GitRPCError("child Git service UID is not empty after close")

    def status(self) -> dict[str, Any]:
        """Read final status through the child while its Git authority is live."""

        if self._closed:
            raise GitRPCError("child Git service is already closed")
        return RemoteGitService(
            endpoint=str(self.endpoint), capability=self.capability, tool_gid=self.tool_gid,
        ).handle({"op": "status"})


def _read_startup_line(process: subprocess.Popen[bytes], *, limit: int, timeout: float) -> bytes:
    """Read one bounded readiness line without trusting a child-controlled stream."""

    if process.stdout is None:
        raise GitRPCError("child Git service stdout is unavailable")
    deadline = time.monotonic() + timeout
    output = bytearray()
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, min(4096, limit + 1 - len(output)))
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit or b"\n" not in output:
            if len(output) > limit:
                raise GitRPCError("child Git service readiness exceeds its bound")
            continue
        line, extra = bytes(output).split(b"\n", 1)
        if extra:
            raise GitRPCError("child Git service readiness has trailing data")
        return line
    raise GitRPCError("child Git service readiness timed out")
