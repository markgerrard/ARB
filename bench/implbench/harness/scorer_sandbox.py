"""Boundaries for the isolated G1/G4 scorers.

The controller supplies only a verified post-import materialization to this module.  The
topology descriptions are deliberately value-oriented so launch construction can be tested
without starting a scorer or requiring privileged UIDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ctypes
import signal
import socket
import stat
import struct
import subprocess
import secrets
import time
import threading
import tempfile
import sys
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .scorer_launcher import SUPERVISOR_REGISTRATION_MAX_BYTES


class ScorerInputError(ValueError):
    """Raised when a scorer is given anything other than trusted post-import input."""


class BatteryBoundaryError(RuntimeError):
    """Raised when the legacy host battery path is requested."""


class ScorerOutputLimitExceeded(ScorerInputError):
    """A scorer exceeded the controller-owned pipe budget."""


class ScorerModelExecutionLimit(ScorerInputError):
    """A proven submitted-role execution limit, charged to G1."""


def _pid_gone(pid: int) -> bool:
    """Treat a reaped PID as absent; a zombie is not a completed cleanup."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # A zombie still occupies its exact PID until its parent reaps it.  It is
    # therefore not proof that the cleanup boundary completed.
    return False


class ScorerRole(str, Enum):
    KEYED_RUNNER = "keyed-runner"
    BROKER = "broker"
    SUBMITTED_PROGRAM = "submitted-program"
    COORDINATOR = "coordinator"
    SUITE_RUNNER_BROKER = "suite-runner/broker"
    SUBMITTED_CODE = "submitted-code"


MODEL_ROLES = {ScorerRole.SUBMITTED_PROGRAM.value, ScorerRole.SUBMITTED_CODE.value}
INFRASTRUCTURE_ROLES = {
    ScorerRole.KEYED_RUNNER.value,
    ScorerRole.BROKER.value,
    ScorerRole.COORDINATOR.value,
    ScorerRole.SUITE_RUNNER_BROKER.value,
}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^[0-9a-f]{40}$")
FINAL_TREE_DIGEST_VERSION = "final-tree-v1"


@dataclass(frozen=True)
class PostImportInput:
    materialization: Path
    digest: str
    provenance: str = "post-import"
    digest_version: str = FINAL_TREE_DIGEST_VERSION

    def __post_init__(self) -> None:
        path = Path(self.materialization)
        if not path.is_absolute() or path.is_symlink() or path != path.resolve(strict=False) or not path.is_dir():
            raise ScorerInputError("scorer input must be an absolute non-symlinked directory")
        if self.provenance != "post-import":
            raise ScorerInputError("scorer input is not post-import materialization")
        if self.digest_version != FINAL_TREE_DIGEST_VERSION or not _DIGEST.fullmatch(self.digest):
            raise ScorerInputError("scorer input digest is not pinned")
        object.__setattr__(self, "materialization", path.resolve(strict=True))

    @classmethod
    def from_attestation(cls, attestation: Mapping[str, Any]) -> "PostImportInput":
        if not isinstance(attestation, Mapping) or attestation.get("attested") is not True:
            raise ScorerInputError("post-import attestation is not authoritative")
        materialization = attestation.get("materialization")
        digest = attestation.get("materialization_digest")
        if not isinstance(materialization, (str, Path)) or not isinstance(digest, str):
            raise ScorerInputError("post-import attestation fields are incomplete")
        from .completion import materialization_digest

        actual = materialization_digest(materialization)
        if actual != digest:
            raise ScorerInputError("post-import materialization digest mismatch")
        return cls(Path(materialization), actual)


def post_import_input(
    materialization: str | Path,
    *,
    digest: str,
    provenance: str = "post-import",
    digest_version: str = FINAL_TREE_DIGEST_VERSION,
) -> PostImportInput:
    return PostImportInput(Path(materialization), digest, provenance, digest_version)


@dataclass(frozen=True)
class ScorerProcess:
    role: ScorerRole
    uid: int
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScorerTopology:
    gate: str
    processes: tuple[ScorerProcess, ...]
    public_suite_oid: str | None = None
    public_suite_digest: str | None = None

    def process(self, role: ScorerRole | str) -> ScorerProcess:
        wanted = role.value if isinstance(role, ScorerRole) else role
        for process in self.processes:
            if process.role.value == wanted:
                return process
        raise ScorerInputError(f"role is not part of {self.gate} topology: {wanted}")


@dataclass(frozen=True)
class G4ReceiptBinding:
    """The controller-authenticated facts which a G4 receipt may expose."""

    cell_id: str
    attempt_id: str
    commit_oid: str
    public_suite_oid: str
    public_suite_digest: str
    public_suite_digest_version: str
    controller_sequence: int
    nonce: str

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"cell-[0-9a-f]{64}", self.cell_id)
                or not re.fullmatch(r"attempt-[0-9a-f]{32,64}", self.attempt_id)
                or not _OID.fullmatch(self.commit_oid)
                or not _OID.fullmatch(self.public_suite_oid)
                or not _DIGEST.fullmatch(self.public_suite_digest)
                or not isinstance(self.public_suite_digest_version, str)
                or not self.public_suite_digest_version
                or isinstance(self.controller_sequence, bool)
                or not isinstance(self.controller_sequence, int)
                or self.controller_sequence < 1
                or not _DIGEST.fullmatch(self.nonce)):
            raise ScorerInputError("G4 receipt binding is not fixed-width authenticated evidence")


def _check_uids(uids: Sequence[int]) -> None:
    if any(isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 for uid in uids):
        raise ScorerInputError("scorer UIDs must be positive integers")
    if len(set(uids)) != len(uids):
        raise ScorerInputError("scorer UIDs must be distinct")


def _env(*, role: ScorerRole, key: str | None = None) -> dict[str, str]:
    environment = {
        "IMPLBENCH_SCORER_ROLE": role.value,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.defpath,
    }
    if key is not None:
        if not key:
            raise ScorerInputError("battery key must not be empty")
        environment["IMPLBENCH_BATTERY_KEY"] = key
    return environment


def build_g1_topology(
    *,
    keyed_runner_uid: int,
    broker_uid: int,
    submitted_program_uid: int,
    battery_key: str,
    verdict_key: str | None = None,
) -> ScorerTopology:
    if verdict_key is not None:
        raise ScorerInputError("verdict authority is controller-owned, not a role capability")
    _check_uids((keyed_runner_uid, broker_uid, submitted_program_uid))
    return ScorerTopology(
        "G1",
        (
            ScorerProcess(ScorerRole.KEYED_RUNNER, keyed_runner_uid, _env(role=ScorerRole.KEYED_RUNNER, key=battery_key)),
            ScorerProcess(ScorerRole.BROKER, broker_uid, _env(role=ScorerRole.BROKER)),
            ScorerProcess(ScorerRole.SUBMITTED_PROGRAM, submitted_program_uid, _env(role=ScorerRole.SUBMITTED_PROGRAM)),
        ),
    )


