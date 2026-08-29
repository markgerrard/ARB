"""Controller-owned production runtime assembly for the bakeoff CLI.

The manifest is immutable configuration.  This module turns it into the live, controller-owned
maps consumed by the phase runners and binds the one real scored dispatch implementation.  It
does not manufacture readiness or pilot evidence: missing live evidence remains fail-closed when
the corresponding phase is executed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .cell_runtime import ACLIdentity, ACLLifecycle, CellPaths, CellRuntime, IdentityAllocator, PlaneIdentities, PlaneProvisioner, ProcessLedger, ProcessRecord, attempt_id_for, delete_tree_descriptor_safe
from .completion import CompletionVerifier, materialization_digest
from .controller import ScoredCloseRuntime
from .dispatch import ScoredDispatchBinding, run_task
from .git_service import ChildAttemptGitServiceServer, GitService
from .importer import ImportGraphAttestation, attest_imported_graph, import_from_descriptor_child
from .sandbox import LaunchSpec, SandboxPaths, build_launch_spec, generate_profile, verify_launch_spec
from .quarantine import clone_quarantine, export_quarantine
from .receipts import ReceiptChain
from .phases import PilotSeal
from .readiness import GATE_IDS, production_gate_checks
from .schedule import ScheduleCell, cell_suffix, expand_schedule
from .scorer_sandbox import G4ReceiptBinding, PostImportInput, ScorerModelExecutionLimit, ScorerRole, ScorerRunResult, ScorerSandbox, build_g1_topology, build_g4_topology, reap_and_prove_empty, validate_g4_receipts
from .tasks import Task, load_task


class ProductionRuntimeUnavailable(RuntimeError):
    """Raised when required controller-owned configuration is unavailable or invalid."""


_OID40 = re.compile(r"^[0-9a-f]{40}$")


class _FixedIdentityAllocator(IdentityAllocator):
    """Replay a controller-reserved plane identity during restart recovery."""

    def __init__(self, identities: PlaneIdentities):
        self._values = iter((identities.control, identities.tool, identities.git, identities.tool_gid))

    def mint(self, role: str) -> int:
        del role
        value = next(self._values)
        if value is None:
            raise ProductionRuntimeUnavailable("persisted tool-plane GID is missing")
        return value


class _UnavailableIdentityAllocator(IdentityAllocator):
    """Prevents CellRuntime from minting labels before the OS helper has reserved identities."""

    def mint(self, role: str) -> int:
        raise ProductionRuntimeUnavailable(f"real OS identity reservation required for {role}")


class _SystemPlaneProvisioner:
    """Production OS boundary.

    Identity reservation and seat startup are delegated to an operator-installed privileged
    helper.  The helper is a serialized JSON boundary; absent or malformed host authority is a
    hard failure.  There is intentionally no marker, guessed UID, or ambient process fallback.
    """

    real = True

    def __init__(self, *, helper: str | None = None, run_id: str | None = None) -> None:
        repository_helper = Path(__file__).resolve().parents[1] / "plane_helper.py"
        configured = helper or os.environ.get("IMPLBENCH_PLANE_HELPER") or str(repository_helper)
        if not configured or not os.path.isabs(configured):
            raise ProductionRuntimeUnavailable("IMPLBENCH_PLANE_HELPER must name an absolute installed helper")
        path = Path(configured)
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise ProductionRuntimeUnavailable("configured plane helper is not an executable real file")
        repository_owned = path.resolve() == repository_helper
        self._repository_owned = repository_owned
        self.structural_only = repository_owned
        owner_pin = os.environ.get("IMPLBENCH_PLANE_HELPER_OWNER_UID")
        mode_pin = os.environ.get("IMPLBENCH_PLANE_HELPER_MODE")
        digest_pin = os.environ.get("IMPLBENCH_PLANE_HELPER_SHA256")
        try:
            expected_owner = int(owner_pin) if owner_pin is not None else -1
            expected_mode = int(mode_pin, 8) if mode_pin is not None else -1
        except ValueError as exc:
            raise ProductionRuntimeUnavailable("plane helper ownership/mode pins are malformed") from exc
        if repository_owned and owner_pin is None and mode_pin is None and digest_pin is None:
            expected_owner = path.stat().st_uid
            expected_mode = stat.S_IMODE(path.stat().st_mode)
        if expected_owner < 0 or expected_mode < 0 or (not repository_owned and not re.fullmatch(r"[0-9a-f]{64}", digest_pin or "")):
            raise ProductionRuntimeUnavailable("plane helper owner, mode, and digest pins are required")
        try:
            # Keep the authority image open.  Rechecking a pathname immediately before
            # exec still permits a check/launch replacement race.
            helper_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(helper_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("unsafe helper image")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(helper_fd, 131072)
                if not chunk:
                    break
                chunks.append(chunk)
            os.lseek(helper_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256(b"".join(chunks)).hexdigest()
        except OSError as exc:
            raise ProductionRuntimeUnavailable("plane helper authority metadata is unavailable") from exc
        if info.st_uid != expected_owner or stat.S_IMODE(info.st_mode) != expected_mode or (digest_pin is not None and digest != digest_pin):
            os.close(helper_fd)
            raise ProductionRuntimeUnavailable("plane helper authority pin mismatch")
        # macOS does not support executing an open descriptor through /dev/fd.
        # Materialize the verified bytes once into a controller-only directory instead;
        # this is the image that is subsequently executed, never the mutable install path.
        image_dir = Path(tempfile.mkdtemp(prefix="implbench-plane-", dir=str(path.parent)))
        os.chmod(image_dir, 0o700)
        image = image_dir / "helper"
        image_fd = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, expected_mode)
        try:
            os.fchmod(image_fd, expected_mode)
            for chunk in chunks:
                os.write(image_fd, chunk)
            os.fsync(image_fd)
        finally:
            os.close(image_fd)
        os.close(helper_fd)
        self._helper_image_dir = image_dir
        self.helper = image
        self.helper_digest = digest
        self.run_id = run_id or os.environ.get("IMPLBENCH_RUN_ID", "")
        if not isinstance(self.run_id, str) or not self.run_id.startswith("oi-pi-bakeoff-"):
            raise ProductionRuntimeUnavailable("plane helper requires the controller's non-empty scored run ID")

    def __del__(self) -> None:
        image_dir = getattr(self, "_helper_image_dir", None)
        if isinstance(image_dir, Path):
            try:
                (image_dir / "helper").unlink(missing_ok=True)
                image_dir.rmdir()
            except OSError:
                pass

    @staticmethod
    def _census_uid(uid: int) -> set[int]:
        return _SystemProcessTable().census_uid(uid)

    def _call(self, action: str, **payload: Any) -> Mapping[str, Any]:
        required = {"cell_id", "attempt_id", "root"}
        if not required.issubset(payload):
            raise ProductionRuntimeUnavailable("plane helper request is missing attempt binding")
        request = {"version": "implbench-plane-v1", "action": action, "run_id": self.run_id,
                   "nonce": secrets.token_hex(32), **payload}
        request_fields = {
            "reserve": {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root"},
            "provision": {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root", "control_uid", "tool_uid", "git_uid", "tool_gid"},
            "start-seat": {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root", "control_uid", "tool_uid", "git_uid", "tool_gid"},
            "stop-seat": {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root", "control_uid", "tool_uid", "git_uid", "tool_gid"},
            "census": {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root", "uids"},
        }
        if action not in request_fields or set(request) != request_fields[action]:
            raise ProductionRuntimeUnavailable("plane helper request is not closed")
        try:
            result = subprocess.run([str(self.helper)], input=json.dumps(request, sort_keys=True), text=True,
                                    capture_output=True, check=False, shell=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProductionRuntimeUnavailable(f"plane helper {action} unavailable") from exc
        if result.returncode != 0:
            raise ProductionRuntimeUnavailable(f"plane helper {action} failed")
        try:
            response = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable(f"plane helper {action} returned malformed evidence") from exc
        response_fields = {
            "reserve": {"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce", "control_uid", "tool_uid", "git_uid", "tool_gid", "processes"},
            "provision": {"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce"},
            "start-seat": ({"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce", "processes", "endpoints"}
                           if self._repository_owned else {"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce"}),
            "stop-seat": ({"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce", "processes"}
                          if self._repository_owned else {"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce"}),
            "census": {"version", "ok", "action", "run_id", "cell_id", "attempt_id", "root", "nonce", "processes"},
        }
        if (not isinstance(response, Mapping) or set(response) != response_fields[action]
                or response.get("version") != "implbench-plane-v1" or response.get("ok") is not True):
            raise ProductionRuntimeUnavailable(f"plane helper {action} did not acknowledge success")
        for key in ("action", "run_id", "cell_id", "attempt_id", "root", "nonce"):
            if response.get(key) != request[key]:
                raise ProductionRuntimeUnavailable(f"plane helper {action} response binding mismatch")
        return response

    def spawn_child(
        self,
        spec: LaunchSpec,
        *,
        cell_id: str,
        attempt_id: str,
        root: Path,
        pass_fds: tuple[int, ...],
    ) -> subprocess.Popen[bytes]:
        """Ask the pinned plane helper to exec one exact LaunchSpec payload.

        This is intentionally not a controller ``Popen(..., user=...)`` path.  The
        installed, byte-pinned helper consumes this closed request, applies the
        reserved UID/GID and Seatbelt profile, closes every descriptor except the
        listed set, and execs the requested child in place.  Its stdout is therefore
        the child proof channel and its PID is the exact payload PID after exec.
        """

        verify_launch_spec(spec)
        if (not isinstance(cell_id, str) or not cell_id.startswith("cell-")
                or not isinstance(attempt_id, str) or not attempt_id.startswith("attempt-")
                or not root.is_absolute() or root.is_symlink()):
            raise ProductionRuntimeUnavailable("plane helper child launch binding is invalid")
        try:
            spec.cwd.relative_to(root)
        except ValueError as exc:
            raise ProductionRuntimeUnavailable("plane helper child root does not bind its working directory") from exc
        inherited = tuple(pass_fds)
        if inherited != spec.inherited_fds:
            raise ProductionRuntimeUnavailable("plane helper child descriptors do not match LaunchSpec")
        if any(fd < 3 for fd in inherited):
            raise ProductionRuntimeUnavailable("plane helper child descriptor is invalid")
        request = {
            "version": "implbench-plane-v1",
            "action": "launch-child",
            "run_id": self.run_id,
            "cell_id": cell_id,
            "attempt_id": attempt_id,
            "root": str(root),
            "nonce": secrets.token_hex(32),
            "launch": {
                "plane": spec.plane,
                "argv": list(spec.argv),
                "env": dict(spec.env),
                "cwd": str(spec.cwd),
                "profile": spec.profile,
                "profile_digest": spec.profile_digest,
                "template_digest": spec.template_digest,
                "uid": spec.uid,
                "gid": spec.gid,
                "inherited_fds": list(inherited),
                "fresh_context": spec.fresh_context,
                "resume": spec.resume,
                "fork_from": spec.fork_from,
                "warm_process": spec.warm_process,
                "shell": spec.shell,
            },
        }
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 256 * 1024:
            raise ProductionRuntimeUnavailable("plane helper child launch request exceeds its bound")
        request_read, request_write = os.pipe()
        try:
            process = subprocess.Popen(
                [str(self.helper)],
                cwd=str(root),
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=request_read,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                pass_fds=inherited,
                start_new_session=True,
            )
            os.close(request_read)
            request_read = -1
            os.write(request_write, encoded)
            os.close(request_write)
            request_write = -1
            return process
        except OSError as exc:
            raise ProductionRuntimeUnavailable("plane helper child launch is unavailable") from exc
        finally:
            if request_read >= 0:
                os.close(request_read)
            if request_write >= 0:
                os.close(request_write)

    def reserve_identities(self, cell_id: str, *, attempt_id: str, root: str | Path | None = None) -> PlaneIdentities:
        if root is None:
            raise ProductionRuntimeUnavailable("plane helper reservation requires a canonical root")
        root_path = Path(root)
        if not root_path.is_absolute() or root_path.is_symlink():
            raise ProductionRuntimeUnavailable("plane helper reservation root is not canonical")
        row = self._call("reserve", cell_id=cell_id, attempt_id=attempt_id, root=str(root_path))
        if row.get("processes") != [] or row.get("census") not in (None, []):
            raise ProductionRuntimeUnavailable("plane helper reserved identities are not empty")
        try:
            values = PlaneIdentities(int(row["control_uid"]), int(row["tool_uid"]), int(row["git_uid"]), int(row["tool_gid"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionRuntimeUnavailable("plane helper returned invalid OS identities") from exc
        if len({values.control, values.tool, values.git, values.tool_gid}) != 4:
            raise ProductionRuntimeUnavailable("plane helper returned colliding OS identities")
        if any(value <= 0 for value in (values.control, values.tool, values.git, values.tool_gid)):
            raise ProductionRuntimeUnavailable("plane helper returned invalid OS identities")
        # Helper JSON is only a claim.  The controller's OS table decides whether
        # reservation is actually clean, including identities omitted by a helper.
        if any(self._census_uid(uid) for uid in (values.control, values.tool, values.git)):
            raise ProductionRuntimeUnavailable("plane helper reserved identities are not empty")
        return values

    def provision_planes(self, paths: CellPaths, identities: PlaneIdentities, *, attempt_id: str) -> None:
        self._call("provision", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root),
                   control_uid=identities.control, tool_uid=identities.tool, git_uid=identities.git, tool_gid=identities.tool_gid)

    def start_seat_daemon(self, paths: CellPaths, identities: PlaneIdentities, *, attempt_id: str) -> None:
        row = self._call("start-seat", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root),
                         control_uid=identities.control, tool_uid=identities.tool, git_uid=identities.git, tool_gid=identities.tool_gid)
        if not self._repository_owned:
            return
        processes = row.get("processes")
        endpoints = row.get("endpoints")
        expected = {"control": identities.control, "tool": identities.tool}
        if (not isinstance(processes, list) or not isinstance(endpoints, Mapping)
                or {item.get("role"): item.get("requested_uid") for item in processes if isinstance(item, Mapping)} != expected
                or set(endpoints) != set(expected)):
            raise ProductionRuntimeUnavailable("plane helper did not prove both seat processes")
        for item in processes:
            if (not isinstance(item, Mapping) or not isinstance(item.get("pid"), int)
                    or not isinstance(item.get("effective_uid"), int)
                    or item.get("setuid_attempted") is not True
                    or not isinstance(item.get("setuid_succeeded"), bool)
                    or item.get("executable") != str(self.helper)
                    or item.get("executable_digest") != self.helper_digest):
                raise ProductionRuntimeUnavailable("plane helper seat exec evidence is malformed")
        for role, endpoint in endpoints.items():
            expected_tag = hashlib.sha256(
                (self.run_id + "\0" + paths.cell_id + "\0" + attempt_id + "\0" + role).encode()
            ).hexdigest()[:24]
            expected_endpoint = str(Path(tempfile.gettempdir()) / f"implbench-p-{expected_tag}.sock")
            if not isinstance(endpoint, str) or endpoint != expected_endpoint:
                raise ProductionRuntimeUnavailable("plane helper returned an unbound seat endpoint")
            self._probe_seat_endpoint(endpoint, role)

    def stop_seat_daemon(self, paths: CellPaths, identities: PlaneIdentities, *, attempt_id: str) -> None:
        row = self._call("stop-seat", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root),
                         control_uid=identities.control, tool_uid=identities.tool, git_uid=identities.git, tool_gid=identities.tool_gid)
        if self._repository_owned and row.get("processes") != []:
            raise ProductionRuntimeUnavailable("plane helper stop left a live seat process")

    def prove_absent(self, paths: CellPaths, identities: PlaneIdentities, *, attempt_id: str) -> bool:
        row = self._call("census", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root),
                         uids=[identities.control, identities.tool, identities.git])
        if row.get("processes") != []:
            return False
        return not any(self._census_uid(uid) for uid in (identities.control, identities.tool, identities.git))

    @staticmethod
    def _probe_seat_endpoint(endpoint: str, role: str) -> None:
        """Require the repository helper's fixed seat protocol, not a path marker."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2.0)
                connection.connect(endpoint)
                connection.sendall(json.dumps({"protocol": "implbench-plane-seat-v1", "op": "ping"}, sort_keys=True).encode("utf-8"))
                raw = connection.recv(512)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable("plane seat IPC probe failed") from exc
        if (not isinstance(value, Mapping) or set(value) != {"protocol", "role", "pid"}
                or value.get("protocol") != "implbench-plane-seat-v1" or value.get("role") != role
                or not isinstance(value.get("pid"), int) or value["pid"] <= 1):
            raise ProductionRuntimeUnavailable("plane seat IPC response is malformed")


