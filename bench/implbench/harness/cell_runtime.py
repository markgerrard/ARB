"""Journaled disposable-cell, ACL, and process lifecycle primitives.

The runtime owns lifecycle bookkeeping only.  It deliberately does not launch engines, import
Git objects, or score a cell; those boundaries belong to later bench tasks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import signal
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


CANONICAL_CELL_ROOT = Path("/Users/Shared/arb-implbench")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CELL_ID = re.compile(r"^cell-[0-9a-f]{64}$")


class CellRuntimeError(RuntimeError):
    """Raised when a cell lifecycle invariant cannot be proved."""


class ACLNotProvisioned(CellRuntimeError):
    """Raised after cleanup when the intended ACL identity was never provisioned."""


class ACLNotFound(CellRuntimeError):
    """Authenticated backend response that a cleanup target is already absent."""


class ProcessLifecycleError(CellRuntimeError):
    """Raised when an independent process census is not empty."""


class ProvisioningError(CellRuntimeError):
    """Raised when controller-owned plane provisioning cannot be proved."""


def cell_id_for(pair: str, arm: str, task: str, repetition: int, schedule_index: int) -> str:
    """Derive the immutable cell identity from all schedule identity inputs."""

    values = (pair, arm, task, str(repetition), str(schedule_index))
    if not all(isinstance(value, str) and value for value in values[:3]):
        raise ValueError("pair, arm, and task must be non-empty strings")
    if not isinstance(repetition, int) or isinstance(repetition, bool) or repetition < 0:
        raise ValueError("repetition must be a non-negative integer")
    if not isinstance(schedule_index, int) or isinstance(schedule_index, bool) or schedule_index < 0:
        raise ValueError("schedule_index must be a non-negative integer")
    payload = "\x00".join(values).encode("utf-8")
    return "cell-" + hashlib.sha256(payload).hexdigest()


def attempt_id_for(cell_id: str, attempt_number: int) -> str:
    """Derive the canonical immutable attempt identity for one cell execution."""

    if not _CELL_ID.fullmatch(cell_id):
        raise ValueError("attempt identity requires a canonical cell identity")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or attempt_number < 1:
        raise ValueError("attempt number must be a positive integer")
    return "attempt-" + hashlib.sha256(f"{cell_id}\x00{attempt_number}".encode("ascii")).hexdigest()


def _validate_identity(run_id: str, cell_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id contains an invalid path component")
    if not isinstance(cell_id, str) or not _CELL_ID.fullmatch(cell_id):
        raise ValueError("cell_id must be a cell- prefixed SHA-256 identity")


@dataclass(frozen=True)
class CellPaths:
    """All paths owned by one cell, rooted under the canonical run directory."""

    run_root: Path
    cell_root: Path
    run_id: str
    cell_id: str
    control_home: Path
    tool_home: Path
    git_home: Path
    config_root: Path
    bus_namespace: Path
    runtime: Path

    @classmethod
    def for_run(cls, run_id: str, cell_id: str, *, root: Path = CANONICAL_CELL_ROOT) -> "CellPaths":
        _validate_identity(run_id, cell_id)
        root = Path(root)
        if not root.is_absolute():
            raise ValueError("cell root must be absolute")
        try:
            canonical_root = root.resolve(strict=False)
        except OSError as exc:
            raise ValueError("cell root cannot be canonicalized") from exc
        mac_var_alias = root == Path("/var") or (root.is_relative_to(Path("/var")) and not root.is_symlink())
        if canonical_root != root and not mac_var_alias:
            raise ValueError("cell root must be canonical")
        root = canonical_root
        run_root = root / run_id
        cell_root = run_root / cell_id
        return cls(
            run_root=run_root,
            cell_root=cell_root,
            run_id=run_id,
            cell_id=cell_id,
            control_home=cell_root / "homes" / "control",
            tool_home=cell_root / "homes" / "tool",
            git_home=cell_root / "homes" / "git",
            config_root=cell_root / "configs",
            bus_namespace=cell_root / "bus",
            runtime=cell_root / "runtime",
        )

    @classmethod
    def for_attempt(cls, run_id: str, cell_id: str, attempt_id: str, *, root: Path = CANONICAL_CELL_ROOT) -> "CellPaths":
        """Return a disposable root that cannot be reused by another attempt."""

        if not isinstance(attempt_id, str) or not re.fullmatch(r"attempt-[0-9a-f]{64}", attempt_id):
            raise ValueError("attempt_id must be canonical")
        base = cls.for_run(run_id, cell_id, root=root)
        cell_root = base.cell_root / "attempts" / attempt_id
        return cls(
            run_root=base.run_root,
            cell_root=cell_root,
            run_id=run_id,
            cell_id=cell_id,
            control_home=cell_root / "homes" / "control",
            tool_home=cell_root / "homes" / "tool",
            git_home=cell_root / "homes" / "git",
            config_root=cell_root / "configs",
            bus_namespace=cell_root / "bus",
            runtime=cell_root / "runtime",
        )

    @property
    def managed_paths(self) -> tuple[Path, ...]:
        return (
            self.cell_root,
            self.control_home,
            self.tool_home,
            self.git_home,
            self.config_root,
            self.bus_namespace,
            self.runtime,
        )

    def agent_id(self, arm_prefix: str) -> str:
        if not isinstance(arm_prefix, str) or not arm_prefix:
            raise ValueError("arm prefix must be non-empty")
        digest = hashlib.sha256(self.cell_id.encode("ascii")).digest()
        suffix = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return f"{arm_prefix}-{suffix}"


class IdentityAllocator(ABC):
    """Controller-side source of fresh non-root plane UIDs."""

    @abstractmethod
    def mint(self, role: str) -> int:
        raise NotImplementedError

    def mint_gid(self, role: str) -> int:
        """Mint the group identity for a provisioned plane."""

        return self.mint(f"{role}-gid")


@dataclass(frozen=True)
class PlaneIdentities:
    control: int
    tool: int
    git: int
    tool_gid: int | None = None

    @classmethod
    def mint(cls, allocator: IdentityAllocator) -> "PlaneIdentities":
        values = tuple(allocator.mint(role) for role in ("control", "tool", "git"))
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
            raise CellRuntimeError("plane UID must be a positive integer")
        if len(set(values)) != 3:
            raise CellRuntimeError("plane UIDs must be distinct")
        tool_gid = allocator.mint_gid("tool")
        if not isinstance(tool_gid, int) or isinstance(tool_gid, bool) or tool_gid <= 0:
            raise CellRuntimeError("tool-plane GID must be a positive integer")
        return cls(*values, tool_gid=tool_gid)

    def __iter__(self):
        return iter((self.control, self.tool, self.git))


class ACLBackend(Protocol):
    def provision(self, identity: "ACLIdentity") -> None: ...
    def namespace_keys(self, prefix: str) -> set[str]: ...
    def cross_prefix_probe(self, user: str, prefix: str) -> bool: ...
    def disable_user(self, user: str) -> None: ...
    def kill_clients(self, user: str) -> None: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def delete_user(self, user: str) -> None: ...
    def authenticate(self, user: str, password: str) -> bool: ...


class PlaneProvisioner(Protocol):
    """Controller-owned OS/daemon boundary for a scored cell.

    Implementations must create real identities and start the per-cell seat daemon.  The
    lifecycle deliberately accepts this narrow interface instead of treating numeric labels,
    paths, or a registry heartbeat as provisioning evidence.
    """

    real: bool

    def reserve_identities(self, cell_id: str, *, attempt_id: str) -> "PlaneIdentities": ...
    def provision_planes(self, paths: "CellPaths", identities: "PlaneIdentities", *, attempt_id: str) -> None: ...
    def start_seat_daemon(self, paths: "CellPaths", identities: "PlaneIdentities", *, attempt_id: str) -> None: ...
    def stop_seat_daemon(self, paths: "CellPaths", identities: "PlaneIdentities", *, attempt_id: str) -> None: ...
    def prove_absent(self, paths: "CellPaths", identities: "PlaneIdentities", *, attempt_id: str) -> bool: ...


@dataclass(frozen=True)
class ACLIdentity:
    user: str
    prefix: str
    password: str

    @classmethod
    def create(cls, cell_id: str, *, token: str | None = None) -> "ACLIdentity":
        if not _CELL_ID.fullmatch(cell_id):
            raise ValueError("ACL identity requires a cell identity")
        nonce = secrets.token_hex(12)
        digest = hashlib.sha256(f"{cell_id}:{nonce}".encode()).hexdigest()[:24]
        return cls(
            user=f"implbench-cell-{digest}",
            prefix=f"implbench:{digest}",
            password=token if token is not None else secrets.token_urlsafe(32),
        )


class ACLLifecycle:
    """Redis/Valkey ACL lifecycle with namespace proofs independent of ping reachability."""

    def __init__(self, backend: ACLBackend):
        self.backend = backend
        self._provisioned: set[str] = set()

    def provision(self, identity: ACLIdentity) -> None:
        self.backend.provision(identity)
        self._provisioned.add(identity.user)

    def pre_empty(self, identity: ACLIdentity) -> bool:
        return self.backend.namespace_keys(identity.prefix) == set()

    def cross_prefix_denied(self, identity: ACLIdentity) -> bool:
        forbidden = f"{identity.prefix}-forbidden-{secrets.token_hex(8)}"
        denied = self.backend.cross_prefix_probe(identity.user, identity.prefix, forbidden)
        return denied is False

    def endpoint_reachable(self) -> bool:
        ping = getattr(self.backend, "ping", None)
        return bool(ping()) if callable(ping) else False

    def close(self, identity: ACLIdentity) -> None:
        # These probes intentionally run even when SETUSER never completed.  Cleanup is based on
        # the controller-minted intended identity, not on an optimistic provisioning flag.
        for operation, value in (
            (self.backend.disable_user, identity.user),
            (self.backend.kill_clients, identity.user),
            (self.backend.delete_prefix, identity.prefix),
            (self.backend.delete_user, identity.user),
        ):
            try:
                operation(value)
            except ACLNotFound:
                continue
        retired_auth = self.backend.authenticate(identity.user, identity.password)
        remaining = self.backend.namespace_keys(identity.prefix)
        if retired_auth or remaining:
            raise CellRuntimeError("retired ACL identity or namespace remains")
        if identity.user not in self._provisioned:
            raise ACLNotProvisioned("ACL_NOT_PROVISIONED")


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    uid: int
    pgid: int
    session_id: int
    role: str


class ProcessTable(Protocol):
    def census_uid(self, uid: int) -> set[int]: ...
    def signal(self, pid: int, sig: int) -> None: ...


class ProcessLedger:
    """Controller ledger plus an independent UID census for every process exit."""

    def __init__(self, table: ProcessTable):
        self.table = table
        self.records: dict[int, ProcessRecord] = {}
        self.absence_proof: dict[int, bool] = {}

    def register(self, record: ProcessRecord) -> None:
        if record.pid <= 0 or record.uid <= 0 or record.pgid <= 0 or record.session_id <= 0:
            raise ProcessLifecycleError("process identity is invalid")
        if record.pid in self.records:
            raise ProcessLifecycleError("process identity was reused")
        self.records[record.pid] = record

    def close(self, uids: Iterable[int], *, grace_s: float) -> None:
        if grace_s < 0:
            raise ValueError("grace_s must be non-negative")
        unique_uids = tuple(dict.fromkeys(uids))
        if any(not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0 for uid in unique_uids):
            raise ProcessLifecycleError("process census UID is invalid")

        # The independent census, not just the ledger, finds double-fork/setsid descendants.
        pending: dict[int, set[int]] = {uid: self.table.census_uid(uid) for uid in unique_uids}
        for pids in pending.values():
            for pid in sorted(pids):
                self.table.signal(pid, signal.SIGTERM)

        wait = getattr(self.table, "wait_uid_empty", None)
        if callable(wait):
            for uid in unique_uids:
                wait(uid, grace_s)
        # Without a supervisor wait hook, the second census is immediate and deterministic while
        # still preserving TERM-before-KILL in hermetic tests.
        pending = {uid: self.table.census_uid(uid) for uid in unique_uids}
        for pids in pending.values():
            for pid in sorted(pids):
                self.table.signal(pid, signal.SIGKILL)
        pending = {uid: self.table.census_uid(uid) for uid in unique_uids}
        self.absence_proof = {uid: not pids for uid, pids in pending.items()}
        retained = {uid: sorted(pids) for uid, pids in pending.items() if pids}
        if retained:
            raise ProcessLifecycleError(f"process census not empty: {retained}")


class Journal:
    """Append-only write-ahead journal; every record is fsynced before the effect proceeds."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, action: str, status: str, **details: Any) -> None:
        record = {"action": action, "status": status, "details": details}
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]