def build_g4_topology(
    *,
    coordinator_uid: int,
    broker_uid: int,
    submitted_code_uid: int,
    suite_runner_broker_uid: int | None = None,
    public_suite_oid: str,
    public_suite_digest: str,
    verdict_key: str | None = None,
) -> ScorerTopology:
    if verdict_key is not None:
        raise ScorerInputError("verdict authority is controller-owned, not a role capability")
    suite_runner_broker_uid = broker_uid if suite_runner_broker_uid is None else suite_runner_broker_uid
    _check_uids((coordinator_uid, suite_runner_broker_uid, submitted_code_uid))
    if not _OID.fullmatch(public_suite_oid) or not _DIGEST.fullmatch(public_suite_digest):
        raise ScorerInputError("public suite pin is not fixed width")
    return ScorerTopology(
        "G4",
        (
            ScorerProcess(ScorerRole.COORDINATOR, coordinator_uid, _env(role=ScorerRole.COORDINATOR)),
            ScorerProcess(ScorerRole.SUITE_RUNNER_BROKER, suite_runner_broker_uid, _env(role=ScorerRole.SUITE_RUNNER_BROKER)),
            ScorerProcess(ScorerRole.SUBMITTED_CODE, submitted_code_uid, _env(role=ScorerRole.SUBMITTED_CODE)),
        ),
        public_suite_oid,
        public_suite_digest,
    )


@dataclass(frozen=True)
class ScorerRunResult:
    role: str
    exit_code: int
    stdout: str
    stderr: str
    submitted_child_exit_code: int | None = None


@dataclass(frozen=True)
class ScorerParentChildResult:
    """Authenticated launcher report for both role exits."""

    broker: subprocess.CompletedProcess[str]
    child: subprocess.CompletedProcess[str]
    broker_pid: int | None = None
    execution_timeout_roles: frozenset[str] = frozenset()
    output_limit_role: str | None = None


_GRAPH_PROTOCOL = "implbench-role-v2"
_GRAPH_MAX_FRAME = 64 * 1024
_GRAPH_MAX_VALUE = 16 * 1024