class ValkeyACLBackend:
    """Managed Valkey ACL lifecycle; never a local dictionary substitute."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("ARB_MEMORY_REDIS_URL")
        if not self.url:
            raise ProductionRuntimeUnavailable("ARB_MEMORY_REDIS_URL is required for managed ACLs")
        try:
            import redis
            self.client = redis.Redis.from_url(self.url, decode_responses=True)
            self.client.ping()
            self._user_clients: dict[str, Any] = {}
        except Exception as exc:
            raise ProductionRuntimeUnavailable("managed Valkey ACL client is unavailable") from exc

    def provision(self, identity: ACLIdentity) -> None:
        self.client.execute_command("ACL", "SETUSER", identity.user, "reset", "on", ">" + identity.password,
                                    "resetkeys", "resetchannels", f"~{identity.prefix}:*",
                                    "+ping", "+select", "+get", "+set", "+del", "+exists")
        import redis
        user_client = redis.Redis.from_url(self.url, username=identity.user, password=identity.password, decode_responses=True)
        if not user_client.ping():
            raise ProductionRuntimeUnavailable("managed ACL user did not authenticate")
        self._user_clients[identity.user] = user_client

    def namespace_keys(self, prefix: str) -> set[str]:
        return set(self.client.scan_iter(match=f"{prefix}:*"))

    def cross_prefix_probe(self, user: str, prefix: str, forbidden_prefix: str | None = None) -> bool:
        probe = f"{forbidden_prefix or (prefix + '-forbidden')}:cross-prefix-probe"
        try:
            self._user_clients[user].set(probe, "1")
            return True
        except Exception as exc:
            # Only Redis' explicit ACL denial proves isolation.  Authentication,
            # transport and malformed-client failures are infrastructure faults.
            from redis.exceptions import NoPermissionError

            if isinstance(exc, NoPermissionError):
                return False
            raise ProductionRuntimeUnavailable("ACL isolation probe did not receive NOPERM") from exc

    def disable_user(self, user: str) -> None:
        self.client.execute_command("ACL", "SETUSER", user, "off")

    def kill_clients(self, user: str) -> None:
        try:
            self.client.execute_command("CLIENT", "KILL", "USER", user)
        finally:
            self._user_clients.pop(user, None)

    def delete_prefix(self, prefix: str) -> None:
        keys = tuple(self.client.scan_iter(match=f"{prefix}:*"))
        if keys:
            self.client.delete(*keys)

    def delete_user(self, user: str) -> None:
        self.client.execute_command("ACL", "DELUSER", user)

    def authenticate(self, user: str, password: str) -> bool:
        try:
            probe = __import__("redis").Redis.from_url(self.url, username=user, password=password, decode_responses=True)
            return bool(probe.ping())
        except Exception:
            return False


def _fsync_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable("persisted cell identities are unreadable") from exc
        if not isinstance(previous, Mapping) or any(previous.get(key) != row for key, row in value.items() if key in previous):
            raise ProductionRuntimeUnavailable("persisted cell allocation changed")
        if previous == value:
            return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    if temporary.exists():
        raise ProductionRuntimeUnavailable("persisted cell allocation has an active writer")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_controller_secret(path: Path, *, attempt_id: str) -> dict[str, str]:
    """Read controller state through stable no-follow descriptors only."""

    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise ProductionRuntimeUnavailable("controller secret state has unsafe ownership or permissions")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeUnavailable("controller secret state is unreadable") from exc
    required = ("acl_user", "acl_prefix", "acl_password", "receipt_key")
    if not isinstance(value, Mapping) or value.get("attempt_id") != attempt_id or not all(isinstance(value.get(key), str) and value[key] for key in required):
        raise ProductionRuntimeUnavailable("controller secret state is not attempt-bound or is incomplete")
    return {key: str(value[key]) for key in required}


def _copy_descriptor_tree(source: Path, destination: Path, *, max_bytes: int = 512 * 1024 * 1024) -> int:
    """Copy a Git object tree using only descriptor-relative, no-follow operations."""

    if not source.is_absolute() or not destination.is_absolute():
        raise ProductionRuntimeUnavailable("descriptor snapshot paths must be absolute")
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    copied = 0

    def walk(src_fd: int, dst: Path) -> None:
        nonlocal copied
        with os.scandir(src_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            name = entry.name
            if stat.S_ISLNK(info.st_mode):
                raise ProductionRuntimeUnavailable(f"descriptor snapshot rejects symlink: {name}")
            if stat.S_ISDIR(info.st_mode):
                child_src = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
                child_dst = dst / name
                child_dst.mkdir(mode=0o700, exist_ok=False)
                try:
                    walk(child_src, child_dst)
                finally:
                    os.close(child_src)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ProductionRuntimeUnavailable(f"descriptor snapshot rejects unsafe entry: {name}")
            if info.st_size < 0 or copied + info.st_size > max_bytes:
                raise ProductionRuntimeUnavailable("descriptor snapshot byte limit exceeded")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
            out_fd = -1
            try:
                before = os.fstat(fd)
                if (before.st_ino, before.st_dev, before.st_mode, before.st_nlink, before.st_size) != (info.st_ino, info.st_dev, info.st_mode, info.st_nlink, info.st_size):
                    raise ProductionRuntimeUnavailable("descriptor snapshot entry changed before copy")
                out = dst / name
                out_fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                remaining = before.st_size
                while remaining:
                    chunk = os.read(fd, min(131072, remaining))
                    if not chunk:
                        raise ProductionRuntimeUnavailable("descriptor snapshot source truncated")
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(out_fd, chunk[offset:])
                    copied += len(chunk)
                    remaining -= len(chunk)
                    if copied > max_bytes:
                        raise ProductionRuntimeUnavailable("descriptor snapshot byte limit exceeded")
                after = os.fstat(fd)
                if (after.st_ino, after.st_dev, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns) != (before.st_ino, before.st_dev, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns):
                    raise ProductionRuntimeUnavailable("descriptor snapshot entry changed during copy")
                os.fsync(out_fd)
            finally:
                if out_fd >= 0:
                    os.close(out_fd)
                os.close(fd)

    try:
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        walk(source_fd, destination)
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(source_fd)
    return copied


class _PersistentIdentityStore:
    """Controller-owned, append-once allocation map; never derives IDs from ambient GID."""

    def __init__(self, root: Path):
        self.path = root / "preflight" / "cell-identities.json"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProductionRuntimeUnavailable("persisted cell identities are unreadable") from exc
            if not isinstance(loaded, dict):
                raise ProductionRuntimeUnavailable("persisted cell identities are malformed")
            self._values = loaded
        else:
            self._values = {}
        used = [int(value) for row in self._values.values() if isinstance(row, Mapping) for value in row.values() if isinstance(value, int)]
        self._next = max([os.getuid() + 1000, os.getgid() + 1000, *used], default=1000) + 1

    @staticmethod
    def _key(cell_id: str, attempt_id: str | None = None) -> str:
        return f"{cell_id}:{attempt_id}" if attempt_id is not None else cell_id

    def get(self, cell_id: str, attempt_id: str | None = None) -> PlaneIdentities:
        key = self._key(cell_id, attempt_id)
        row = self._values.get(key)
        if isinstance(row, Mapping):
            try:
                identity = PlaneIdentities(int(row["control"]), int(row["tool"]), int(row["git"]), int(row["tool_gid"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductionRuntimeUnavailable("persisted cell identity is malformed") from exc
            if len({identity.control, identity.tool, identity.git, identity.tool_gid}) != 4:
                raise ProductionRuntimeUnavailable("persisted cell identity collides")
            return identity
        raise ProductionRuntimeUnavailable("no controller-synthetic identity exists; reserve real OS identities first")

    def put(self, cell_id: str, identity: PlaneIdentities, attempt_id: str | None = None) -> None:
        key = self._key(cell_id, attempt_id)
        values = {
            "control": identity.control,
            "tool": identity.tool,
            "git": identity.git,
            "tool_gid": identity.tool_gid,
        }
        existing = self._values.get(key)
        if existing is not None and dict(existing) != values:
            raise ProductionRuntimeUnavailable("controller-owned cell identity changed")
        self._values[key] = values
        _fsync_json(self.path, self._values)


class _SystemProcessTable:
    """Independent OS process census for the controller-reserved plane UIDs."""

    def census_uid(self, uid: int) -> set[int]:
        result = subprocess.run(["ps", "-axo", "pid=,uid="], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ProductionRuntimeUnavailable("process census failed")
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) == uid and fields[0].isdigit():
                pids.add(int(fields[0]))
        return pids

    def signal(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return

    def wait_uid_empty(self, uid: int, grace_s: float) -> None:
        deadline = time.monotonic() + grace_s
        while self.census_uid(uid) and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


class _ProductionCell:
    """One disposable cell: real lifecycle, persistent identities, and descriptor-backed close."""

    def __init__(self, controller: "_ProductionController", cell: ScheduleCell, *, attempt_id: str | None = None):
        self.controller = controller
        self.cell = cell
        self.attempt_id: str | None = attempt_id
        self.paths = (
            CellPaths.for_attempt(controller.run_id, cell.cell_id, attempt_id, root=controller.cell_root_base)
            if attempt_id is not None
            else CellPaths.for_run(controller.run_id, cell.cell_id, root=controller.cell_root_base)
        )
        self.identities: PlaneIdentities | None = None
        self.runtime = CellRuntime(
            self.paths,
            allocator=_FixedIdentityAllocator(self.identities) if self.identities is not None else _UnavailableIdentityAllocator(),
            processes=ProcessLedger(_SystemProcessTable()),
            provisioner=controller.provisioner,
            attempt_id=attempt_id,
            acl=controller.acl,
            require_provisioner=bool(getattr(controller.provisioner, "real", False)),
        )
        self.repo: Path | None = None
        self.receipt_chain: ReceiptChain | None = None
        self.git_service: GitService | None = None
        self.git_rpc_server: ChildAttemptGitServiceServer | None = None
        self.control_process: subprocess.Popen[bytes] | None = None
        self.tool_process: subprocess.Popen[bytes] | None = None
        self.control_endpoint: Path | None = None
        self.tool_endpoint: Path | None = None
        self.plane_launch_evidence: dict[str, Mapping[str, Any]] = {}
        self.receipt_key: bytes | None = None
        self._status: Mapping[str, Any] | None = None
        self._events: list[str] = []

    def _secret_material(self, attempt_id: str) -> dict[str, str]:
        """Load or mint controller-only recovery secrets; never derive them from IDs."""
        # The cell runtime is mounted into the Git-service profile for its own
        # short-lived scratch files.  Controller recovery secrets must therefore
        # live beside the cell, never beneath that mount.  This path is also needed
        # before ``CellRuntime.allocate`` creates the disposable cell root.
        path = self.paths.run_root / "controller-secrets" / f"{self.paths.cell_id}.{attempt_id}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            return _read_controller_secret(path, attempt_id=attempt_id)
        suffix = secrets.token_hex(16)
        value = {"attempt_id": attempt_id, "acl_user": f"implbench-cell-{suffix}",
                 "acl_prefix": f"implbench:{suffix}",
                 "acl_password": secrets.token_urlsafe(32),
                 "receipt_key": secrets.token_hex(32)}
        _fsync_json(path, value)
        return _read_controller_secret(path, attempt_id=attempt_id)

    @property
    def tool_gid(self) -> int:
        if self.identities is None or self.identities.tool_gid is None:
            raise ProductionRuntimeUnavailable("cell has no reserved tool identity")
        return self.identities.tool_gid

    def prepare(self, *, attempt_id: str | None = None) -> "_ProductionCell":
        if attempt_id is not None:
            if not attempt_id.startswith("attempt-"):
                raise ProductionRuntimeUnavailable("production cell requires a canonical attempt identity")
            self.attempt_id = attempt_id
            self.runtime.attempt_id = attempt_id
            if self.runtime.state == "NEW":
                self.runtime.journal = self.runtime.journal.__class__(
                    self.paths.run_root / f"{self.paths.cell_id}.{attempt_id}.lifecycle.ndjson"
                )
            if self.runtime.state == "NEW" and getattr(self.controller.provisioner, "real", False):
                reserve = getattr(self.controller.provisioner, "reserve_identities", None)
                if not callable(reserve):
                    raise ProductionRuntimeUnavailable("controller-owned identity reservation is unavailable")
                try:
                    reserved = reserve(self.cell.cell_id, attempt_id=attempt_id, root=self.paths.cell_root)
                except TypeError:
                    reserved = reserve(self.cell.cell_id, attempt_id=attempt_id)
                if not isinstance(reserved, PlaneIdentities):
                    raise ProductionRuntimeUnavailable("controller-owned identity reservation is malformed")
                self.identities = reserved
                self.controller.identity_store.put(self.cell.cell_id, reserved, attempt_id)
                self.runtime.allocator = _FixedIdentityAllocator(reserved)
                material = self._secret_material(attempt_id)
                self.runtime.acl_identity = ACLIdentity(material["acl_user"], material["acl_prefix"], material["acl_password"])
        if self.runtime.state == "NEW":
            if self.paths.cell_root.exists() and not self.paths.cell_root.is_symlink():
                if attempt_id is not None:
                    committed = {
                        row.get("action")
                        for row in self.runtime.journal.read()
                        if row.get("status") == "committed"
                    }
                    if "plane-provisioning" not in committed or "dispatch" not in committed:
                        raise ProductionRuntimeUnavailable("persisted cell root is not bound to this scored attempt")
                if self.paths.cell_root.stat().st_mode & 0o777 != 0o700:
                    raise ProductionRuntimeUnavailable("persisted cell root has unsafe permissions")
                required_dirs = (
                    self.paths.control_home, self.paths.tool_home, self.paths.git_home,
                    self.paths.config_root, self.paths.bus_namespace, self.paths.runtime,
                )
                if any(not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o777 != 0o700 for path in required_dirs):
                    raise ProductionRuntimeUnavailable("persisted cell provisioning is incomplete")
                self.runtime.identities = self.identities
                if self.runtime.acl_identity is None:
                    material = self._secret_material(self.attempt_id or "")
                    self.runtime.acl_identity = ACLIdentity(material["acl_user"], material["acl_prefix"], material["acl_password"])
                    if self.runtime.acl is not None:
                        self.runtime.acl.provision(self.runtime.acl_identity)
                self.runtime.state = "PREFLIGHTED"
            else:
                self.runtime.allocate()
                self.runtime.provision()
        if attempt_id is not None:
            self.attempt_id = attempt_id
        return self

    def ensure_clone(self) -> Path:
        self.prepare(attempt_id=self.attempt_id)
        if self.repo is not None:
            return self.repo
        repo = self.paths.cell_root / "repo"
        export = self.paths.cell_root / "export.git"
        if repo.exists() and not repo.is_symlink() and repo.is_dir():
            self.repo = repo
            return repo
        controller_repo = self.controller._minimal_controller_repo(self.cell)
        if not export.exists():
            export_quarantine(controller_repo, export, base_oid=self.controller.manifest["base_sha"], fixture_oid=self.controller.fixture_root_oid_for_cell(self.cell))
        clone_quarantine(export, repo)
        self._git(repo, "checkout", "--detach", "refs/arb-export/fixture")
        self.repo = repo
        return repo

    def bind_receipts(self, attempt_id: str, *, allowed_paths: tuple[str, ...]) -> None:
        self.prepare(attempt_id=attempt_id)
        if self.repo is None:
            self.ensure_clone()
        if self.receipt_chain is not None:
            return
        self.receipt_key = bytes.fromhex(self._secret_material(attempt_id)["receipt_key"])
        identity = self.controller.receipt_identity(self.cell, attempt_id)
        if not attempt_id.startswith("attempt-"):
            raise ProductionRuntimeUnavailable("receipt chain requires a canonical attempt identity")
        log_path = self.paths.runtime / "attempts" / attempt_id / "receipts.ndjson"
        self.receipt_chain = ReceiptChain(log_path, self.receipt_key, identity=identity, fixture_root_oid=self.controller.fixture_root_oid_for_cell(self.cell), allowed_paths=allowed_paths)
        self.git_service = GitService(
            self.repo,
            fixture_root_oid=self.controller.fixture_root_oid_for_cell(self.cell),
            allowed_paths=allowed_paths,
            receipt_chain=self.receipt_chain,
            completion_provider=self.completion_projection,
            scored=True,
            tool_gid=self.tool_gid,
        )

    def open_attempt_git_service(self, attempt_id: str, *, allowed_paths: tuple[str, ...]) -> Mapping[str, str]:
        """Start the controller-owned RPC before dispatch and return envelope fields."""
        self.bind_receipts(attempt_id, allowed_paths=allowed_paths)
        if self.git_service is None:
            raise ProductionRuntimeUnavailable("attempt Git service was not bound")
        if self.git_rpc_server is not None:
            raise ProductionRuntimeUnavailable("attempt Git service is already open")
        if self.identities is None:
            raise ProductionRuntimeUnavailable("attempt Git service has no provisioned bridge identity")
        socket_name = "implbench-g-" + hashlib.sha256(
            f"{self.controller.run_id}\0{self.cell.cell_id}\0{attempt_id}".encode("utf-8")
        ).hexdigest()[:24] + ".sock"
        service_socket = Path(tempfile.gettempdir()) / socket_name
        git_runtime = self.paths.git_home / "runtime"
        git_runtime.mkdir(mode=0o700, exist_ok=True)
        runtime_info = git_runtime.lstat()
        if (not stat.S_ISDIR(runtime_info.st_mode) or runtime_info.st_uid != os.getuid()
                or stat.S_IMODE(runtime_info.st_mode) != 0o700):
            raise ProductionRuntimeUnavailable("Git-service runtime is not controller-owned mode 0700")
        paths = SandboxPaths(
            cell_root=self.paths.cell_root, worktree=self.repo or self.paths.cell_root,
            git_dir=(self.repo or self.paths.cell_root) / ".git", evidence_root=self.controller.evidence_root,
            base_checkout=self.controller.repo, sibling_worktree=self.paths.cell_root / "sibling-denied",
            credential_root=self.paths.config_root, key_root=self.paths.run_root / "controller-secrets",
            home=self.paths.git_home, runtime=git_runtime, service_socket=service_socket,
        )
        spec = build_launch_spec("git-service", paths, uid=self.identities.git, gid=self.identities.tool_gid,
                                 argv=("git-service-child",), git_socket=service_socket,
                                 extra_env={"PYTHONPATH": str(Path(__file__).resolve().parents[2])})
        helper_spawn = getattr(self.controller.provisioner, "spawn_child", None)
        if not callable(helper_spawn):
            raise ProductionRuntimeUnavailable("verified plane helper child launcher is unavailable")

        def spawn(specification: LaunchSpec, fds: tuple[int, ...]) -> subprocess.Popen[bytes]:
            return helper_spawn(
                specification, cell_id=self.cell.cell_id, attempt_id=attempt_id,
                root=self.paths.cell_root, pass_fds=fds,
            )

        structural_only = bool(getattr(self.controller.provisioner, "structural_only", False))
        server = ChildAttemptGitServiceServer(
            self.git_service, root=self.paths.cell_root, attempt_id=attempt_id,
            tool_gid=self.identities.tool_gid,
            peer_uids=(self.identities.control, self.identities.tool, *((os.getuid(),) if structural_only else ())),
            launch_spec=spec, receipt_chain=self.receipt_chain, child_spawner=spawn,
            census_uid=_SystemProcessTable().census_uid, structural_identity=structural_only,
        )
        try:
            fields = server.start()
        except Exception as exc:
            raise ProductionRuntimeUnavailable("attempt Git service could not start") from exc
        self.git_rpc_server = server
        return fields

    @staticmethod
    def _authority_descriptor(value: Mapping[str, Any]) -> int:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ProductionRuntimeUnavailable("plane authority descriptor exceeds its bound")
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, encoded)
        except Exception:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        return read_fd

    @staticmethod
    def _plane_rpc(endpoint: Path, protocol: str, op: str) -> Mapping[str, Any]:
        request = json.dumps(
            {"protocol": protocol, "op": op}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(1.0)
                    connection.connect(str(endpoint))
                    connection.sendall(request)
                    raw = b""
                    while not raw.endswith(b"\n") and len(raw) <= 1024 * 1024:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                response = json.loads(raw)
                if (
                    not isinstance(response, Mapping)
                    or set(response) != {"ok", "result", "error"}
                    or response.get("ok") is not True
                    or not isinstance(response.get("result"), Mapping)
                ):
                    raise ValueError("plane probe response is malformed")
                return response["result"]
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.02)
        raise ProductionRuntimeUnavailable("production plane endpoint did not become ready") from last_error

    def start_attempt_planes(self, remote_binding: Mapping[str, str]) -> Mapping[str, Any]:
        """Fork/exec the actual one-attempt tool broker and bridge control process."""

        if self.runtime.state != "PREFLIGHTED" or self.repo is None or self.identities is None or self.attempt_id is None:
            raise ProductionRuntimeUnavailable("attempt planes require a preflighted scored cell")
        if set(remote_binding) != {"endpoint", "capability"}:
            raise ProductionRuntimeUnavailable("attempt Git authority is malformed")
        helper_spawn = getattr(self.controller.provisioner, "spawn_child", None)
        if not callable(helper_spawn):
            raise ProductionRuntimeUnavailable("verified plane helper child launcher is unavailable")

        entry = self.repo / "bench" / "implbench" / "scored_plane_entry.py"
        if not entry.is_file() or entry.is_symlink():
            raise ProductionRuntimeUnavailable("scored plane production entry is unavailable in the frozen cell")
        tag = hashlib.sha256(
            f"{self.controller.run_id}\0{self.cell.cell_id}\0{self.attempt_id}".encode()
        ).hexdigest()[:24]
        self.tool_endpoint = Path(tempfile.gettempdir()) / f"implbench-t-{tag}.sock"
        self.control_endpoint = Path(tempfile.gettempdir()) / f"implbench-c-{tag}.sock"
        tool_runtime = self.paths.tool_home / "runtime"
        control_runtime = self.paths.control_home / "runtime"
        for root in (tool_runtime, control_runtime):
            root.mkdir(mode=0o700, exist_ok=False)
            (root / "tmp").mkdir(mode=0o700, exist_ok=False)

        shared = dict(
            cell_root=self.paths.cell_root,
            worktree=self.repo,
            git_dir=self.repo / ".git",
            evidence_root=self.controller.evidence_root,
            base_checkout=self.controller.repo,
            sibling_worktree=self.paths.cell_root / "sibling-denied",
            credential_root=self.paths.config_root,
            key_root=self.paths.run_root / "controller-secrets",
        )
        tool_paths = SandboxPaths(
            **shared, home=self.paths.tool_home, runtime=tool_runtime,
            service_socket=self.tool_endpoint,
        )
        control_paths = SandboxPaths(
            **shared, home=self.paths.control_home, runtime=control_runtime,
            service_socket=self.control_endpoint,
        )
        arm = self.controller.arms.get(self.cell.arm)
        if not isinstance(arm, Mapping):
            raise ProductionRuntimeUnavailable("scored arm configuration is unavailable")
        provider_endpoint = self.controller.provider_endpoint_for_cell(self.cell)
        bus_endpoint = self.controller.bus_endpoint_for_cell(self.cell)
        config_payload: dict[str, Any] = {
            "schema": "implbench-control-config-v1",
            "run_id": self.controller.run_id,
            "cell_id": self.cell.cell_id,
            "attempt_id": self.attempt_id,
            "arm": self.cell.arm,
            "engine": arm.get("engine"),
            "provider": arm.get("provider"),
            "model": arm.get("model"),
            "harness": arm.get("harness"),
            "workdir": str(self.repo),
            "interpreter_bin": os.environ.get("IMPLBENCH_INTERPRETER_BIN"),
            "interpreter_sha256": os.environ.get("IMPLBENCH_INTERPRETER_SHA256"),
        }
        config_payload["config_digest"] = hashlib.sha256(_canonical(config_payload)).hexdigest()
        secret_fd = self.controller.open_control_secret_for_cell(self.cell)
        descriptors = [secret_fd]
        try:
            tool_fd = self._authority_descriptor({
                "git_endpoint": remote_binding["endpoint"],
                "git_capability": remote_binding["capability"],
                "socket_gid": self.tool_gid,
                "cell_id": self.cell.cell_id,
                "attempt_id": self.attempt_id,
                "workdir": str(self.repo),
            })
            descriptors.append(tool_fd)
            control_fd = self._authority_descriptor({
                "tool_endpoint": str(self.tool_endpoint),
                "socket_gid": self.tool_gid,
                "cell_id": self.cell.cell_id,
                "attempt_id": self.attempt_id,
            })
            descriptors.append(control_fd)
            config_fd = self._authority_descriptor(config_payload)
            descriptors.append(config_fd)
            self.control_endpoint.unlink(missing_ok=True)
            control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            control_listener.bind(str(self.control_endpoint))
            os.chmod(self.control_endpoint, 0o600)
            control_listener.listen(8)
            control_fds = tuple(sorted((control_fd, config_fd, secret_fd, control_listener.fileno())))
            tool_spec = build_launch_spec(
                "tool", tool_paths, uid=self.identities.tool, gid=self.tool_gid,
                argv=(sys.executable, "-u", str(entry), "tool", "--authority-fd", str(tool_fd),
                      "--endpoint", str(self.tool_endpoint)),
                git_socket=Path(remote_binding["endpoint"]), inherited_fds=(tool_fd,),
            )
            control_spec = build_launch_spec(
                "control", control_paths, uid=self.identities.control, gid=self.tool_gid,
                argv=(sys.executable, "-u", str(entry), "control", "--authority-fd", str(control_fd),
                      "--config-fd", str(config_fd), "--secret-fd", str(secret_fd),
                      "--listener-fd", str(control_listener.fileno()),
                      "--endpoint", str(self.control_endpoint)),
                provider_endpoint=provider_endpoint, bus_endpoint=bus_endpoint,
                inherited_fds=control_fds,
            )
            self.runtime.journal.append("dispatch", "prepared", attempt_id=self.attempt_id)
        except Exception:
            listener = locals().get("control_listener")
            if isinstance(listener, socket.socket):
                listener.close()
            if self.control_endpoint is not None:
                self.control_endpoint.unlink(missing_ok=True)
            for descriptor in descriptors:
                os.close(descriptor)
            raise
        def terminate_spawned_planes() -> None:
            nonlocal control_listener
            if control_listener is not None:
                control_listener.close()
                control_listener = None
            for process in (self.control_process, self.tool_process):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            for endpoint in (self.control_endpoint, self.tool_endpoint):
                if endpoint is not None:
                    endpoint.unlink(missing_ok=True)

        try:
            self.tool_process = helper_spawn(
                tool_spec, cell_id=self.cell.cell_id, attempt_id=self.attempt_id,
                root=self.paths.cell_root, pass_fds=(tool_fd,),
            )
            tool_probe = self._plane_rpc(self.tool_endpoint, "implbench-tool-plane-v1", "ping")
            self.control_process = helper_spawn(
                control_spec, cell_id=self.cell.cell_id, attempt_id=self.attempt_id,
                root=self.paths.cell_root, pass_fds=control_fds,
            )
            control_listener.close()
            control_listener = None
            control_probe = self._plane_rpc(self.control_endpoint, "implbench-control-plane-v1", "ping")
        except Exception:
            terminate_spawned_planes()
            raise
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        try:
            expected = {
                "tool": (self.tool_process, self.identities.tool, tool_probe),
                "control": (self.control_process, self.identities.control, control_probe),
            }
            for role, (process, _uid, probe) in expected.items():
                if process is None or probe.get("pid") != process.pid or process.pid == os.getpid():
                    raise ProductionRuntimeUnavailable(f"{role} plane exec evidence is invalid")
                if role == "control" and (
                    probe.get("config_digest") != config_payload["config_digest"]
                    or probe.get("secret_descriptor_consumed") is not True
                    or not isinstance(probe.get("secret_names"), list)
                    or not probe["secret_names"]
                ):
                    raise ProductionRuntimeUnavailable("control plane config/secret evidence is invalid")
            for role, (process, uid, _probe) in expected.items():
                assert process is not None
                self.runtime.processes.register(ProcessRecord(
                    process.pid, uid, os.getpgid(process.pid), os.getsid(process.pid), role,
                ))
            self.plane_launch_evidence = {
                "tool": {"pid": self.tool_process.pid, "launch": tool_spec, "probe": tool_probe},
                "control": {"pid": self.control_process.pid, "launch": control_spec, "probe": control_probe},
            }
            self.runtime.journal.append("dispatch", "committed", attempt_id=self.attempt_id)
            self.runtime.state = "DISPATCHED"
        except Exception:
            terminate_spawned_planes()
            raise
        return {"control_endpoint": str(self.control_endpoint), "processes": self.plane_launch_evidence}

    def dispatch_through_control(self, task: Task, engine: str, *, timeout: int) -> Mapping[str, Any]:
        if self.control_endpoint is None:
            raise ProductionRuntimeUnavailable("scored control plane is not running")
        arm = self.controller.arms.get(self.cell.arm)
        if not isinstance(arm, Mapping) or engine != arm.get("engine"):
            raise ProductionRuntimeUnavailable("scored engine does not match the frozen arm config")
        payload = {
            "task": task.brief,
            "timeout": timeout,
        }
        request = json.dumps(
            {"protocol": "implbench-control-plane-v1", "op": "run", "payload": payload},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout + 10)
                connection.connect(str(self.control_endpoint))
                connection.sendall(request)
                raw = b""
                while not raw.endswith(b"\n") and len(raw) <= 1024 * 1024:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
            response = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable("scored control plane dispatch failed") from exc
        if not isinstance(response, Mapping) or response.get("ok") is not True or not isinstance(response.get("result"), Mapping):
            raise ProductionRuntimeUnavailable(str(response.get("error") if isinstance(response, Mapping) else "scored control response malformed"))
        return response["result"]

    def close_attempt_git_service(self) -> None:
        if self.git_rpc_server is not None:
            server = self.git_rpc_server
            try:
                self._status = server.status()
            except Exception:
                # A final status query is itself a Git-plane operation.  Do not
                # fall back to controller-side Git if the child has failed.
                self._status = {"status": "UNAVAILABLE", "reason": "git-service-status"}
                if self.git_service is not None:
                    self.git_service._infrastructure_failure = "git-service-status"
            finally:
                server.close()
                self.git_rpc_server = None

    def completion_projection(self) -> dict[str, Any]:
        records = self.receipt_chain._rows() if self.receipt_chain is not None else []
        status = self._status
        return {
            "mode": "receipt-only",
            "ref_namespace": "cell-attempt",
            "receipt_oids": [row.get("payload", {}).get("commit_oid") for row in records if row.get("record_type") == "git-receipt"],
            "receipt_records": records,
            "status": status,
            "dirty": bool(status.get("dirty")) if isinstance(status, Mapping) else False,
            "seal_complete": status is not None,
            "receipts_authenticated": self.receipt_chain is not None,
            "source_descriptor": str(self.repo) if self.repo is not None else None,
            "infrastructure_failure": getattr(self.git_service, "_infrastructure_failure", None),
        }

    def descriptor_root(self, status: Mapping[str, Any]) -> Path:
        """Capture the closed Git object directory behind a descriptor-only import boundary."""

        if self.repo is None or not isinstance(status.get("head"), str) or not _OID40.fullmatch(status["head"]):
            raise ProductionRuntimeUnavailable("closed Git descriptor is unavailable")
        source = self.repo / ".git"
        if not source.is_dir() or source.is_symlink():
            raise ProductionRuntimeUnavailable("scored cell Git directory is not a real directory")
        descriptor = self.paths.runtime / "descriptor"
        ref = descriptor / "refs" / "implbench" / "candidate"
        for stale_stage in self.paths.runtime.glob(".descriptor-stage-*"):
            delete_tree_descriptor_safe(stale_stage)
        if descriptor.is_dir() and not descriptor.is_symlink():
            try:
                if ref.is_file() and ref.read_text(encoding="ascii").strip() == status["head"]:
                    return descriptor
            except (OSError, UnicodeError):
                pass
            delete_tree_descriptor_safe(descriptor)
        for attempt in range(2):
            stage = self.paths.runtime / f".descriptor-stage-{os.getpid()}-{time.time_ns()}"
            try:
                stage.mkdir(mode=0o700, parents=False, exist_ok=False)
                _copy_descriptor_tree(source / "objects", stage / "objects")
                stage_ref = stage / "refs" / "implbench" / "candidate"
                stage_ref.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
                fd = os.open(stage_ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, (status["head"] + "\n").encode("ascii"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                runtime_fd = os.open(self.paths.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(runtime_fd)
                finally:
                    os.close(runtime_fd)
                os.replace(stage, descriptor)
                runtime_fd = os.open(self.paths.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(runtime_fd)
                finally:
                    os.close(runtime_fd)
                if ref.read_text(encoding="ascii").strip() != status["head"]:
                    raise ProductionRuntimeUnavailable("closed Git descriptor candidate ref changed")
                return descriptor
            except Exception as exc:  # policy and mutation failures must clean the stage now
                delete_tree_descriptor_safe(stage)
                retryable = isinstance(exc, (OSError, UnicodeError))
                if not retryable or attempt == 1:
                    if isinstance(exc, ProductionRuntimeUnavailable):
                        raise
                    raise ProductionRuntimeUnavailable("closed Git descriptor snapshot failed") from exc
        raise ProductionRuntimeUnavailable("closed Git descriptor snapshot failed")

    def import_descriptor_child(self, source_fd: int, _destination: str | Path, candidate_ref: str | None) -> Any:
        """Invoke the post-close importer through its own bounded OS process."""

        if self.identities is None:
            raise ProductionRuntimeUnavailable("importer has no reserved Git identity")
        root = self.paths.runtime / "importer"
        for path in (root, root / "home", root / "runtime", root / "work"):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths = SandboxPaths(
            cell_root=root,
            worktree=root / "work",
            git_dir=self.paths.git_home,
            evidence_root=self.controller.evidence_root,
            base_checkout=self.controller.repo,
            sibling_worktree=self.paths.cell_root / "sibling-denied",
            credential_root=self.paths.config_root,
            key_root=root / "controller-key-denied",
            home=root / "home",
            runtime=root / "runtime",
        )
        spec = build_launch_spec(
            "importer", paths, uid=self.identities.git, gid=self.identities.tool_gid,
            argv=("importer-child",),
            # This is a fixed, non-secret import root needed only when the hermetic
            # test runner has not installed the bench package.  Production's helper
            # pins the interpreter image and need not use this test-only value.
            extra_env={"PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
        helper_spawn = getattr(self.controller.provisioner, "spawn_child", None)
        if not callable(helper_spawn) or self.attempt_id is None:
            raise ProductionRuntimeUnavailable("verified plane helper importer launcher is unavailable")

        def spawn(specification: LaunchSpec, fds: tuple[int, ...]) -> subprocess.Popen[bytes]:
            return helper_spawn(
                specification, cell_id=self.cell.cell_id, attempt_id=self.attempt_id,
                root=self.paths.cell_root, pass_fds=fds,
            )

        try:
            structural_only = bool(getattr(self.controller.provisioner, "structural_only", False))
            result, evidence = import_from_descriptor_child(
                source_fd, root / "runtime" / "bundle", launch_spec=spec,
                candidate_ref=candidate_ref,
                allow_unprofiled_test=False,
                structural_identity=structural_only,
                child_spawner=spawn,
            )
        except Exception as exc:
            raise ProductionRuntimeUnavailable("importer child boundary failed") from exc
        expected_uid = os.getuid() if structural_only else self.identities.git
        if evidence.get("pid") == os.getpid() or evidence.get("uid") != expected_uid:
            raise ProductionRuntimeUnavailable("importer child identity evidence is invalid")
        return result

    def _record(self, action: str) -> None:
        self._events.append(action)

    def _commit_record(self, action: str) -> None:
        self.runtime.journal.append(f"close:{action}", "committed")

    @staticmethod
    def _stop_plane_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def stop_tools(self) -> None:
        self._record("stop_tools")
        self._stop_plane_process(self.tool_process)
        self.runtime.processes.close((self.identities.tool,), grace_s=1.0)
        self._commit_record("stop_tools")
    def drain_rpc(self) -> None:
        self._record("drain_rpc")
        self._stop_plane_process(self.control_process)
        if self.control_endpoint is not None:
            self.control_endpoint.unlink(missing_ok=True)
        self.runtime.processes.close((self.identities.control,), grace_s=0.0)
        self._commit_record("drain_rpc")
    def kill_planes(self) -> None:
        self._record("kill_planes")
        self.runtime.processes.close(tuple(self.identities), grace_s=1.0)
        self._commit_record("kill_planes")
    def close_acl(self) -> None:
        self._record("close_acl")
        if self.runtime.acl is not None and self.runtime.acl_identity is not None:
            self.runtime._close_acl()
        self._commit_record("close_acl")
    def final_status(self) -> Mapping[str, Any]:
        self._record("final_status")
        if self.git_service is None:
            self._status = {"status": "UNAVAILABLE", "reason": "git-service-not-started"}
        elif self._status is None:
            self._status = {"status": "UNAVAILABLE", "reason": "git-service-status"}
        self._commit_record("final_status")
        return self._status
    def kill_git(self) -> None:
        self._record("kill_git")
        self.runtime.processes.close((self.identities.git,), grace_s=0.0)
        self._commit_record("kill_git")
    def census_snapshot(self) -> None:
        self._record("census_snapshot")
        self.runtime.processes.close(tuple(self.identities), grace_s=0.0)
        self._commit_record("census_snapshot")
    def append_post_g4_attestation(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.receipt_chain is None:
            raise ProductionRuntimeUnavailable("receipt chain is unavailable for post-G4 attestation")
        return self.receipt_chain.append_post_g4_attestation(payload)
    def append_pre_scorer_attestation(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.receipt_chain is None:
            raise ProductionRuntimeUnavailable("receipt chain is unavailable for pre-scorer attestation")
        return self.receipt_chain.append_pre_scorer_attestation(payload)
    def append_g4_receipt(self, payload: Mapping[str, Any]) -> None:
        if self.receipt_chain is None:
            raise ProductionRuntimeUnavailable("receipt chain is unavailable for G4 receipt")
        self.receipt_chain.append_g4_receipt(payload)
    def g4_receipt_bindings(self, completion: Mapping[str, Any], attestation: Mapping[str, Any]) -> tuple[G4ReceiptBinding, ...]:
        """Derive stable, controller-authenticated G4 nonces across close recovery."""
        if self.receipt_chain is None or self.receipt_key is None:
            raise ProductionRuntimeUnavailable("receipt chain is unavailable for G4 receipt bindings")
        public_oid = attestation.get("public_suite_oid")
        public_digest = attestation.get("public_suite_digest")
        public_version = attestation.get("public_suite_digest_version")
        rows = completion.get("receipts")
        if (not isinstance(public_oid, str) or not isinstance(public_digest, str)
                or not isinstance(public_version, str) or not isinstance(rows, list)):
            raise ProductionRuntimeUnavailable("G4 receipt binding inputs are unavailable")
        bindings: list[G4ReceiptBinding] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ProductionRuntimeUnavailable("G4 receipt binding input is malformed")
            try:
                unsigned = {
                    "cell_id": self.cell.cell_id, "attempt_id": self.attempt_id,
                    "commit_oid": row["commit_oid"], "public_suite_oid": public_oid,
                    "public_suite_digest": public_digest,
                    "public_suite_digest_version": public_version,
                    "controller_sequence": row["controller_sequence"],
                }
                nonce = hmac.new(self.receipt_key, _canonical(unsigned), hashlib.sha256).hexdigest()
                binding = G4ReceiptBinding(**unsigned, nonce=nonce)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductionRuntimeUnavailable("G4 receipt binding input is invalid") from exc
            # An fsynced row is reusable only when every fixed field and the
            # controller-derived nonce are exact.  Altered durable evidence blocks
            # recovery rather than receiving a newly minted nonce.
            self.receipt_chain.validate_g4_binding(binding)
            bindings.append(binding)
        return tuple(bindings)
    def environment_manifest_digest(self) -> str:
        return hashlib.sha256(self.controller.manifest_bytes).hexdigest()
    def destroy(self) -> None:
        self._record("destroy")
        try:
            self.runtime.destroy()
        finally:
            self.receipt_chain = None
            self.git_service = None
            self.receipt_key = None
            self.repo = None
            self._status = None
            self.runtime.acl_identity = None
            self.runtime.identities = None
        self._commit_record("destroy")

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ProductionRuntimeUnavailable(result.stderr.strip() or "cell Git operation failed")


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class _ProductionController:
    """Concrete controller configuration and live factory maps for one immutable run."""

    def __init__(self, manifest: Mapping[str, Any], *, provisioner: PlaneProvisioner | None = None, acl: Any | None = None) -> None:
        if not isinstance(manifest, Mapping):
            raise ProductionRuntimeUnavailable("production manifest is not a mapping")
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("oi-pi-bakeoff-"):
            raise ProductionRuntimeUnavailable("live production runtime: production manifest has no scored run ID")
        source = manifest.get("source")
        source_root = source.get("realpath") if isinstance(source, Mapping) else None
        if not isinstance(source_root, str) or not os.path.isabs(source_root):
            raise ProductionRuntimeUnavailable("production controller source root is missing")
        self.run_id = run_id
        self.manifest = dict(manifest)
        self.repo = Path(source_root)
        if not self.repo.is_absolute() or self.repo.is_symlink() or not self.repo.is_dir():
            raise ProductionRuntimeUnavailable("production controller source root must be a real absolute directory")
        if self.repo.resolve(strict=True) != self.repo:
            raise ProductionRuntimeUnavailable("production controller source root is not canonical")

        task_rows = manifest.get("tasks")
        if not isinstance(task_rows, (tuple, list)):
            raise ProductionRuntimeUnavailable("production manifest task map is missing")
        self._task_rows = {row.get("task_id"): row for row in task_rows if isinstance(row, Mapping)}
        self.tasks: dict[str, Task] = {}
        for task_id, row in self._task_rows.items():
            if not isinstance(task_id, str) or not isinstance(row.get("fixture_sha"), str):
                raise ProductionRuntimeUnavailable("production task pin is malformed")
            task_path = self.repo / "bench" / "implbench" / "fixtures" / task_id / "task.yaml"
            try:
                task = load_task(task_path)
            except Exception as exc:  # noqa: BLE001 - configuration cannot become a fake task
                raise ProductionRuntimeUnavailable(f"production task {task_id} is unavailable") from exc
            if task.task_id != task_id:
                raise ProductionRuntimeUnavailable(f"production task identity mismatch: {task_id}")
            self.tasks[task_id] = task

        seed = manifest.get("seed")
        if not isinstance(seed, str):
            raise ProductionRuntimeUnavailable("production schedule seed is missing")
        task_pins = [(task_id, str(row["fixture_sha"])) for task_id, row in self._task_rows.items()]
        try:
            expanded = expand_schedule(seed, task_pins)
        except Exception as exc:  # noqa: BLE001 - schedule drift is a configuration failure
            raise ProductionRuntimeUnavailable("production schedule cannot be expanded") from exc
        stored = manifest.get("schedule")
        if stored is not None and _plain(stored) != [cell.as_dict() for cell in expanded]:
            raise ProductionRuntimeUnavailable("production schedule does not match manifest pins")
        self.cells: dict[str, ScheduleCell] = {cell.cell_id: cell for cell in expanded}

        arms = manifest.get("arms")
        if not isinstance(arms, (tuple, list)):
            raise ProductionRuntimeUnavailable("production arm map is missing")
        self.arms = {row.get("arm"): row for row in arms if isinstance(row, Mapping)}
        if set(self.arms) != {"glm-pi", "glm-zcode", "kimi-pi", "kimi-cli"}:
            raise ProductionRuntimeUnavailable("production arm map is incomplete")

        evidence = manifest.get("evidence")
        evidence_root = evidence.get("root") if isinstance(evidence, Mapping) else None
        if not isinstance(evidence_root, str) or not os.path.isabs(evidence_root):
            raise ProductionRuntimeUnavailable("production evidence root is missing")
        self.evidence_root = Path(evidence_root)
        self.runtime_root = self.evidence_root
        configured_cell_root = Path(os.environ.get("IMPLBENCH_CELL_ROOT_BASE", "/Users/Shared/arb-implbench"))
        if not configured_cell_root.is_absolute():
            raise ProductionRuntimeUnavailable("production cell root base must be absolute")
        try:
            self.cell_root_base = configured_cell_root.resolve(strict=False)
        except OSError as exc:
            raise ProductionRuntimeUnavailable("production cell root base is unavailable") from exc
        if self.cell_root_base != configured_cell_root:
            raise ProductionRuntimeUnavailable("production cell root base must be canonical")
        if (
            self.cell_root_base == self.evidence_root
            or self.cell_root_base.is_relative_to(self.evidence_root)
            or self.evidence_root.is_relative_to(self.cell_root_base)
        ):
            raise ProductionRuntimeUnavailable("production cell roots must be separate from evidence")
        self.identity_store = _PersistentIdentityStore(self.runtime_root)
        self.provisioner = provisioner or _SystemPlaneProvisioner(run_id=self.run_id)
        self.acl = acl or ACLLifecycle(ValkeyACLBackend())
        self._cells: dict[str, _ProductionCell] = {}
        self._controller_repos: dict[str, Path] = {}
        self.scored_scorer_factory: Any | None = None
        self.live_handles = {
            "repo": self.repo,
            "tasks": self.tasks,
            "arms": self.arms,
            "cells": self.cells,
            "evidence_root": self.evidence_root,
            "cell_root_base": self.cell_root_base,
        }
        self.factory_maps = {
            "task": self.task_for_cell,
            "seat": self.seat_for_cell,
            "engine": self.engine_for_cell,
            "fixture_root_oid": self.fixture_root_oid_for_cell,
            "tool_gid": self.tool_gid_for_cell,
            "cell_root": self.cell_root_for_cell,
            "lifecycle": self.cell_for_cell,
            "scored_runtime": self.scored_runtime_factory,
        }
        # This is deliberately the module function, never a manifest-provided callable.
        self.dispatch_fn = run_task
        self.gate_checks = production_gate_checks(manifest)
        self.manifest_bytes = _canonical(manifest)
        config = {name: manifest.get(name) for name in ("runtime", "pins", "controls", "budgets", "git_rpc")}
        self.config_bytes = _canonical(config)
        self.refs = self._read_refs()
        self.journal_tail = self._read_journal_tail()
        self.pilot_seal = self._read_pilot_seal()
        self.final_index_present = (self.evidence_root / "git-refs.txt").is_file()
        self.max_same_cause_failures = 3

    def _read_refs(self) -> tuple[tuple[str, str], ...]:
        path = self.evidence_root / "pilot-refs.json"
        if not path.is_file():
            return ()
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable("pilot refs are unreadable") from exc
        if not isinstance(rows, list):
            raise ProductionRuntimeUnavailable("pilot refs are malformed")
        result: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("ref"), str) or not isinstance(row.get("oid"), str) or not _OID40.fullmatch(row["oid"]):
                raise ProductionRuntimeUnavailable("pilot refs contain malformed identity")
            result.append((row["ref"], row["oid"]))
        return tuple(result)

    def _read_journal_tail(self) -> bytes:
        path = self.evidence_root / "pilot-journal.ndjson"
        if not path.is_file():
            return b""
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ProductionRuntimeUnavailable("pilot journal is unreadable") from exc

    def _read_pilot_seal(self) -> PilotSeal | None:
        path = self.evidence_root / "pilot-seal.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("pilot seal is not an object")
            seal = PilotSeal(
                str(value["digest"]),
                bool(value.get("final_index_present", False)),
                bytes.fromhex(str(value.get("manifest_bytes_hex", ""))),
                bytes.fromhex(str(value.get("config_bytes_hex", ""))),
                tuple((str(row[0]), str(row[1])) for row in value.get("refs", [])),
                bytes.fromhex(str(value.get("journal_tail_hex", ""))),
                (),
                str(value.get("manifest_identity_digest", "")),
            )
            seal.validate()
            return seal
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise ProductionRuntimeUnavailable("pilot seal is malformed") from exc

    def task_for_cell(self, cell: ScheduleCell) -> Task:
        self._require_cell(cell)
        try:
            return self.tasks[cell.task_id]
        except KeyError as exc:
            raise ProductionRuntimeUnavailable("scheduled task is not controller-owned") from exc

    def receipt_identity(self, cell: ScheduleCell, attempt_id: str) -> dict[str, Any]:
        """Build the controller-owned, schema-complete envelope for receipt fsync.

        The Git child receives only the three routing fields it needs.  The full
        record identity and every digest remain controller material until
        ``ReceiptChain.append`` authenticates the candidate.
        """

        self._require_cell(cell)
        arm = self.arms.get(cell.arm)
        controls = self.manifest.get("controls")
        source = self.manifest.get("source")
        capabilities = self.manifest.get("capabilities")
        if not isinstance(arm, Mapping) or not isinstance(controls, Mapping) or not isinstance(source, Mapping) or not isinstance(capabilities, Mapping):
            raise ProductionRuntimeUnavailable("receipt identity inputs are unavailable")
        reasoning = controls.get("reasoning")
        record_controls = {name: dict(value) for name, value in controls.items() if name != "reasoning" and isinstance(value, Mapping)}
        expected_controls = {
            "temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior",
            "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts",
        }
        if set(record_controls) != expected_controls or not isinstance(reasoning, Mapping):
            raise ProductionRuntimeUnavailable("receipt controls are malformed")
        model = arm.get("model")
        engine = arm.get("engine")
        harness = arm.get("harness")
        corpus = self.manifest.get("corpus_sha")
        source_commit = source.get("commit")
        if not all(isinstance(value, str) and value for value in (model, engine, harness, corpus, source_commit)):
            raise ProductionRuntimeUnavailable("receipt provenance is incomplete")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "run_id": self.run_id, "cell_id": cell.cell_id, "attempt_id": attempt_id,
            "pair": cell.pair, "arm": cell.arm, "task": cell.task_id, "repetition": cell.repetition,
            "schedule_index": cell.schedule_index, "fixture_sha": cell.fixture_sha,
            "model_declared": model, "model_verified_via": "controller-pinned-manifest",
            "engine_version": engine, "harness_version": harness + "@" + source_commit,
            "corpus_version": corpus, "config_digest": hashlib.sha256(self.config_bytes).hexdigest(),
            "capability_manifest_digest": hashlib.sha256(_canonical(capabilities)).hexdigest(),
            "reasoning_requested": reasoning.get("requested"), "reasoning_effective": reasoning.get("effective"),
            "reasoning_verified_via": reasoning.get("verified_via"), "started_at": stamp, "ended_at": stamp,
            "wall_time_s": 0, "terminal_status": "unknown", "retry_count": 0, "tool_call_count": 0,
            "schema_version": "record-v2", "prior_record_digest": None, "controls": record_controls,
        }

    def seat_for_cell(self, cell: ScheduleCell) -> str:
        self._require_cell(cell)
        arm = self.arms.get(cell.arm)
        prefix = arm.get("agent_prefix") if isinstance(arm, Mapping) else None
        if not isinstance(prefix, str) or not prefix:
            raise ProductionRuntimeUnavailable("arm agent prefix is missing")
        return f"{prefix}-{cell_suffix(cell.cell_id)}"

    def engine_for_cell(self, cell: ScheduleCell) -> str:
        self._require_cell(cell)
        engine = self.arms[cell.arm].get("engine")
        if not isinstance(engine, str) or not engine:
            raise ProductionRuntimeUnavailable("arm engine is missing")
        return engine

    def provider_endpoint_for_cell(self, cell: ScheduleCell) -> str:
        self._require_cell(cell)
        arm = self.arms.get(cell.arm)
        provider = arm.get("provider") if isinstance(arm, Mapping) else None
        key = "IMPLBENCH_PROVIDER_ENDPOINT_" + re.sub(r"[^A-Z0-9]", "_", str(provider).upper())
        value = os.environ.get(key)
        if not isinstance(value, str) or not value:
            raise ProductionRuntimeUnavailable(f"production provider endpoint is unavailable: {key}")
        return value

    def open_control_secret_for_cell(self, cell: ScheduleCell) -> int:
        """Open one opaque arm secret descriptor without reading it in the controller."""

        self._require_cell(cell)
        key = "IMPLBENCH_CONTROL_SECRET_" + re.sub(r"[^A-Z0-9]", "_", cell.arm.upper())
        raw = os.environ.get(key)
        if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
            raise ProductionRuntimeUnavailable(f"control secret descriptor is unavailable: {key}")
        path = Path(raw)
        try:
            info = path.lstat()
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise ProductionRuntimeUnavailable("control secret descriptor cannot be inspected") from exc
        if (
            path.is_symlink()
            or canonical != path
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ProductionRuntimeUnavailable("control secret descriptor must be controller-owned mode 0600")
        forbidden = (self.repo.resolve(), self.evidence_root.resolve(), self.cell_root_base.resolve())
        if any(canonical == root or canonical.is_relative_to(root) for root in forbidden):
            raise ProductionRuntimeUnavailable("control secret descriptor is inside a forbidden runtime/repository root")
        try:
            return os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ProductionRuntimeUnavailable("control secret descriptor cannot be opened") from exc

    def bus_endpoint_for_cell(self, cell: ScheduleCell) -> str:
        self._require_cell(cell)
        value = os.environ.get("IMPLBENCH_BUS_ENDPOINT")
        if not isinstance(value, str) or not value:
            raise ProductionRuntimeUnavailable("production bus endpoint is unavailable: IMPLBENCH_BUS_ENDPOINT")
        return value

    def fixture_root_oid_for_cell(self, cell: ScheduleCell) -> str:
        self._require_cell(cell)
        fixture = self._task_rows.get(cell.task_id, {}).get("fixture_sha")
        if not isinstance(fixture, str) or not _OID40.fullmatch(fixture):
            raise ProductionRuntimeUnavailable("fixture root pin is not a Git commit OID")
        return fixture

    def tool_gid_for_cell(self, cell: ScheduleCell, attempt_id: str | None = None) -> int:
        self._require_cell(cell)
        attempt_id = attempt_id or attempt_id_for(cell.cell_id, 1)
        return self.identity_store.get(cell.cell_id, attempt_id).tool_gid or 0

    def _cell_for_cell(self, cell: ScheduleCell, attempt_id: str | None = None) -> _ProductionCell:
        self._require_cell(cell)
        current = self._cells.get(cell.cell_id)
        replacing = current is not None and attempt_id is not None and current.attempt_id not in {None, attempt_id}
        if replacing:
            # Attempt identity is a serialized lifecycle boundary: the old seat is stopped,
            # reaped, ACL-retired, census-proved, and destroyed before controller state moves.
            assert current is not None
            current.runtime.close(grace_s=1.0)
        if current is None or current.runtime.state == "DESTROYED" or replacing:
            current = _ProductionCell(self, cell, attempt_id=attempt_id)
            self._cells[cell.cell_id] = current
        return current

    def cell_for_cell(self, cell: ScheduleCell, attempt_id: str | None = None) -> _ProductionCell:
        """Return the controller-owned lifecycle handle used by close recovery."""

        if attempt_id is None:
            raise ProductionRuntimeUnavailable("production lifecycle requires an attempt identity")
        current = self._cell_for_cell(cell, attempt_id)
        current.prepare(attempt_id=attempt_id)
        return current

    def cell_root_for_cell(self, cell: ScheduleCell, attempt_id: str | None = None) -> Path:
        """Provision and return the disposable scored repository."""

        if attempt_id is None:
            attempt_id = attempt_id_for(cell.cell_id, 1)
        current = self._cell_for_cell(cell, attempt_id)
        root = current.ensure_clone()
        task = self.task_for_cell(cell)
        current.bind_receipts(attempt_id, allowed_paths=task.allowed_paths)
        return root

    def _minimal_controller_repo(self, cell: ScheduleCell) -> Path:
        """Build the exact two-tip source used by the quarantine export boundary."""

        fixture = self.fixture_root_oid_for_cell(cell)
        cached = self._controller_repos.get(fixture)
        if cached is not None:
            return cached
        destination = self.runtime_root / "preflight" / "controller-repos" / f"{fixture}.git"
        if destination.exists():
            self._controller_repos[fixture] = destination
            return destination
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        result = subprocess.run(
            ["git", "init", "--bare", "--ref-format=files", str(destination)],
            capture_output=True, text=True, check=False, env=env,
        )
        if result.returncode:
            raise ProductionRuntimeUnavailable(result.stderr.strip() or "controller Git repository setup failed")
        base = self.manifest.get("base_sha")
        if not isinstance(base, str) or not _OID40.fullmatch(base):
            raise ProductionRuntimeUnavailable("controller quarantine roots are not pinned")
        fetch = subprocess.run(
            ["git", "-C", str(destination), "fetch", "--no-tags", str(self.repo),
             f"{base}:refs/arb-export/base", f"{fixture}:refs/arb-export/fixture"],
            capture_output=True, text=True, check=False, env=env,
        )
        if fetch.returncode:
            shutil.rmtree(destination, ignore_errors=True)
            raise ProductionRuntimeUnavailable(fetch.stderr.strip() or "controller quarantine roots unavailable")
        self._controller_repos[fixture] = destination
        return destination

    def recorder_for_cell(self, cell: ScheduleCell, attempt_id: str | None = None) -> Any:
        if not isinstance(attempt_id, str) or not attempt_id.startswith("attempt-"):
            raise ProductionRuntimeUnavailable("production recorder requires an attempt identity")
        path = self.evidence_root / "cell-journals" / cell.cell_id / f"{attempt_id}.ndjson"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return SimpleNamespace(path=path)

    def scored_runtime_factory(self, **kwargs: Any) -> Any:
        """Bind a close runtime only when the controller has a complete receipt descriptor.

        A missing descriptor is not replaced with a fake scorer.  Empty receipts correctly take
        the non-delivery path; a non-empty submission is classified as infrastructure UNKNOWN by
        ``run_task`` if its trusted import inputs are absent.
        """

        completion = kwargs.get("completion")
        cell_id = kwargs.get("cell_id")
        attempt_id = kwargs.get("attempt_id")
        if not isinstance(completion, Mapping) or not isinstance(cell_id, str) or not isinstance(attempt_id, str):
            raise ProductionRuntimeUnavailable("scored completion descriptor is incomplete")
        cell = self._cells.get(cell_id)
        if cell is None:
            raise ProductionRuntimeUnavailable("scored completion is for an unprovisioned cell")
        if cell.attempt_id != attempt_id:
            raise ProductionRuntimeUnavailable("scored completion attempt does not match the active cell")
        task = self.task_for_cell(cell.cell)
        records = completion.get("receipt_records")
        status = completion.get("status")
        if not isinstance(records, list) or not isinstance(status, Mapping):
            raise ProductionRuntimeUnavailable("scored completion lacks authenticated receipt descriptors")
        cell.bind_receipts(attempt_id, allowed_paths=task.allowed_paths)
        descriptor = cell.descriptor_root(status)
        source_fd = os.open(descriptor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        scorer_factory = self.scored_scorer_factory
        if not callable(scorer_factory):
            os.close(source_fd)
            raise ProductionRuntimeUnavailable("controller-owned scorer is not bound")

        def attest(imported: Any, verified: Any) -> Any:
            value = attest_imported_graph(
                imported,
                fixture_root_oid=self.fixture_root_oid_for_cell(cell.cell),
                receipts=verified.payload["receipts"],
                allowed_paths=task.allowed_paths,
            )
            pins = self.manifest.get("pins") if isinstance(self.manifest, Mapping) else None
            public_pin = pins.get("public_suite") if isinstance(pins, Mapping) else None
            digest = public_pin.get("digest") if isinstance(public_pin, Mapping) else None
            if isinstance(digest, str) and digest.startswith("sha256:"):
                digest = digest.removeprefix("sha256:")
            oid = os.environ.get("IMPLBENCH_PUBLIC_SUITE_OID")
            version = public_pin.get("digest_version") if isinstance(public_pin, Mapping) else None
            if not isinstance(value, ImportGraphAttestation) or not isinstance(oid, str) or not isinstance(digest, str) or not isinstance(version, str):
                raise ProductionRuntimeUnavailable("controller-owned public suite binding is unavailable")
            return {
                "attested": value.attested,
                "imported_graph_digest": value.imported_graph_digest,
                "object_ids": value.object_ids,
                "materialization": value.materialization,
                "materialization_digest": value.materialization_digest,
                "public_suite_oid": oid,
                "public_suite_digest": digest,
                "public_suite_digest_version": version,
            }

        try:
            return ScoredCloseRuntime.from_descriptor(
                completion_verifier=CompletionVerifier(
                    cell.receipt_key or b"",
                    identity={"run_id": self.run_id, "cell_id": cell_id, "attempt_id": attempt_id},
                    fixture_root_oid=self.fixture_root_oid_for_cell(cell.cell),
                ),
                source_fd=source_fd,
                import_destination=self.runtime_root / "imports" / cell_id / attempt_id,
                candidate_ref="refs/implbench/candidate",
                importer_runner=getattr(cell, "import_descriptor_child", None),
                attestation_verifier=attest,
                scorer=lambda post_import, attestation: scorer_factory(post_import, attestation),
                receipts=[row for row in records if isinstance(row, Mapping) and row.get("record_type") == "git-receipt"],
                status=dict(status),
                worktree=cell.repo or self.cell_root_for_cell(cell.cell),
                lifecycle=cell,
            )
        finally:
            os.close(source_fd)

    def close_cell(self, outcome: Any) -> None:
        self._append_json("cells.ndjson", outcome)

    def append_attempt(self, outcome: Any) -> None:
        self._append_json("attempts.ndjson", outcome)

    def _append_json(self, name: str, value: Any) -> None:
        if self.final_index_present:
            raise ProductionRuntimeUnavailable("sealed evidence package is immutable")
        if not self.evidence_root.exists():
            return
        self.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.evidence_root / name
        payload = _canonical(vars(value) if hasattr(value, "__dict__") else value) + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def stop_observation(self, _cell: ScheduleCell) -> Mapping[str, Any]:
        return {}

    def freeze_final(self, outcomes: tuple[Any, ...]) -> None:
        self._append_json("final-outcomes.ndjson", list(outcomes))

    def validate(self, manifest: Mapping[str, Any]) -> None:
        if _canonical(manifest) != self.manifest_bytes:
            raise ProductionRuntimeUnavailable("production manifest changed after binding")

    def hermetic_suite(self, _manifest: Mapping[str, Any]) -> Any:
        raise ProductionRuntimeUnavailable("hermetic readiness evidence is not bound")

    def adversarial_validation(self, _manifest: Mapping[str, Any]) -> Any:
        raise ProductionRuntimeUnavailable("adversarial readiness evidence is not bound")

    def known_good(self, _manifest: Mapping[str, Any], _cluster: str, _cell_factory: Any) -> Any:
        raise ProductionRuntimeUnavailable("known-good calibration seat is not bound")

    def unscored(self, _manifest: Mapping[str, Any], _task: str, _arm: str, _cell_factory: Any) -> Any:
        raise ProductionRuntimeUnavailable("unscored calibration seat is not bound")

    def cell_factory(self) -> Any:
        def make_cell(cell: ScheduleCell) -> _ProductionCell:
            if not isinstance(cell, ScheduleCell):
                raise ProductionRuntimeUnavailable("cell identity is required")
            return self.cell_for_cell(cell)

        return make_cell

    def known_good_calibration(self, manifest: Mapping[str, Any], cell_factory: Any) -> Any:
        from .controller import production_known_good_calibration

        return production_known_good_calibration(manifest, cell_factory)

    def execute(self, cell: ScheduleCell, attempt_id: str) -> Any:
        return self.scored_dispatch(cell, attempt_id)

    def _require_cell(self, cell: ScheduleCell) -> None:
        if not isinstance(cell, ScheduleCell) or self.cells.get(cell.cell_id) != cell:
            raise ProductionRuntimeUnavailable("cell is not in the immutable controller schedule")


def build_production_scorer(manifest: Mapping[str, Any], *, structural_identity: bool = False) -> Any:
    """Bind the pinned external scorer, or fail closed when it is unavailable."""

    pins = manifest.get("pins") if isinstance(manifest, Mapping) else None
    scorer_pin = pins.get("scorer") if isinstance(pins, Mapping) else None
    binary = os.environ.get("IMPLBENCH_SCORER_BIN")

    def unavailable(_post_import: Any, _attestation: Any) -> Any:
        raise ProductionRuntimeUnavailable("controller-owned production scorer is unavailable")

    if not isinstance(binary, str) or not binary:
        return unavailable
    path = Path(binary)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        return unavailable
    if not isinstance(scorer_pin, Mapping):
        return unavailable
    expected_digest = scorer_pin.get("digest")
    if isinstance(expected_digest, str) and expected_digest.startswith("sha256:"):
        expected_digest = expected_digest.removeprefix("sha256:")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        return unavailable
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
        return unavailable
    expected_version = scorer_pin.get("version")
    if not isinstance(expected_version, str) or not expected_version:
        return unavailable
    try:
        version = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, check=False, timeout=10,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return unavailable
    if version.returncode != 0 or version.stdout.strip() != expected_version:
        return unavailable

    def score(post_import: Any, attestation: Any) -> Any:
        if not isinstance(post_import, PostImportInput):
            raise ProductionRuntimeUnavailable("scorer input is not the attested post-import boundary")
        trusted_input = post_import
        public_pin = pins.get("public_suite") if isinstance(pins, Mapping) else None
        public_digest = public_pin.get("digest") if isinstance(public_pin, Mapping) else None
        if isinstance(public_digest, str) and public_digest.startswith("sha256:"):
            public_digest = public_digest.removeprefix("sha256:")
        public_oid = attestation.get("public_suite_oid") if isinstance(attestation, Mapping) else None
        public_oid = public_oid or os.environ.get("IMPLBENCH_PUBLIC_SUITE_OID")
        public_digest_version = public_pin.get("digest_version") if isinstance(public_pin, Mapping) else None
        if not isinstance(public_oid, str) or not isinstance(public_digest, str) or not isinstance(public_digest_version, str) or not public_digest_version:
            raise ProductionRuntimeUnavailable("public suite pin is unavailable for G4")
        completion = attestation.get("completion") if isinstance(attestation, Mapping) else None
        if not isinstance(completion, Mapping):
            raise ProductionRuntimeUnavailable("scorer is missing authenticated completion evidence")
        receipt_rows = completion.get("receipts")
        cell_id = completion.get("cell_id")
        attempt_id = completion.get("attempt_id")
        if (not isinstance(receipt_rows, list) or not receipt_rows or not isinstance(cell_id, str)
                or not isinstance(attempt_id, str)):
            raise ProductionRuntimeUnavailable("scorer completion evidence is incomplete")
        supplied_bindings = attestation.get("g4_receipt_bindings") if isinstance(attestation, Mapping) else None
        if not isinstance(supplied_bindings, (tuple, list)):
            raise ProductionRuntimeUnavailable("controller-owned scorer receipt bindings are unavailable")
        scorer_root: Path | None = None
        try:
            g4_bindings = tuple(supplied_bindings)
            if any(not isinstance(item, G4ReceiptBinding) for item in g4_bindings):
                raise ValueError("binding type")
        except (TypeError, ValueError) as exc:
            raise ProductionRuntimeUnavailable("scorer receipt bindings are invalid") from exc
        expected = tuple((row.get("commit_oid"), row.get("controller_sequence")) for row in receipt_rows if isinstance(row, Mapping))
        actual = tuple((item.commit_oid, item.controller_sequence) for item in g4_bindings)
        if len(expected) != len(receipt_rows) or actual != expected or any(
            item.cell_id != cell_id or item.attempt_id != attempt_id
            or item.public_suite_oid != public_oid or item.public_suite_digest != public_digest
            or item.public_suite_digest_version != public_digest_version
            for item in g4_bindings
        ):
            raise ProductionRuntimeUnavailable("scorer receipt bindings are malformed")
        uid_names = (
            "IMPLBENCH_SCORER_KEYED_RUNNER_UID",
            "IMPLBENCH_SCORER_BROKER_UID",
            "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID",
            "IMPLBENCH_SCORER_COORDINATOR_UID",
            "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID",
            "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
        )
        try:
            uids = {name: int(os.environ[name]) for name in uid_names}
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionRuntimeUnavailable("controller-owned scorer UIDs are unavailable") from exc
        try:
            g1 = build_g1_topology(
                keyed_runner_uid=uids[uid_names[0]],
                broker_uid=uids[uid_names[1]],
                submitted_program_uid=uids[uid_names[2]],
                battery_key=os.environ["IMPLBENCH_BATTERY_KEY"],
            )
            g4 = build_g4_topology(
                coordinator_uid=uids[uid_names[3]],
                broker_uid=uids[uid_names[1]],
                submitted_code_uid=uids[uid_names[5]],
                suite_runner_broker_uid=uids[uid_names[4]],
                public_suite_oid=public_oid,
                public_suite_digest=public_digest,
            )
            root = trusted_input.materialization
            process_table = _SystemProcessTable()
            def reap(uid: int) -> None:
                reap_and_prove_empty(
                    uid,
                    list_processes=process_table.census_uid,
                    kill_process=lambda pid, sig=15: os.kill(pid, sig),
                )
            budgets = manifest.get("budgets")
            scorer_budget = budgets.get("scorer_max_output_bytes") if isinstance(budgets, Mapping) else None
            if isinstance(scorer_budget, bool) or not isinstance(scorer_budget, int) or scorer_budget <= 0:
                raise ProductionRuntimeUnavailable("manifest scorer output budget is unavailable")
            scorer_root = Path(tempfile.mkdtemp(prefix=".implbench-scorer-", dir=str(root.parent)))
            home = scorer_root / "home"
            runtime = scorer_root / "runtime"
            home.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            (runtime / "tmp").mkdir(mode=0o700)
            profile_paths = SandboxPaths(
                cell_root=scorer_root,
                worktree=root,
                git_dir=root / ".git",
                evidence_root=scorer_root / "denied-evidence",
                base_checkout=scorer_root / "denied-base",
                sibling_worktree=scorer_root / "denied-sibling",
                credential_root=scorer_root / "denied-credentials",
                key_root=scorer_root / "denied-key",
                home=home,
                runtime=runtime,
            )
            scorer_executables = tuple(dict.fromkeys((
                Path(sys.executable).resolve(strict=True), path.resolve(strict=True),
            )))
            runtime_candidates = [
                Path(__file__).parents[2], Path(sys.base_prefix), Path(sys.prefix),
                Path("/usr/lib"), Path("/System/Library"),
            ]
            scorer_runtime_roots = tuple(dict.fromkeys(
                candidate.resolve(strict=True) for candidate in runtime_candidates if candidate.exists()
            ))
            launch_profile = generate_profile(
                "scorer", profile_paths,
                process_exec_paths=scorer_executables,
                runtime_read_paths=scorer_runtime_roots,
            )
            launch_profile_digest = hashlib.sha256(launch_profile.encode()).hexdigest()
            reaper = None if structural_identity else reap
            sandbox_args = {
                "reaper": reaper,
                "max_output_bytes": scorer_budget,
                "structural_identity": structural_identity,
                "launch_profile": launch_profile,
                "launch_profile_digest": launch_profile_digest,
            }
            sandbox_g1 = ScorerSandbox(root, trusted_input, g1, **sandbox_args)
            sandbox_g4 = ScorerSandbox(root, trusted_input, g4, **sandbox_args)
            results = list(sandbox_g1.run_topology({
                role: [str(path), "--json", "--gate", "G1", "--role", role.value]
                for role in (ScorerRole.KEYED_RUNNER, ScorerRole.BROKER, ScorerRole.SUBMITTED_PROGRAM)
            }, timeout_s=120))
            results.extend(sandbox_g4.run_topology({
                role: [str(path), "--json", "--gate", "G4", "--role", role.value]
                for role in (ScorerRole.COORDINATOR, ScorerRole.SUITE_RUNNER_BROKER, ScorerRole.SUBMITTED_CODE)
            }, timeout_s=120, g4_receipt_bindings=g4_bindings))
            # The real UID launcher folds each submitted role into its trusted
            # parent row.  The parent kernel-observes and reports that child
            # status, so production success is exactly four parent rows.
            expected_roles = {
                ScorerRole.KEYED_RUNNER.value, ScorerRole.BROKER.value,
                ScorerRole.COORDINATOR.value, ScorerRole.SUITE_RUNNER_BROKER.value,
            }
            child_roles = {ScorerRole.BROKER.value, ScorerRole.SUITE_RUNNER_BROKER.value}
            if (len(results) != len(expected_roles)
                    or any(not isinstance(item, ScorerRunResult) for item in results)
                    or {item.role for item in results} != expected_roles
                    or any(isinstance(item.exit_code, bool) or not isinstance(item.exit_code, int) or item.exit_code != 0
                           or not isinstance(item.stdout, str) or not isinstance(item.stderr, str)
                           or (item.role in child_roles and (isinstance(item.submitted_child_exit_code, bool)
                               or not isinstance(item.submitted_child_exit_code, int) or item.submitted_child_exit_code != 0))
                           or (item.role not in child_roles and item.submitted_child_exit_code is not None)
                           for item in results)):
                raise ProductionRuntimeUnavailable("production scorer role results are incomplete or unsuccessful")
            graph_results = (sandbox_g1.last_graph_result, sandbox_g4.last_graph_result)
            if any(not isinstance(value, Mapping) for value in graph_results):
                raise ProductionRuntimeUnavailable("production scorer graph omitted controller result")
            score.last_launch_evidence = tuple(
                [*getattr(getattr(sandbox_g1, "launcher", None), "launch_evidence", ()),
                 *getattr(getattr(sandbox_g4, "launcher", None), "launch_evidence", ())]
            )
        except ScorerModelExecutionLimit:
            # The sandbox emits this only when the submitted model role is
            # known to have crossed its execution/output limit.  Do not turn
            # that model result into a launcher/infrastructure UNKNOWN.
            return {"model_limit_proven": True,
                    "g1": "FAIL", "g3": "UNKNOWN", "g4": "UNKNOWN", "g5": "UNKNOWN",
                    "g6": "UNKNOWN", "g7": "UNKNOWN", "g4_receipts": ()}
        except Exception as exc:  # noqa: BLE001 - scorer isolation failures are UNKNOWN
            raise ProductionRuntimeUnavailable("production scorer execution failed") from exc
        finally:
            if scorer_root is not None:
                try:
                    delete_tree_descriptor_safe(scorer_root)
                except Exception as exc:  # noqa: BLE001 - cleanup is an authority boundary
                    raise ProductionRuntimeUnavailable("scorer scratch cleanup failed") from exc
                if scorer_root.exists() or scorer_root.is_symlink():
                    raise ProductionRuntimeUnavailable("scorer scratch cleanup left state behind")
        result = {key: value[key] for value in graph_results for key in value}
        required = {"g1", "g3", "g4", "g5", "g6", "g7", "g4_receipts"}
        if set(result) != required or any(result[name] not in {"PASS", "FAIL", "UNKNOWN"} for name in required - {"g4_receipts"}):
            raise ProductionRuntimeUnavailable("production scorer controller result is invalid")
        try:
            validate_g4_receipts(
                result["g4_receipts"], expected_oids=[item.commit_oid for item in g4_bindings],
                cell_id=cell_id, attempt_id=attempt_id, public_suite_oid=public_oid,
                public_suite_digest=public_digest, public_suite_digest_version=public_digest_version,
                expected_sequences={item.commit_oid: item.controller_sequence for item in g4_bindings},
            )
        except Exception as exc:
            raise ProductionRuntimeUnavailable("production scorer G4 receipt evidence is invalid") from exc
        return result

    score.last_launch_evidence = ()
    return score


def build_production_controller(manifest: Mapping[str, Any], *, scorer_factory: Any | None = None, provisioner: PlaneProvisioner | None = None, acl: Any | None = None) -> Any:
    """Construct the real controller-owned configuration and live factory maps."""

    controller = _ProductionController(manifest, provisioner=provisioner, acl=acl)
    controller.scored_scorer_factory = (
        scorer_factory if scorer_factory is not None
        else build_production_scorer(
            manifest,
            structural_identity=bool(getattr(controller.provisioner, "structural_only", False)),
        )
    )
    return controller


def build_production_runtime(manifest: Mapping[str, Any], *, controller: Any | None = None) -> Any:
    """Assemble phase callbacks and the typed scored binding from controller state."""

    if controller is None:
        raise ProductionRuntimeUnavailable("required live production runtime controller factory is unavailable")
    if not isinstance(manifest, Mapping):
        raise ProductionRuntimeUnavailable("production manifest is not a mapping")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("oi-pi-bakeoff-"):
        raise ProductionRuntimeUnavailable("production manifest has no scored run ID")
    source = manifest.get("source")
    source_root = source.get("realpath") if isinstance(source, Mapping) else None
    repo = getattr(controller, "repo", source_root)
    if not isinstance(repo, Path):
        if not isinstance(repo, str) or not repo:
            raise ProductionRuntimeUnavailable("production controller repo is missing")
        repo = Path(repo)
    if not repo.is_absolute():
        raise ProductionRuntimeUnavailable("production controller repo must be absolute")

    factory_fields = (
        "task_for_cell", "seat_for_cell", "engine_for_cell", "fixture_root_oid_for_cell",
        "tool_gid_for_cell", "scored_runtime_factory", "close_cell",
    )
    missing = [name for name in factory_fields if not callable(getattr(controller, name, None))]
    if missing:
        raise ProductionRuntimeUnavailable("production controller factories are incomplete: " + ", ".join(missing))
    dispatch_fn = getattr(controller, "dispatch_fn", run_task)
    if dispatch_fn is not run_task:
        raise ProductionRuntimeUnavailable("production scored dispatch is not bound to run_task")
    required = (
        "gate_checks", "cell_factory", "validate", "known_good_calibration",
        "hermetic_suite", "adversarial_validation", "known_good", "unscored",
        "execute", "append_attempt", "pilot_seal", "freeze_final", "stop_observation",
        "manifest_bytes", "config_bytes", "refs", "journal_tail",
    )
    missing = [name for name in required if not hasattr(controller, name)]
    if missing:
        raise ProductionRuntimeUnavailable("production controller is incomplete: " + ", ".join(missing))
    callbacks = getattr(controller, "gate_checks")
    if not isinstance(callbacks, Mapping) or set(callbacks) != set(GATE_IDS) or any(not callable(callbacks[name]) for name in GATE_IDS):
        raise ProductionRuntimeUnavailable("production controller gate checks are incomplete")
    callable_fields = (
        "cell_factory", "validate", "known_good_calibration", "hermetic_suite", "adversarial_validation",
        "known_good", "unscored", "execute", "append_attempt", "stop_observation", "freeze_final",
    )
    if any(not callable(getattr(controller, name)) for name in callable_fields):
        raise ProductionRuntimeUnavailable("production controller callbacks are incomplete")
    binding = ScoredDispatchBinding(
        run_id=run_id,
        repo=repo,
        task_for_cell=controller.task_for_cell,
        seat_for_cell=controller.seat_for_cell,
        engine_for_cell=controller.engine_for_cell,
        fixture_root_oid_for_cell=controller.fixture_root_oid_for_cell,
        tool_gid_for_cell=controller.tool_gid_for_cell,
        scored_runtime_factory=controller.scored_runtime_factory,
        cell_root_for_cell=getattr(controller, "cell_root_for_cell", None),
        lifecycle_for_cell=getattr(controller, "cell_for_cell", None),
        evidence_log=getattr(controller, "evidence_log", None),
        recorder_for_cell=getattr(controller, "recorder_for_cell", None),
        dispatch_fn=run_task,
    )
    runtime_fields = {name: getattr(controller, name) for name in required}
    runtime_fields.update({
        "gate_checks": callbacks,
        "close_cell": controller.close_cell,
        "scored_dispatch": binding,
        "execute": binding,
        "repo": repo,
    })
    return SimpleNamespace(**runtime_fields)


__all__ = ["ProductionRuntimeUnavailable", "build_production_controller", "build_production_runtime"]