class _NoopAllocator(IdentityAllocator):
    def __init__(self) -> None:
        self._next = os.getuid() + 1000

    def mint(self, role: str) -> int:
        self._next += 1
        return self._next


class _NoopProcesses:
    def close(self, identities: Iterable[int], *, grace_s: float) -> None:
        return None


class CellRuntime:
    """The canonical disposable-cell state machine used by scored cells and preflight."""

    def __init__(
        self,
        paths: CellPaths,
        *,
        allocator: IdentityAllocator | None = None,
        acl: ACLLifecycle | ACLBackend | None = None,
        processes: Any | None = None,
        provisioner: PlaneProvisioner | None = None,
        attempt_id: str | None = None,
        require_provisioner: bool = False,
        fault_before: int | None = None,
        fault_after: int | None = None,
    ) -> None:
        self.paths = paths
        self.allocator = allocator or _NoopAllocator()
        if isinstance(acl, ACLLifecycle) or acl is None or hasattr(acl, "close") and not hasattr(acl, "disable_user"):
            self.acl = acl
        else:
            self.acl = ACLLifecycle(acl)
        self.processes = processes or _NoopProcesses()
        self.provisioner = provisioner
        self.attempt_id = attempt_id
        self.require_provisioner = require_provisioner
        self.fault_before = fault_before
        self.fault_after = fault_after
        self._provision_count = 0
        self.state = "NEW"
        self.identities: PlaneIdentities | None = None
        self.acl_identity: ACLIdentity | None = None
        # Keep the journal outside the disposable cell so the committed destroy phase remains
        # durable after descriptor-safe deletion of the cell root.
        self.journal = Journal(paths.run_root / f"{paths.cell_id}.lifecycle.ndjson")

    @property
    def tool_gid(self) -> int:
        """Return the provisioned tool-plane group, never a process or default group."""

        if self.identities is None or self.identities.tool_gid is None:
            raise CellRuntimeError("tool-plane GID was not provisioned")
        return self.identities.tool_gid

    def allocate(self) -> None:
        if self.state != "NEW":
            raise CellRuntimeError("cell allocation is not fresh")
        if self.paths.cell_root.exists() or self.paths.cell_root.is_symlink():
            raise CellRuntimeError("cell root already exists")
        _assert_no_symlink_ancestors(self.paths.run_root)
        self.paths.run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_controller_owned(self.paths.run_root)
        self.paths.cell_root.mkdir(mode=0o700, parents=True)
        self.paths.cell_root.chmod(0o700)
        self._assert_controller_owned(self.paths.cell_root)
        self.journal.append("cell-root", "committed", path=str(self.paths.cell_root))
        self.state = "ALLOCATED"

    def _action(self, action: str, effect, **details: Any) -> None:
        self.journal.append(action, "prepared", **details)
        if self.fault_before == self._provision_count + 1:
            self.fault_before = None
            raise RuntimeError("injected pre-effect provisioning fault")
        effect()
        self.journal.append(action, "committed", **details)
        self._provision_count += 1
        if self.fault_after == self._provision_count:
            raise RuntimeError("injected provisioning fault")

    def provision(self) -> None:
        if self.state != "ALLOCATED":
            raise CellRuntimeError("cell must be allocated before provisioning")

        def mint_identities() -> None:
            self.identities = PlaneIdentities.mint(self.allocator)
            # Production persists the attempt-bound ACL identity before this state
            # transition.  Hermetic callers without that controller seam retain the
            # local mint, but never overwrite an authority already injected.
            if self.acl_identity is None:
                self.acl_identity = ACLIdentity.create(self.paths.cell_id)

        self._action("identities", mint_identities)
        if self.require_provisioner and self.provisioner is None:
            raise ProvisioningError("controller-owned plane provisioner is not bound")
        if self.provisioner is not None and (self.require_provisioner or getattr(self.provisioner, "real", False)):
            if not isinstance(self.attempt_id, str) or not self.attempt_id.startswith("attempt-"):
                raise ProvisioningError("plane provisioning requires an attempt identity")
            if not getattr(self.provisioner, "real", False):
                raise ProvisioningError("plane provisioner is not a real controller-owned backend")
            self._action(
                "plane-provisioning",
                lambda: self.provisioner.provision_planes(self.paths, self.identities, attempt_id=self.attempt_id),
                attempt_id=self.attempt_id,
            )
        self._action(
            "homes",
            lambda: [path.mkdir(mode=0o700, parents=True, exist_ok=False) for path in (self.paths.control_home, self.paths.tool_home, self.paths.git_home)],
        )
        self._action(
            "configs-and-bus",
            lambda: [path.mkdir(mode=0o700, parents=True, exist_ok=False) for path in (self.paths.config_root, self.paths.bus_namespace)],
        )
        if self.acl is not None and self.acl_identity is not None:
            self._action("acl", lambda: self.acl.provision(self.acl_identity))
        else:
            self._action("acl", lambda: None)
        self._action("runtime", lambda: (self.paths.runtime.mkdir(mode=0o700), (self.paths.runtime / "tmp").mkdir(mode=0o700)))
        self._action("ready", lambda: self._assert_controller_owned(self.paths.cell_root))
        self.state = "PREFLIGHTED"

    def mark_dispatched(self) -> None:
        if self.state != "PREFLIGHTED":
            raise CellRuntimeError("cell must be preflighted before dispatch")
        if self.provisioner is None or not getattr(self.provisioner, "real", False):
            raise ProvisioningError("controller-owned plane provisioner is not ready")
        if self.acl is None or self.acl_identity is None:
            raise ProvisioningError("controller-owned ACL backend is not ready")
        if self.identities is None or not isinstance(self.attempt_id, str):
            raise ProvisioningError("dispatch requires provisioned attempt identities")
        self.journal.append("dispatch", "prepared", attempt_id=self.attempt_id)
        self.provisioner.start_seat_daemon(self.paths, self.identities, attempt_id=self.attempt_id)
        self.journal.append("dispatch", "committed", attempt_id=self.attempt_id)
        self.state = "DISPATCHED"

    def _close_action(self, action: str, effect) -> None:
        if any(row.get("action") == action and row.get("status") == "committed" for row in self.journal.read()):
            return
        self.journal.append(action, "prepared")
        effect()
        self.journal.append(action, "committed")

    def close(self, *, grace_s: float = 1.0) -> None:
        if self.state == "DESTROYED":
            return
        if self.state == "NEW":
            raise CellRuntimeError("cannot close an unallocated cell")
        self.state = "CLOSING"
        if self.provisioner is not None and self.identities is not None and isinstance(self.attempt_id, str):
            self._close_action(
                "seat-daemon",
                lambda: self.provisioner.stop_seat_daemon(self.paths, self.identities, attempt_id=self.attempt_id),
            )
        if self.identities is not None:
            self._close_action("processes", lambda: self.processes.close(tuple(self.identities), grace_s=grace_s))
        if self.acl is not None and self.acl_identity is not None:
            self._close_action("acl-close", lambda: self._close_acl())
        self._close_action("cell-destroy", self.destroy)

    def _close_acl(self) -> None:
        try:
            self.acl.close(self.acl_identity)
        except ACLNotProvisioned:
            pass

    def recover(self) -> None:
        """Finish cleanup from any prepared/committed provisioning prefix after a crash."""

        if self.state == "DESTROYED":
            return
        self.state = "CLOSING"
        if self.provisioner is not None and self.identities is not None and isinstance(self.attempt_id, str):
            self._close_action(
                "seat-daemon-recovery",
                lambda: self.provisioner.stop_seat_daemon(self.paths, self.identities, attempt_id=self.attempt_id),
            )
        if self.identities is not None:
            self._close_action("processes-recovery", lambda: self.processes.close(tuple(self.identities), grace_s=0))
        if self.acl is not None and self.acl_identity is not None:
            self._close_action("acl-recovery", self._close_acl)
        self._close_action("cell-destroy-recovery", self.destroy)

    def destroy(self) -> None:
        if self.provisioner is not None and self.identities is not None and isinstance(self.attempt_id, str):
            if not self.provisioner.prove_absent(self.paths, self.identities, attempt_id=self.attempt_id):
                raise ProvisioningError("controller-owned plane absence was not proved")
        if self.paths.cell_root.exists() or self.paths.cell_root.is_symlink():
            delete_tree_descriptor_safe(self.paths.cell_root)
        self.state = "DESTROYED"

    @staticmethod
    def _assert_controller_owned(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CellRuntimeError("cell path is not controller-owned mode 0700")


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise CellRuntimeError("cell path contains a symlinked ancestor")


def _delete_dir_at(parent_fd: int, name: str) -> None:
    child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        for entry in list(os.scandir(child_fd)):
            entry_name = entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _delete_dir_at(child_fd, entry_name)
            else:
                os.unlink(entry_name, dir_fd=child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(child_fd)


def delete_tree_descriptor_safe(root: Path) -> None:
    """Delete a tree without following symlinked children or hostile git metadata."""

    root = Path(root)
    if not root.is_absolute():
        raise CellRuntimeError("descriptor-safe deletion requires an absolute root")
    if not root.exists() and not root.is_symlink():
        return
    parent_fd = os.open(root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode):
            os.unlink(root.name, dir_fd=parent_fd)
        elif stat.S_ISDIR(info.st_mode):
            _delete_dir_at(parent_fd, root.name)
        else:
            os.unlink(root.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