class _RoleGraph:
    """Controller-owned, one-shot AF_UNIX protocol for the six scorer roles.

    Each role gets its own 0600 endpoint, capability, and kernel-UID check.  The
    controller owns routing and the only admitted result; roles never receive a
    shared verdict key or a peer endpoint they can impersonate.
    """

    def __init__(self, root: Path, topology: ScorerTopology, *,
                 g4_receipt_bindings: Sequence[G4ReceiptBinding] = (),
                 structural_identity: bool = False):
        self.topology = topology
        self.root = root
        self.ready: set[str] = set()
        self._directory = Path(tempfile.mkdtemp(prefix=f"implbench-{topology.gate.lower()}-"))
        self._endpoints = {process.role.value: self._directory / f"{index}.sock" for index, process in enumerate(topology.processes)}
        self._listeners: dict[str, socket.socket] = {}
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._lock = threading.Condition()
        if topology.gate == "G4":
            if not g4_receipt_bindings:
                raise ScorerInputError("G4 graph requires controller receipt bindings")
            if len({item.commit_oid for item in g4_receipt_bindings}) != len(g4_receipt_bindings):
                raise ScorerInputError("G4 receipt bindings replay a commit")
        elif g4_receipt_bindings:
            raise ScorerInputError("G1 graph cannot receive G4 receipt bindings")
        self._g4_receipt_bindings = {item.commit_oid: item for item in g4_receipt_bindings}
        self._g4_receipt_order = tuple(item.commit_oid for item in g4_receipt_bindings)
        self._g4_receipts: dict[str, dict[str, Any]] = {}
        self._inboxes: dict[str, deque[dict[str, Any]]] = {process.role.value: deque() for process in topology.processes}
        self._result: dict[str, str] = {}
        if not isinstance(structural_identity, bool):
            raise ScorerInputError("scorer graph structural identity mode is invalid")
        self.structural_identity = structural_identity

    def environment_for(self, role: ScorerRole) -> dict[str, str]:
        return {
            "IMPLBENCH_GRAPH_ENDPOINT": str(self._endpoints[role.value]),
            "IMPLBENCH_GRAPH_GATE": self.topology.gate,
        }

    def start(self) -> None:
        # The directory is traversable but not readable; every socket is a distinct
        # role capability and only its declared UID can open it.
        os.chmod(self._directory, 0o711)
        for process in self.topology.processes:
            endpoint = self._endpoints[process.role.value]
            if endpoint.exists() or endpoint.is_symlink():
                raise ScorerInputError("scorer graph endpoint already exists")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(endpoint))
            if not stat.S_ISSOCK(endpoint.stat().st_mode):
                listener.close()
                raise ScorerInputError("scorer graph endpoint is not a socket")
            try:
                if process.uid != os.getuid() and not self.structural_identity:
                    os.chown(endpoint, process.uid, -1)
            except OSError as exc:
                listener.close()
                raise ScorerInputError("scorer graph endpoint ownership could not be set") from exc
            os.chmod(endpoint, 0o600)
            listener.listen(8)
            listener.settimeout(0.05)
            self._listeners[process.role.value] = listener
        self._thread = threading.Thread(target=self._serve, name="implbench-scorer-graph", daemon=True)
        self._thread.start()

    @staticmethod
    def _peer_uid(connection: socket.socket) -> int | None:
        try:
            if hasattr(socket, "SO_PEERCRED"):
                return int.from_bytes(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)[4:8], "little")
            if hasattr(connection, "getpeereid"):
                return connection.getpeereid()[0]  # type: ignore[attr-defined]
            # CPython on macOS does not expose getpeereid(), while LOCAL_PEERCRED
            # has a platform-specific xucred layout.  Call the kernel primitive
            # directly rather than guessing offsets in that structure.
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            if ctypes.CDLL(None).getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
                return int(uid.value)
        except (AttributeError, OSError):
            return None
        return None

    @staticmethod
    def _read_frame(connection: socket.socket) -> dict[str, Any]:
        header = _read_exact(connection, 4)
        length = struct.unpack("!I", header)[0]
        if length == 0 or length > _GRAPH_MAX_FRAME:
            raise ScorerInputError("scorer graph frame is out of bounds")
        raw = _read_exact(connection, length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ScorerInputError("scorer graph frame is not an object")
        return value

    @staticmethod
    def _write_frame(connection: socket.socket, value: Mapping[str, Any]) -> None:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > _GRAPH_MAX_FRAME:
            raise ScorerInputError("scorer graph response is out of bounds")
        connection.sendall(struct.pack("!I", len(raw)) + raw)

    def _serve(self) -> None:
        while not self._closed.is_set():
            for expected_role, listener in tuple(self._listeners.items()):
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    continue
                threading.Thread(target=self._handle, args=(connection, expected_role), daemon=True).start()

    def _handle(self, connection: socket.socket, expected_role: str) -> None:
        with connection:
            connection.settimeout(2.0)
            try:
                value = self._read_frame(connection)
                process = self.topology.process(expected_role)
                expected_uid = os.getuid() if self.structural_identity else process.uid
                if (value.get("protocol") != _GRAPH_PROTOCOL or value.get("role") != expected_role
                        or self._peer_uid(connection) != expected_uid):
                    raise ScorerInputError("scorer graph authentication failed")
                kind = value.get("kind")
                if kind == "ready":
                    with self._lock:
                        self.ready.add(expected_role)
                        self._lock.notify_all()
                    response: Mapping[str, Any] = {"ok": True}
                elif kind == "send":
                    response = self._route(expected_role, value)
                elif kind == "receive":
                    response = self._receive(expected_role, value)
                else:
                    raise ScorerInputError("scorer graph message kind is invalid")
                self._write_frame(connection, response)
            except (OSError, UnicodeError, ValueError, ScorerInputError):
                # A rejected peer gets no diagnostic that could disclose a capability.
                return

    def _route(self, sender: str, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        message_type = frame.get("type")
        payload = frame.get("payload")
        if not isinstance(message_type, str) or not isinstance(payload, Mapping):
            raise ScorerInputError("scorer graph message schema is invalid")
        with self._lock:
            if self.ready != {process.role.value for process in self.topology.processes}:
                raise ScorerInputError("scorer graph is not ready")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _GRAPH_MAX_VALUE:
            raise ScorerInputError("scorer graph payload is out of bounds")
        recipient = self._transition(sender, message_type, payload)
        if recipient is not None:
            with self._lock:
                self._inboxes[recipient].append({"type": message_type, "payload": dict(payload)})
                self._lock.notify_all()
        return {"ok": True}

    def _transition(self, sender: str, message_type: str, payload: Mapping[str, Any]) -> str | None:
        if self.topology.gate == "G1":
            edges = {
                (ScorerRole.KEYED_RUNNER.value, "g1.request"): ScorerRole.BROKER.value,
                (ScorerRole.BROKER.value, "g1.execute"): ScorerRole.SUBMITTED_PROGRAM.value,
                (ScorerRole.SUBMITTED_PROGRAM.value, "g1.response"): ScorerRole.BROKER.value,
                (ScorerRole.BROKER.value, "g1.candidate"): ScorerRole.KEYED_RUNNER.value,
            }
            recipient = edges.get((sender, message_type))
            if recipient is not None:
                return recipient
            if sender == ScorerRole.KEYED_RUNNER.value and message_type == "g1.verdict":
                self._record_g1_result(payload)
                return None
        else:
            edges = {
                (ScorerRole.COORDINATOR.value, "g4.call"): ScorerRole.SUITE_RUNNER_BROKER.value,
                (ScorerRole.SUITE_RUNNER_BROKER.value, "g4.execute"): ScorerRole.SUBMITTED_CODE.value,
                (ScorerRole.SUBMITTED_CODE.value, "g4.response"): ScorerRole.SUITE_RUNNER_BROKER.value,
                (ScorerRole.SUITE_RUNNER_BROKER.value, "g4.outcome"): ScorerRole.COORDINATOR.value,
            }
            recipient = edges.get((sender, message_type))
            if recipient is not None:
                return recipient
            if sender == ScorerRole.COORDINATOR.value and message_type == "g4.receipt":
                self._record_g4_receipt(payload)
                return None
            if sender == ScorerRole.COORDINATOR.value and message_type == "g4.verdict":
                self._record_g4_result(payload)
                return None
        raise ScorerInputError("scorer graph role is not authorized for this message")

    def _record_g1_result(self, payload: Mapping[str, Any]) -> None:
        if set(payload) != {"g1", "g3", "g5", "g6", "g7"}:
            raise ScorerInputError("G1 result schema is not closed")
        if any(payload[name] not in {"PASS", "FAIL", "UNKNOWN"} for name in payload):
            raise ScorerInputError("G1 result verdict is not closed")
        with self._lock:
            if self._result:
                raise ScorerInputError("scorer graph result is duplicated")
            self._result.update({name: str(payload[name]) for name in payload})
            self._lock.notify_all()

    def _record_g4_receipt(self, payload: Mapping[str, Any]) -> None:
        if set(payload) != {"commit_oid", "outcome_enum"}:
            raise ScorerInputError("G4 receipt schema is not closed")
        commit_oid = payload.get("commit_oid")
        outcome = payload.get("outcome_enum")
        if not isinstance(commit_oid, str) or outcome not in {"PASS", "FAIL"}:
            raise ScorerInputError("G4 receipt evidence is invalid")
        binding = self._g4_receipt_bindings.get(commit_oid)
        if binding is None or commit_oid in self._g4_receipts:
            raise ScorerInputError("G4 receipt replay or pin mismatch")
        self._g4_receipts[commit_oid] = {
            "cell_id": binding.cell_id, "attempt_id": binding.attempt_id,
            "commit_oid": binding.commit_oid, "public_suite_oid": binding.public_suite_oid,
            "public_suite_digest": binding.public_suite_digest,
            "public_suite_digest_version": binding.public_suite_digest_version,
            "outcome_enum": outcome, "controller_sequence": binding.controller_sequence,
            "nonce": binding.nonce,
        }

    def _record_g4_result(self, payload: Mapping[str, Any]) -> None:
        if set(payload) != {"g4"} or payload.get("g4") not in {"PASS", "FAIL", "UNKNOWN"}:
            raise ScorerInputError("G4 verdict schema is not closed")
        with self._lock:
            if "g4" in self._result:
                raise ScorerInputError("G4 verdict is duplicated")
            self._result["g4"] = str(payload["g4"])
            self._lock.notify_all()

    def _receive(self, role: str, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout_ms = frame.get("timeout_ms", 1000)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 5_000:
            raise ScorerInputError("scorer graph receive timeout is invalid")
        deadline = time.monotonic() + timeout_ms / 1000
        with self._lock:
            while not self._inboxes[role] and not self._closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ok": True, "message": None}
                self._lock.wait(remaining)
            message = self._inboxes[role].popleft() if self._inboxes[role] else None
        return {"ok": True, "message": message}

    def wait_ready(self, timeout: float) -> bool:
        wanted = {p.role.value for p in self.topology.processes}
        deadline = time.monotonic() + timeout
        with self._lock:
            while self.ready != wanted:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(remaining)
        return True

    def release(self) -> None:
        """Controller-only start barrier: no scorer role may execute before all join."""

        with self._lock:
            wanted = {p.role.value for p in self.topology.processes}
            if self.ready != wanted:
                raise ScorerInputError("scorer graph cannot release before all roles are ready")
            for role in wanted:
                self._inboxes[role].append({"type": "graph.start", "payload": {}})
            self._lock.notify_all()

    def controller_result(self) -> dict[str, Any]:
        with self._lock:
            if self.topology.gate == "G1":
                if set(self._result) != {"g1", "g3", "g5", "g6", "g7"}:
                    raise ScorerInputError("scorer graph did not produce its controller-owned result")
                return dict(self._result)
            # Arrival is intentionally independent of import order.  The controller
            # admits the exact authenticated set and emits it in import order below.
            if set(self._result) != {"g4"} or set(self._g4_receipts) != set(self._g4_receipt_order):
                raise ScorerInputError("scorer graph did not produce its controller-owned result")
            return {"g4": self._result["g4"], "g4_receipts": tuple(self._g4_receipts[oid] for oid in self._g4_receipt_order)}

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._lock.notify_all()
        for listener in self._listeners.values():
            listener.close()
        if self._thread:
            self._thread.join(timeout=1)
        for endpoint in self._endpoints.values():
            endpoint.unlink(missing_ok=True)
        self._directory.rmdir()


def _read_exact(connection: socket.socket, wanted: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < wanted:
        chunk = connection.recv(wanted - len(chunks))
        if not chunk:
            raise ScorerInputError("scorer graph frame is truncated")
        chunks.extend(chunk)
    return bytes(chunks)


def role_graph_request(kind: str, *, message_type: str | None = None, payload: Mapping[str, Any] | None = None,
                       timeout_ms: int | None = None, nonce: str | None = None) -> Mapping[str, Any]:
    """Role-side narrow client used by scorer binaries and executable integration tests."""
    endpoint = os.environ["IMPLBENCH_GRAPH_ENDPOINT"]
    frame: dict[str, Any] = {
        "protocol": _GRAPH_PROTOCOL, "role": os.environ["IMPLBENCH_SCORER_ROLE"],
        "kind": kind,
    }
    if message_type is not None:
        frame["type"] = message_type
    if payload is not None:
        frame["payload"] = dict(payload)
    if timeout_ms is not None:
        frame["timeout_ms"] = timeout_ms
    if nonce is not None:
        frame["nonce"] = nonce
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(6.0)
        connection.connect(endpoint)
        _RoleGraph._write_frame(connection, frame)
        return _RoleGraph._read_frame(connection)


class ScorerUidLauncher:
    """Launch scorer roles through a fresh, single-threaded Python boundary."""

    _GRACE_S = 1.0
    _PAIR_CLEANUP_MARGIN_S = 0.5

    def __init__(self, *, structural_identity: bool = False, profile: str | None = None,
                 profile_digest: str | None = None) -> None:
        if not isinstance(structural_identity, bool):
            raise ScorerInputError("scorer launcher structural identity mode is invalid")
        if (profile is None) != (profile_digest is None):
            raise ScorerInputError("scorer launch profile binding is incomplete")
        if profile is not None and hashlib.sha256(profile.encode()).hexdigest() != profile_digest:
            raise ScorerInputError("scorer launch profile digest mismatch")
        self.structural_identity = structural_identity
        self.profile = profile
        self.profile_digest = profile_digest
        self.launch_evidence: list[dict[str, Any]] = []

    def _command(self, args: Sequence[str], config: Mapping[str, Any]) -> list[str]:
        base = [sys.executable, "-m", "implbench.harness.scorer_launcher", *args]
        enforced = (["/usr/bin/sandbox-exec", "-p", self.profile, *base]
                    if self.profile is not None else base)
        structural = ([sys.executable, "-m", "implbench.harness.scorer_profile_helper",
                       "--profile", self.profile, "--digest", self.profile_digest or "", "--", *base]
                      if self.profile is not None else base)
        command = structural if self.structural_identity else enforced
        self.launch_evidence.append({
            "argv": tuple(command),
            "executable": base[0],
            "profile_digest": self.profile_digest,
            "structural_identity": self.structural_identity,
            "requested_uids": tuple(
                value for key, value in config.items() if key in {"uid", "child_uid"}
            ),
        })
        return command

    @staticmethod
    def _config_pipe(config: Mapping[str, Any]) -> int:
        """Return a read descriptor containing only this launch's bounded config."""
        raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > 4096:
            raise ScorerInputError("scorer launcher configuration exceeds its pipe budget")
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, raw)
        finally:
            os.close(write_fd)
        return read_fd

    @staticmethod
    def _launcher_environment() -> dict[str, str]:
        # Never pass controller ambient state to the privilege boundary.  The
        # inherited configuration descriptor is the complete authority surface.
        return {
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).parents[2]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    @staticmethod
    def _terminate(process: subprocess.Popen[str], registered_pids: Sequence[int] = ()) -> None:
        """Terminate only this launch session and its pre-registered descendants."""
        targets = tuple(pid for pid in registered_pids if isinstance(pid, int) and pid > 0 and pid != os.getpid())
        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        # The launcher leader may already have exited while a different-UID
        # submitted role remains in its session, so do not treat wait() of the
        # leader as evidence that the registered descendants are gone.
        deadline = time.monotonic() + ScorerUidLauncher._GRACE_S
        while time.monotonic() < deadline:
            if all(_pid_gone(pid) for pid in targets):
                break
            time.sleep(.01)
        for pid in targets:
            if not _pid_gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=ScorerUidLauncher._GRACE_S)
        deadline = time.monotonic() + ScorerUidLauncher._GRACE_S
        while any(not _pid_gone(pid) for pid in targets) and time.monotonic() < deadline:
            time.sleep(.01)
        if any(not _pid_gone(pid) for pid in targets):
            raise ScorerInputError("scorer cleanup left a registered process live")

    @staticmethod
    def _supervisor_registration_from_bytes(raw: bytes) -> tuple[int, ...]:
        """Validate the fixed-size trusted cleanup registration payload."""
        if not raw or len(raw) > SUPERVISOR_REGISTRATION_MAX_BYTES:
            return ()
        try:
            value = json.loads(raw.decode())
        except (UnicodeError, ValueError):
            return ()
        if not isinstance(value, Mapping):
            return ()
        pids = tuple(value.get(name) for name in ("boundary", "helper", "broker", "child"))
        return tuple(pid for pid in pids if isinstance(pid, int) and pid > 0)

    @staticmethod
    def _supervisor_registration(fd: int) -> tuple[int, ...]:
        """Read the boundary's fixed, trusted cleanup registration if available."""
        try:
            raw = os.read(fd, SUPERVISOR_REGISTRATION_MAX_BYTES)
        except BlockingIOError:
            return ()
        return ScorerUidLauncher._supervisor_registration_from_bytes(raw)

    def _invoke(self, config: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        if os.name != "posix":
            raise ScorerInputError("scorer UID launcher requires POSIX")
        config_fd = self._config_pipe(config)
        try:
            process = subprocess.Popen(
                self._command(("--config-fd", str(config_fd)), config),
                cwd=str(config["cwd"]), env=self._launcher_environment(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, shell=False, start_new_session=True, pass_fds=(config_fd,),
            )
            os.close(config_fd)
            config_fd = -1
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                raise
            if process.returncode != 0:
                raise ScorerInputError(f"scorer launcher failed: {stderr or stdout}")
            try:
                value = json.loads(stdout)
            except (TypeError, ValueError) as exc:
                raise ScorerInputError("scorer launcher produced invalid status") from exc
            if not isinstance(value, Mapping) or value.get("ok") is not True or not isinstance(value.get("result"), Mapping):
                raise ScorerInputError(f"scorer launcher reported setup failure: {value.get('error') if isinstance(value, Mapping) else stderr or stdout}")
            result = dict(value["result"])
            role_pids = result.get("role_pids")
            if (not isinstance(role_pids, Mapping) or not role_pids
                    or any(not isinstance(role, str) or not isinstance(pid, int) or pid <= 0
                           for role, pid in role_pids.items())):
                raise ScorerInputError("scorer launcher omitted role PID evidence")
            self.launch_evidence.append({
                "role": config.get("env", {}).get("IMPLBENCH_SCORER_ROLE"),
                "role_pids": dict(role_pids),
            })
            return result
        finally:
            if config_fd >= 0:
                os.close(config_fd)

    def _invoke_parent_child(self, config: Mapping[str, Any], *, timeout: float) -> ScorerParentChildResult:
        config_fd = self._config_pipe(config)
        supervisor_read, supervisor_write = os.pipe()
        os.set_blocking(supervisor_read, False)
        registered_pids: tuple[int, ...] = ()
        try:
            process = subprocess.Popen(
                self._command(("--config-fd", str(config_fd), "--supervisor-fd", str(supervisor_write)), config),
                cwd=str(config["cwd"]), env=self._launcher_environment(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, shell=False, start_new_session=True, pass_fds=(config_fd, supervisor_write),
            )
            os.close(config_fd)
            config_fd = -1
            os.close(supervisor_write)
            supervisor_write = -1
            try:
                # The paired boundary owns the scoring deadline and needs only
                # this short margin to finish its helper-mediated reaping.
                stdout, stderr = process.communicate(timeout=timeout + self._PAIR_CLEANUP_MARGIN_S)
            except subprocess.TimeoutExpired:
                registered_pids = self._supervisor_registration(supervisor_read)
                self._terminate(process, registered_pids)
                raise
            registered_pids = self._supervisor_registration(supervisor_read)
            if len(stdout.encode()) > config["max_output_bytes"] or len(stderr.encode()) > config["max_output_bytes"]:
                self._terminate(process, registered_pids)
                raise ScorerOutputLimitExceeded("scorer output cap exceeded")
            if process.returncode != 0:
                self._terminate(process, registered_pids)
            try:
                value = json.loads(stdout)
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise ScorerInputError("scorer launcher produced invalid status") from exc
            if not isinstance(value, Mapping) or value.get("ok") is not True or not isinstance(value.get("result"), Mapping):
                raise ScorerInputError(f"scorer launcher reported setup failure: {value.get('error') if isinstance(value, Mapping) else stderr or stdout}")
            result = value["result"]
            broker = result.get("broker")
            child = result.get("child")
            broker_pid = result.get("broker_pid")
            timeout_roles = result.get("execution_timeout_roles")
            output_limit_role = result.get("output_limit_role")
            role_pids = result.get("role_pids")
            if (not isinstance(broker, Mapping) or not isinstance(broker.get("exit_code"), int)
                    or not isinstance(child, Mapping) or not isinstance(child.get("exit_code"), int)
                    or not isinstance(broker_pid, int) or broker_pid <= 0
                    or not isinstance(role_pids, Mapping) or set(role_pids) != {"broker", "child"}
                    or any(not isinstance(pid, int) or pid <= 0 for pid in role_pids.values())
                    or not isinstance(timeout_roles, list)
                    or any(role not in {"broker", "child"} for role in timeout_roles)
                    or len(set(timeout_roles)) != len(timeout_roles)
                    or output_limit_role not in {None, "broker", "child"}):
                raise ScorerInputError("submitted child status is unavailable")
            self.launch_evidence.append({
                "role": config.get("env", {}).get("IMPLBENCH_SCORER_ROLE"),
                "child_role": config.get("child_env", {}).get("IMPLBENCH_SCORER_ROLE"),
                "role_pids": dict(role_pids),
            })
            return ScorerParentChildResult(
                subprocess.CompletedProcess(config["argv"], broker["exit_code"], str(broker.get("stdout", "")), str(broker.get("stderr", ""))),
                subprocess.CompletedProcess(config["child_argv"], child["exit_code"], "", ""),
                broker_pid,
                frozenset(timeout_roles),
                output_limit_role,
            )
        finally:
            if config_fd >= 0:
                os.close(config_fd)
            if supervisor_write >= 0:
                os.close(supervisor_write)
            os.close(supervisor_read)

    def run(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int = 64 * 1024,
    ) -> subprocess.CompletedProcess[str]:
        if max_output_bytes <= 0:
            raise ScorerInputError("scorer output budget must be positive")
        result = self._invoke({"argv": list(argv), "uid": uid, "cwd": str(cwd), "env": dict(env),
                               "max_output_bytes": max_output_bytes,
                               "structural_identity": self.structural_identity}, timeout=timeout)
        broker = result.get("broker")
        if not isinstance(broker, Mapping) or not isinstance(broker.get("exit_code"), int):
            raise ScorerInputError("scorer launcher omitted broker status")
        return subprocess.CompletedProcess(argv, broker["exit_code"], str(broker.get("stdout", "")), str(broker.get("stderr", "")))

    def run_parent_child(
        self,
        argv: Sequence[str],
        *,
        uid: int,
        cwd: Path,
        env: Mapping[str, str],
        child_argv: Sequence[str],
        child_uid: int,
        child_env: Mapping[str, str],
        timeout: float,
        max_output_bytes: int = 64 * 1024,
    ) -> ScorerParentChildResult:
        """Launch and require explicit terminal status for broker and submitted child."""
        if not child_argv or max_output_bytes <= 0:
            raise ScorerInputError("scorer parent/child launch is invalid")
        # The executable supervisor directly waits for both of its children and
        # returns their independently observed statuses on its controller pipe.
        return self._invoke_parent_child(
            {"argv": list(argv), "uid": uid, "cwd": str(cwd), "env": dict(env),
             "child_argv": list(child_argv), "child_uid": child_uid,
             "child_env": dict(child_env), "max_output_bytes": max_output_bytes,
             "execution_timeout_s": timeout,
             "structural_identity": self.structural_identity}, timeout=timeout,
        )


class ScorerSandbox:
    """Small hermetic subprocess boundary used by the keyed and keyless runners."""

    def __init__(self, root: str | Path, materialization: PostImportInput, topology: ScorerTopology, *,
                 launcher: Any | None = None, reaper: Any | None = None,
                 max_output_bytes: int = 64 * 1024, max_program_bytes: int = 64 * 1024 * 1024,
                 structural_identity: bool = False, launch_profile: str | None = None,
                 launch_profile_digest: str | None = None):
        root_path = Path(root)
        if not root_path.is_absolute() or root_path.is_symlink() or not root_path.is_dir():
            raise ScorerInputError("scorer sandbox root must be a real absolute directory")
        self.root = root_path.resolve(strict=True)
        self.materialization = materialization
        self.topology = topology
        self.structural_identity = structural_identity
        self.launcher = launcher or ScorerUidLauncher(
            structural_identity=structural_identity,
            profile=launch_profile,
            profile_digest=launch_profile_digest,
        )
        self.reaper = reaper
        if not callable(getattr(self.launcher, "run", None)):
            raise ScorerInputError("scorer launcher must expose run")
        self.max_output_bytes = max_output_bytes
        self._graph_environment: dict[str, str] = {}
        self.last_graph_result: dict[str, Any] | None = None
        if not isinstance(materialization, PostImportInput):
            raise ScorerInputError("scorer requires a trusted post-import input")
        if max_output_bytes <= 0:
            raise ScorerInputError("max_output_bytes must be positive")
        if not _beneath(materialization.materialization, self.root):
            raise ScorerInputError("post-import materialization is outside scorer sandbox")
        validate_program_size(materialization.materialization, max_bytes=max_program_bytes)

    def run(self, role: ScorerRole | str, argv: Sequence[str], *, timeout_s: float = 30.0,
            graph_environment: Mapping[str, str] | None = None) -> ScorerRunResult:
        process = self.topology.process(role)
        if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ScorerInputError("scorer argv is invalid")
        env = dict(process.environment)
        env.update(self._graph_environment if graph_environment is None else graph_environment)
        env["IMPLBENCH_POST_IMPORT_DIGEST"] = self.materialization.digest
        env["IMPLBENCH_POST_IMPORT_DIGEST_VERSION"] = self.materialization.digest_version
        try:
            try:
                completed = self.launcher.run(
                    argv, uid=process.uid, cwd=self.materialization.materialization, env=env,
                    timeout=timeout_s, max_output_bytes=self.max_output_bytes,
                )
            except TypeError as exc:
                # Hermetic test doubles from older callers may not accept the new boundary
                # argument; the independent post-drain check below remains mandatory.
                if "max_output_bytes" not in str(exc):
                    raise
                completed = self.launcher.run(
                    argv, uid=process.uid, cwd=self.materialization.materialization, env=env,
                    timeout=timeout_s,
                )
        except subprocess.TimeoutExpired as exc:
            if process.role.value in MODEL_ROLES:
                raise ScorerModelExecutionLimit("submitted scorer execution timeout") from exc
            raise ScorerInputError("scorer execution timeout") from exc
        except (OSError, subprocess.SubprocessError, ScorerInputError) as exc:
            raise ScorerInputError("scorer UID launch failed") from exc
        finally:
            if callable(self.reaper):
                self.reaper(process.uid)
        # Production's ScorerUidLauncher drains role bytes to an unlinked
        # descriptor before this boundary.  Older hermetic launch doubles still
        # return bytes, so retain the independent deny checks that prove a secret
        # or cap violation goes RED rather than weakening those tests.
        if completed.stdout or completed.stderr:
            raw = (completed.stdout + completed.stderr).encode("utf-8", errors="replace")
            secret = next(
                (item.environment["IMPLBENCH_BATTERY_KEY"] for item in self.topology.processes
                 if "IMPLBENCH_BATTERY_KEY" in item.environment),
                None,
            )
            if secret and secret.encode() in raw:
                raise ScorerInputError("battery key exfiltrated in scorer output")
            if len(raw) > self.max_output_bytes:
                if process.role.value in MODEL_ROLES:
                    raise ScorerModelExecutionLimit("submitted scorer output cap exceeded")
                raise ScorerOutputLimitExceeded("scorer output cap exceeded")
            raise ScorerInputError("scorer launcher released raw role output")
        if completed.returncode != 0:
            raise ScorerInputError(f"scorer role {process.role.value} exited {completed.returncode}")
        # The trusted in-sandbox encoder releases only this bounded fixed schema.
        return ScorerRunResult(process.role.value, completed.returncode, "", "")

    def run_parent_child(
        self, parent: ScorerRole | str, parent_argv: Sequence[str], child: ScorerRole | str,
        child_argv: Sequence[str], *, timeout_s: float, parent_graph_environment: Mapping[str, str],
        child_graph_environment: Mapping[str, str],
    ) -> ScorerRunResult:
        if not isinstance(self.launcher, ScorerUidLauncher):
            raise ScorerInputError("real parent/child launch requires the production UID launcher")
        parent_process = self.topology.process(parent)
        child_process = self.topology.process(child)
        parent_env = {**parent_process.environment, **parent_graph_environment,
                      "IMPLBENCH_POST_IMPORT_DIGEST": self.materialization.digest,
                      "IMPLBENCH_POST_IMPORT_DIGEST_VERSION": self.materialization.digest_version}
        child_env = {**child_process.environment, **child_graph_environment,
                     "IMPLBENCH_POST_IMPORT_DIGEST": self.materialization.digest,
                     "IMPLBENCH_POST_IMPORT_DIGEST_VERSION": self.materialization.digest_version}
        try:
            completed = self.launcher.run_parent_child(
                parent_argv, uid=parent_process.uid, cwd=self.materialization.materialization,
                env=parent_env, child_argv=child_argv, child_uid=child_process.uid,
                child_env=child_env, timeout=timeout_s, max_output_bytes=self.max_output_bytes,
            )
        except ScorerInputError as exc:
            raise ScorerInputError("scorer parent/child launch failed") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScorerInputError("scorer parent/child launch failed") from exc
        finally:
            if callable(self.reaper):
                self.reaper(parent_process.uid); self.reaper(child_process.uid)
        if child_process.role.value in MODEL_ROLES:
            if "child" in completed.execution_timeout_roles:
                raise ScorerModelExecutionLimit("submitted scorer execution timeout")
            if completed.output_limit_role == "child":
                raise ScorerModelExecutionLimit("submitted scorer output cap exceeded")
        if completed.broker.returncode != 0:
            raise ScorerInputError(f"scorer role {parent_process.role.value} exited {completed.broker.returncode}")
        if completed.child.returncode != 0:
            raise ScorerInputError(f"scorer role {child_process.role.value} exited {completed.child.returncode}")
        if completed.broker.stdout or completed.broker.stderr or completed.child.stdout or completed.child.stderr:
            raise ScorerInputError("scorer launcher released raw role output")
        return ScorerRunResult(parent_process.role.value, completed.broker.returncode, "", "",
                               submitted_child_exit_code=completed.child.returncode)

    def run_topology(self, commands: Mapping[ScorerRole | str, Sequence[str]], *, timeout_s: float = 30.0,
                     g4_receipt_bindings: Sequence[G4ReceiptBinding] = ()) -> tuple[ScorerRunResult, ...]:
        """Start every role concurrently and supervise the topology as one unit."""
        if {role.value if isinstance(role, ScorerRole) else role for role in commands} != {p.role.value for p in self.topology.processes}:
            raise ScorerInputError("topology command set is incomplete")
        # A real scorer topology has no shared environment capability.  Its only IPC
        # edges are role-private sockets authenticated by kernel UID and a one-shot
        # capability; each typed transition is admitted by the controller.
        graph = _RoleGraph(
            self.root, self.topology, g4_receipt_bindings=g4_receipt_bindings,
            structural_identity=self.structural_identity,
        )
        real_launcher = isinstance(self.launcher, ScorerUidLauncher)
        try:
            if real_launcher:
                graph.start()
            self._graph_environment = {}
        except (OSError, TypeError):
            graph.close()
            raise ScorerInputError("scorer graph IPC endpoint could not be created")
        pool = ThreadPoolExecutor(max_workers=len(commands), thread_name_prefix=f"implbench-{self.topology.gate}")
        futures = []
        try:
            parent_for_child = (
                {ScorerRole.SUBMITTED_PROGRAM: ScorerRole.BROKER}
                if self.topology.gate == "G1"
                else {ScorerRole.SUBMITTED_CODE: ScorerRole.SUITE_RUNNER_BROKER}
            )
            if real_launcher:
                child_roles = set(parent_for_child)
                for role, argv in commands.items():
                    role_value = role if isinstance(role, ScorerRole) else ScorerRole(role)
                    if role_value in child_roles:
                        continue
                    child = next((item for item, parent in parent_for_child.items() if parent is role_value), None)
                    process = self.topology.process(role_value)
                    graph_environment = graph.environment_for(process.role)
                    if child is None:
                        futures.append(pool.submit(self.run, role_value, _readiness_wrapper(argv), timeout_s=timeout_s,
                                                   graph_environment=graph_environment))
                    else:
                        child_argv = commands[child]
                        futures.append(pool.submit(
                            self.run_parent_child, role_value, _readiness_wrapper(argv), child,
                            _readiness_wrapper(child_argv), timeout_s=timeout_s,
                            parent_graph_environment=graph_environment,
                            child_graph_environment=graph.environment_for(child),
                        ))
            else:
                for role, argv in commands.items():
                    process = self.topology.process(role)
                    graph_environment = graph.environment_for(process.role)
                    futures.append(pool.submit(self.run, role, _readiness_wrapper(argv), timeout_s=timeout_s,
                                               graph_environment=graph_environment))
            if real_launcher:
                if not graph.wait_ready(min(timeout_s, 5.0)):
                    completed, _ = wait(futures, timeout=0.1, return_when=FIRST_EXCEPTION)
                    for future in completed:
                        future.result()
                    raise ScorerInputError("scorer topology readiness failed")
                graph.release()
            done, pending = wait(futures, return_when=FIRST_EXCEPTION)
            for future in done:
                future.result()
            if pending:
                # No role failed, so all remaining work must complete normally.
                for future in pending:
                    future.result()
            results = tuple(future.result() for future in futures)
            if real_launcher:
                self.last_graph_result = graph.controller_result()
            self._reap_all()
            return results
        except Exception:
            for future in futures:
                future.cancel()
            self._reap_all()
            raise
        finally:
            self._graph_environment = {}
            graph.close()
            pool.shutdown(wait=False, cancel_futures=True)

    def _reap_all(self) -> None:
        if callable(self.reaper):
            for process in self.topology.processes:
                self.reaper(process.uid)


def _readiness_wrapper(argv: Sequence[str]) -> list[str]:
    """Exec the submitted role only after it joins the authenticated role graph."""
    import_root = str(Path(__file__).resolve().parents[2])
    code = (
        f"import sys; sys.path.insert(0,{import_root!r}); "
        "from implbench.harness.scorer_sandbox import role_graph_request; import os; "
        "role_graph_request('ready'); r=role_graph_request('receive',timeout_ms=5000); "
        "(r.get('message') or {}).get('type')=='graph.start' or sys.exit(125); os.execvp(sys.argv[1],sys.argv[1:])"
    )
    return [sys.executable, "-c", code, *argv]


def reap_and_prove_empty(
    uid: int,
    *,
    list_processes,
    kill_process,
    grace_s: float = 0.25,
) -> None:
    """Kill and independently census a scorer UID before evidence is persisted."""

    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ScorerInputError("scorer UID is invalid")
    processes = tuple(list_processes(uid))
    for process in processes:
        kill_process(process, signal.SIGTERM) if _accepts_signal(kill_process) else kill_process(process)
    deadline = time.monotonic() + grace_s
    remaining = tuple(list_processes(uid))
    while remaining and time.monotonic() < deadline:
        time.sleep(0.01)
        remaining = tuple(list_processes(uid))
    for process in remaining:
        kill_process(process, signal.SIGKILL) if _accepts_signal(kill_process) else kill_process(process)
    remaining = tuple(list_processes(uid))
    if remaining:
        time.sleep(0.01)
        remaining = tuple(list_processes(uid))
    if remaining:
        raise ScorerInputError(f"scorer UID {uid} still owns processes")


def _accepts_signal(callback: Any) -> bool:
    try:
        return len(__import__("inspect").signature(callback).parameters) >= 2
    except (TypeError, ValueError):
        return False


def validate_program_size(root: str | Path, *, max_bytes: int) -> int:
    """Bound submitted code before launch without following links or caches."""

    if max_bytes <= 0:
        raise ScorerInputError("max program bytes must be positive")
    total = 0
    base = Path(root).resolve(strict=True)
    for path in base.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > max_bytes:
            raise ScorerInputError("submitted program byte limit exceeded")
    return total


def validate_g4_receipts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    expected_oids: Sequence[str],
    cell_id: str | None = None,
    attempt_id: str | None = None,
    public_suite_oid: str,
    public_suite_digest: str,
    public_suite_digest_version: str = "public-suite-v1",
    expected_sequences: Mapping[str, int] | None = None,
) -> None:
    """Require one complete authenticated G4 receipt for every imported commit OID."""

    if len(set(expected_oids)) != len(expected_oids) or any(not _OID.fullmatch(oid) for oid in expected_oids):
        raise ScorerInputError("G4 expected import sequence is invalid")
    if not _OID.fullmatch(public_suite_oid) or not _DIGEST.fullmatch(public_suite_digest) or not public_suite_digest_version:
        raise ScorerInputError("G4 expected suite pin is invalid")
    expected = tuple(expected_oids)
    seen: set[str] = set()
    nonces: set[str] = set()
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "cell_id", "attempt_id", "commit_oid", "public_suite_oid", "public_suite_digest",
            "public_suite_digest_version", "outcome_enum", "controller_sequence", "nonce",
        }:
            raise ScorerInputError("G4 receipt schema is not frozen")
        oid = receipt.get("commit_oid", "")
        if not _OID.fullmatch(oid) or oid in seen:
            raise ScorerInputError("G4 receipt OID is invalid or replayed")
        if oid != expected[index]:
            raise ScorerInputError("G4 receipt order does not match imported commits")
        if (receipt.get("public_suite_oid") != public_suite_oid
                or receipt.get("public_suite_digest") != public_suite_digest
                or receipt.get("public_suite_digest_version") != public_suite_digest_version):
            raise ScorerInputError("G4 receipt suite pin mismatch")
        if cell_id is not None and receipt.get("cell_id") != cell_id:
            raise ScorerInputError("G4 receipt cell identity mismatch")
        if attempt_id is not None and receipt.get("attempt_id") != attempt_id:
            raise ScorerInputError("G4 receipt attempt identity mismatch")
        if receipt.get("outcome_enum") not in {"FAIL", "PASS"}:
            raise ScorerInputError("G4 receipt outcome is not closed")
        sequence = receipt.get("controller_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ScorerInputError("G4 receipt sequence is invalid")
        if expected_sequences is not None and sequence != expected_sequences.get(oid):
            raise ScorerInputError("G4 receipt controller sequence mismatch")
        nonce = receipt.get("nonce")
        if not isinstance(nonce, str) or not _DIGEST.fullmatch(nonce) or nonce in nonces:
            raise ScorerInputError("G4 receipt nonce is invalid or replayed")
        seen.add(oid)
        nonces.add(nonce)
    if tuple(receipt["commit_oid"] for receipt in receipts) != expected:
        raise ScorerInputError("G4 receipts do not cover every receipted OID")


class BytecodeOutcome(str, Enum):
    CLEAN = "clean"
    MODEL_G5 = "model-g5"
    INFRASTRUCTURE_UNKNOWN = "infrastructure-unknown"


@dataclass(frozen=True)
class BytecodeEvent:
    role: str
    path: Path
    action: str


def _beneath(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _bytecode_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    if entry.name == "__pycache__" or entry.name.endswith(".pyc"):
                        found.append(Path(entry.path))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == "__pycache__":
                        found.append(Path(entry.path))
                    stack.append(Path(entry.path))
                elif entry.name.endswith(".pyc"):
                    found.append(Path(entry.path))
    return found


def attribute_bytecode(
    sandbox_root: str | Path,
    *,
    imported_tree: str | Path,
    events: Sequence[BytecodeEvent],
) -> BytecodeOutcome:
    """Apply attribution to the complete sandbox, including scratch and temporary paths."""

    root = Path(sandbox_root)
    imported = Path(imported_tree)
    if not root.is_absolute() or not imported.is_absolute():
        raise ScorerInputError("bytecode roots must be absolute")
    if root.is_symlink() or imported.is_symlink():
        return BytecodeOutcome.INFRASTRUCTURE_UNKNOWN
    if not root.exists() and not events:
        return BytecodeOutcome.INFRASTRUCTURE_UNKNOWN
    detected = _bytecode_paths(root)
    detected.extend(Path(event.path) for event in events if Path(event.path).name == "__pycache__" or Path(event.path).suffix == ".pyc")
    if not detected:
        return BytecodeOutcome.CLEAN
    for path in detected:
        if _beneath(path, imported):
            return BytecodeOutcome.MODEL_G5
    for event in events:
        event_path = Path(event.path)
        if event_path not in detected:
            continue
        if event.role in MODEL_ROLES:
            return BytecodeOutcome.MODEL_G5
        if event.role not in INFRASTRUCTURE_ROLES:
            return BytecodeOutcome.INFRASTRUCTURE_UNKNOWN
    return BytecodeOutcome.INFRASTRUCTURE_UNKNOWN


def digest_materialization(path: str | Path) -> str:
    """Return a descriptor-independent helper digest for callers creating test attestations."""

    root = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if item.is_symlink() or item.is_dir():
            continue
        digest.update(item.relative_to(root).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()
