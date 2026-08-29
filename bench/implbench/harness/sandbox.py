"""Closed three-plane Seatbelt profiles and launch construction.

The controller owns this boundary.  A launch is built from an explicit allowlist, a
canonical profile, and a fresh identity; no process environment or shell is inherited.
Real Seatbelt/UID probes belong to Task 14 and use the same :class:`LaunchSpec` contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence
from urllib.parse import urlparse


Plane = Literal["control", "tool", "git-service", "importer", "scorer"]
PROFILE_ROLES: tuple[Plane, ...] = ("control", "tool", "git-service", "importer", "scorer")
PROFILE_ROOT = Path(__file__).resolve().parents[1] / "profiles"
MACH_ALLOWLIST: tuple[str, ...] = ()
MACH_ALLOWLIST_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ENDPOINT = re.compile(r"^[A-Za-z0-9._-]+:[0-9]{1,5}$")
_FORBIDDEN_ENV_PREFIXES = (
    "CLAUDE_", "CODEX_", "MCP_", "OPENAI_API_KEY", "ANTHROPIC_", "SKILL_", "PROJECT_", "ROLE_",
)
_FORBIDDEN_GIT_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXEC_PATH",
}
_PINNED_TEMPLATE_DIGESTS = {
    "control": "7db292cfad8baf7355e25bda54b7dabc5aa54f4322bb46531d86e49e69b750ba",
    "tool": "4ee8930a676e1a82856ccb5c5ee614981346762f7465c59bb17853b6dddc61c2",
    "git-service": "533aef8785925cbba756621c975236c7c0784ee9e2340e2a859e7d5b4f31b168",
    "importer": "533aef8785925cbba756621c975236c7c0784ee9e2340e2a859e7d5b4f31b168",
    "scorer": "533aef8785925cbba756621c975236c7c0784ee9e2340e2a859e7d5b4f31b168",
}
PROFILE_TEMPLATE_DIGESTS = _PINNED_TEMPLATE_DIGESTS


class SandboxError(ValueError):
    """Raised when a profile or launch request is not closed and verifiable."""


class LaunchError(SandboxError):
    """Raised when a launch cannot be proven to satisfy the boundary."""


@dataclass
class SandboxPaths:
    cell_root: Path
    worktree: Path
    git_dir: Path
    evidence_root: Path
    base_checkout: Path
    sibling_worktree: Path
    credential_root: Path
    key_root: Path
    home: Path
    runtime: Path
    service_socket: Path | None = None

    def validate(self) -> None:
        for name, path in self.__dict__.items():
            if path is None and name == "service_socket":
                continue
            if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
                raise SandboxError(f"{name} must be an absolute non-symlink path")
            if any(char in str(path) for char in '\"\n\r\x00'):
                raise SandboxError(f"{name} contains an unsafe profile path")

    @property
    def worktree_git_pointer(self) -> Path:
        return self.worktree / ".git"


@dataclass(frozen=True)
class InheritedDescriptor:
    """A one-shot descriptor passed across a plane boundary, never a secret env value."""

    name: str
    fd: int

    def validate(self) -> None:
        if not self.name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.name):
            raise LaunchError("descriptor name is invalid")
        if self.fd < 3:
            raise LaunchError("descriptor must not overlap stdin/stdout/stderr")


@dataclass(frozen=True)
class LaunchSpec:
    plane: Plane
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    profile: str
    profile_digest: str
    template_digest: str
    uid: int
    gid: int
    root_uid: int
    root_gid: int
    inherited_fds: tuple[int, ...]
    fresh_context: bool
    resume: bool
    fork_from: str | None
    warm_process: bool
    shell: bool = False


_LaunchResult = object
_Launcher = Callable[[LaunchSpec], _LaunchResult]


def _template_path(role: Plane) -> Path:
    if role not in PROFILE_ROLES:
        raise SandboxError(f"unknown sandbox plane: {role}")
    return PROFILE_ROOT / f"{role}.sb"


def template_digest(role: Plane) -> str:
    path = _template_path(role)
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SandboxError(f"profile template unavailable: {path}") from exc
    if actual != PROFILE_TEMPLATE_DIGESTS[role]:
        raise SandboxError(f"profile template digest mismatch: {role}")
    return actual


def mach_allowlist_digest() -> str:
    """Return the pinned digest of the closed, sorted Mach-service allowlist."""

    actual = hashlib.sha256(json.dumps(MACH_ALLOWLIST, separators=(",", ":")).encode("utf-8")).hexdigest()
    if actual != MACH_ALLOWLIST_DIGEST:
        raise SandboxError("Mach allowlist digest mismatch")
    return actual


def profile_digest(role: Plane, paths: SandboxPaths, *, provider_endpoints: Sequence[str] = (), bus_endpoints: Sequence[str] = ()) -> str:
    return hashlib.sha256(generate_profile(role, paths, provider_endpoints=provider_endpoints, bus_endpoints=bus_endpoints).encode()).hexdigest()


def _endpoint_host_port(value: str) -> str:
    if _ENDPOINT.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise SandboxError("network endpoints must include an explicit host and port")
    result = f"{host}:{port}"
    if not _ENDPOINT.fullmatch(result):
        raise SandboxError("network endpoint is not a canonical host:port value")
    return result


def _path_rule(kind: str, path: Path) -> str:
    return f'({kind} "{path}")'


def _deny_paths(paths: SandboxPaths, *, include_worktree_git: bool = True) -> list[str]:
    values = [paths.evidence_root, paths.base_checkout, paths.sibling_worktree, paths.credential_root, paths.key_root]
    if include_worktree_git:
        values.extend((paths.git_dir, paths.worktree_git_pointer))
    lines: list[str] = []
    for path in values:
        lines.append(f"(deny file-read* {_path_rule('subpath', path)})")
        lines.append(f"(deny file-write* {_path_rule('subpath', path)})")
    return lines


def generate_profile(
    role: Plane,
    paths: SandboxPaths,
    *,
    provider_endpoints: Sequence[str] = (),
    bus_endpoints: Sequence[str] = (),
    process_exec_paths: Sequence[Path] = (),
    runtime_read_paths: Sequence[Path] = (),
) -> str:
    """Render one canonical deny-default profile for a fixed cell path set."""

    paths.validate()
    mach_allowlist_digest()
    template_digest(role)
    if role != "scorer" and (process_exec_paths or runtime_read_paths):
        raise SandboxError("runtime execution grants are scorer-only")
    executable_paths: tuple[Path, ...] = ()
    readable_runtime_paths: tuple[Path, ...] = ()
    if role == "scorer":
        executable_paths = tuple(Path(value) for value in process_exec_paths)
        readable_runtime_paths = tuple(Path(value) for value in runtime_read_paths)
        for executable in executable_paths:
            if (not executable.is_absolute() or executable.is_symlink()
                    or executable.resolve(strict=True) != executable or not executable.is_file()):
                raise SandboxError("scorer executable path is not canonical")
        for runtime_path in readable_runtime_paths:
            if (not runtime_path.is_absolute() or runtime_path.is_symlink()
                    or runtime_path.resolve(strict=True) != runtime_path or not runtime_path.is_dir()):
                raise SandboxError("scorer runtime path is not canonical")
    template = _template_path(role).read_text(encoding="utf-8").rstrip("\n")
    lines = [template]
    if role == "control":
        for path in (paths.worktree, paths.home, paths.runtime):
            lines.append(f"(allow file-read* {_path_rule('subpath', path)})")
        for path in (paths.home, paths.runtime):
            lines.append(f"(allow file-write* {_path_rule('subpath', path)})")
        for endpoint in (*provider_endpoints, *bus_endpoints):
            lines.append(f'(allow network-outbound (remote tcp "{_endpoint_host_port(endpoint)}"))')
        lines.extend(_deny_paths(paths))
    elif role == "tool":
        for path in (paths.worktree, paths.home, paths.runtime):
            lines.append(f"(allow file-read* {_path_rule('subpath', path)})")
            lines.append(f"(allow file-write* {_path_rule('subpath', path)})")
        lines.extend(_deny_paths(paths))
    elif role == "git-service":
        for path in (paths.git_dir, paths.worktree, paths.runtime):
            lines.append(f"(allow file-read* {_path_rule('subpath', path)})")
            lines.append(f"(allow file-write* {_path_rule('subpath', path)})")
        if paths.service_socket is not None:
            # The socket is attempt-scoped and exact.  Do not grant the temporary
            # directory that happens to contain it.
            lines.append(f"(allow file-read* {_path_rule('literal', paths.service_socket)})")
            lines.append(f"(allow file-write* {_path_rule('literal', paths.service_socket)})")
        lines.extend(_deny_paths(paths, include_worktree_git=False))
    elif role == "importer":
        # Import has no controller/evidence mount.  ``runtime`` is an importer-only
        # scratch directory; the descriptor-held source arrives through an inherited
        # FD and is deliberately not represented as a pathname grant.
        for path in (paths.home, paths.runtime):
            lines.append(f"(allow file-read* {_path_rule('subpath', path)})")
            lines.append(f"(allow file-write* {_path_rule('subpath', path)})")
        lines.extend(_deny_paths(paths))
    else:
        # The scorer supervisor and all six descendants inherit one deny-default
        # sandbox. The imported tree is read-only; only scorer-owned home/runtime
        # are writable. Role separation and the hidden key remain descriptor/UID
        # boundaries inside this outer sandbox.
        lines.append(f"(allow file-read* {_path_rule('subpath', paths.worktree)})")
        for path in (paths.home, paths.runtime):
            lines.append(f"(allow file-read* {_path_rule('subpath', path)})")
            lines.append(f"(allow file-write* {_path_rule('subpath', path)})")
        for runtime_path in readable_runtime_paths:
            lines.append(f"(allow file-read* {_path_rule('subpath', runtime_path)})")
        if executable_paths:
            # dyld resolves absolute runtime roots from the filesystem root;
            # this exact-directory grant does not expose any descendant.
            lines.append(f"(allow file-read* {_path_rule('literal', Path('/'))})")
        for executable in executable_paths:
            lines.append(f"(allow file-read* {_path_rule('literal', executable)})")
            lines.append(f"(allow process-exec {_path_rule('literal', executable)})")
        lines.extend(_deny_paths(paths))
    return "\n".join(lines) + "\n"


def _base_env(paths: SandboxPaths) -> dict[str, str]:
    return {
        "HOME": str(paths.home),
        "TMPDIR": str(paths.runtime / "tmp"),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _validate_extra_env(role: Plane, extra_env: Mapping[str, str]) -> None:
    allowed = {
        "control": {
            "ARB_PROVIDER_ENDPOINT", "ARB_BUS_ENDPOINT", "INTERPRETER_HOME",
            "PI_CODING_AGENT_DIR", "XDG_STATE_HOME", "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN",
            "BRIDGE_PI_RETIRE_AFTER_TURN",
        },
        "tool": {"GIT_SHIM_SOCKET"},
        "git-service": {"GIT_SERVICE_SOCKET", "PYTHONPATH"},
        "importer": {"PYTHONPATH"},
        "scorer": {"PYTHONPATH"},
    }[role]
    if set(extra_env) - allowed:
        raise LaunchError("environment override is outside the plane allowlist")
    if any(any(key.startswith(prefix) for prefix in _FORBIDDEN_ENV_PREFIXES) for key in extra_env):
        raise LaunchError("role/project/skill/MCP/extension environment is forbidden")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in extra_env.items()):
        raise LaunchError("environment keys and values must be strings")


def build_launch_spec(
    role: Plane,
    paths: SandboxPaths,
    *,
    uid: int,
    gid: int | None = None,
    argv: Sequence[str],
    provider_endpoint: str | None = None,
    bus_endpoint: str | None = None,
    git_socket: Path | None = None,
    inherited_fds: Sequence[int] = (),
    extra_env: Mapping[str, str] | None = None,
    resume: bool = False,
    fork_from: str | None = None,
    warm_process: bool = False,
    shell: bool = False,
) -> LaunchSpec:
    """Build a fresh, no-shell launch request with no inherited environment."""

    if role not in PROFILE_ROLES:
        raise LaunchError(f"unknown sandbox plane: {role}")
    paths.validate()
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise LaunchError("launch UID must be a non-root integer")
    launch_gid = uid if gid is None else gid
    if not isinstance(launch_gid, int) or isinstance(launch_gid, bool) or launch_gid <= 0:
        raise LaunchError("launch GID must be a non-root integer")
    args = tuple(argv)
    if not args or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in args):
        raise LaunchError("argv must be a non-empty NUL-free sequence")
    if shell:
        raise LaunchError("shell launch is forbidden")
    if resume or fork_from is not None or warm_process:
        raise LaunchError("resume, fork, and warm-process paths are forbidden")
    inherited = tuple(inherited_fds)
    if inherited != tuple(sorted(set(inherited))) or any(not isinstance(fd, int) or fd < 3 for fd in inherited):
        raise LaunchError("inherited descriptors must be unique sorted integers >= 3")
    extras = dict(extra_env or {})
    _validate_extra_env(role, extras)
    env = _base_env(paths)
    if role == "control":
        if provider_endpoint is None or bus_endpoint is None:
            raise LaunchError("control launch requires explicit provider and bus endpoints")
        if "ARB_PROVIDER_ENDPOINT" in extras and extras["ARB_PROVIDER_ENDPOINT"] != provider_endpoint:
            raise LaunchError("provider endpoint override does not match the profile")
        if "ARB_BUS_ENDPOINT" in extras and extras["ARB_BUS_ENDPOINT"] != bus_endpoint:
            raise LaunchError("bus endpoint override does not match the profile")
        arm_env = {
            "INTERPRETER_HOME": str(paths.home),
            "PI_CODING_AGENT_DIR": str(paths.home),
            "XDG_STATE_HOME": str(paths.home / "state"),
            "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN": "1",
            "BRIDGE_PI_RETIRE_AFTER_TURN": "1",
        }
        for key, value in arm_env.items():
            if key in extras and extras[key] != value:
                raise LaunchError(f"{key} is not bound to the fresh per-arm context")
        env.update({"ARB_PROVIDER_ENDPOINT": provider_endpoint, "ARB_BUS_ENDPOINT": bus_endpoint})
        env.update(arm_env)
        env.update(extras)
        profile = generate_profile("control", paths, provider_endpoints=(provider_endpoint,), bus_endpoints=(bus_endpoint,))
    elif role == "tool":
        shim_socket = str(git_socket or paths.runtime / "git-shim.sock")
        if "GIT_SHIM_SOCKET" in extras and extras["GIT_SHIM_SOCKET"] != shim_socket:
            raise LaunchError("Git shim socket override does not match the launch boundary")
        env["GIT_SHIM_SOCKET"] = shim_socket
        env.update(extras)
        profile = generate_profile("tool", paths)
    elif role == "git-service":
        service_socket = str(git_socket or paths.runtime / "git-service.sock")
        if paths.service_socket is not None and str(paths.service_socket) != service_socket:
            raise LaunchError("Git service socket is not the profile-bound endpoint")
        if "GIT_SERVICE_SOCKET" in extras and extras["GIT_SERVICE_SOCKET"] != service_socket:
            raise LaunchError("Git service socket override does not match the launch boundary")
        env.update({"GIT_DIR": str(paths.git_dir), "GIT_WORK_TREE": str(paths.worktree), "GIT_CONFIG_NOSYSTEM": "1"})
        env["GIT_SERVICE_SOCKET"] = service_socket
        env.update(extras)
        profile = generate_profile("git-service", paths)
    elif role == "importer":
        env.update(extras)
        profile = generate_profile("importer", paths)
    else:
        env.update(extras)
        profile = generate_profile("scorer", paths)
    return LaunchSpec(
        plane=role,
        argv=args,
        env=env,
        cwd=paths.worktree,
        profile=profile,
        profile_digest=hashlib.sha256(profile.encode()).hexdigest(),
        template_digest=template_digest(role),
        uid=uid,
        gid=launch_gid,
        root_uid=0,
        root_gid=0,
        inherited_fds=inherited,
        fresh_context=True,
        resume=False,
        fork_from=None,
        warm_process=False,
        shell=False,
    )


def verify_launch_spec(spec: LaunchSpec) -> None:
    """Recompute all launch invariants immediately before crossing the OS boundary."""

    if spec.uid <= 0 or spec.gid <= 0 or spec.root_uid != 0 or spec.root_gid != 0:
        raise LaunchError("launch identity or root ownership is not exact")
    if spec.shell or not spec.fresh_context or spec.resume or spec.fork_from is not None or spec.warm_process:
        raise LaunchError("launch is not fresh and no-shell")
    if not spec.argv or any("\x00" in arg for arg in spec.argv):
        raise LaunchError("launch argv is invalid")
    if spec.env.get("PYTHONDONTWRITEBYTECODE") != "1":
        raise LaunchError("bytecode generation is not disabled")
    if spec.plane == "control":
        expected_arm_env = {
            "INTERPRETER_HOME": spec.env.get("HOME"),
            "PI_CODING_AGENT_DIR": spec.env.get("HOME"),
            "XDG_STATE_HOME": str(Path(spec.env["HOME"]) / "state"),
            "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN": "1",
            "BRIDGE_PI_RETIRE_AFTER_TURN": "1",
        }
        if any(spec.env.get(key) != value for key, value in expected_arm_env.items()):
            raise LaunchError("control arm context is not exact")
    if spec.profile_digest != hashlib.sha256(spec.profile.encode()).hexdigest() or not _HEX64.fullmatch(spec.profile_digest):
        raise LaunchError("sandbox profile digest mismatch")
    if spec.template_digest != PROFILE_TEMPLATE_DIGESTS.get(spec.plane):
        raise LaunchError("sandbox template digest mismatch")
    if any(key.startswith(prefix) for key in spec.env for prefix in _FORBIDDEN_ENV_PREFIXES):
        raise LaunchError("forbidden role/project/skill/MCP/extension environment present")
    allowed_env = {
        "control": {
            "HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "ARB_PROVIDER_ENDPOINT", "ARB_BUS_ENDPOINT",
            "INTERPRETER_HOME", "PI_CODING_AGENT_DIR", "XDG_STATE_HOME", "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN",
            "BRIDGE_PI_RETIRE_AFTER_TURN",
        },
        "tool": {"HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "GIT_SHIM_SOCKET"},
        "git-service": {
            "HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_CONFIG_NOSYSTEM", "GIT_SERVICE_SOCKET", "PYTHONPATH",
        },
        "importer": {"HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"},
        "scorer": {"HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"},
    }[spec.plane]
    if set(spec.env) - allowed_env:
        raise LaunchError("launch environment is outside the plane allowlist")
    if spec.plane in {"control", "tool", "importer"} and any(key in _FORBIDDEN_GIT_ENV for key in spec.env):
        raise LaunchError("control/tool launch contains Git environment")
    if spec.plane != "control" and ("ARB_PROVIDER_ENDPOINT" in spec.env or "ARB_BUS_ENDPOINT" in spec.env):
        raise LaunchError("provider/bus endpoint leaked outside control plane")


def _os_launcher(spec: LaunchSpec) -> int:
    """The real macOS hook; Task 14 supplies the live evidence for this path."""

    if os.uname().sysname != "Darwin":
        raise LaunchError("real Seatbelt launch is only available on macOS")
    process = subprocess.Popen(
        ["/usr/bin/sandbox-exec", "-p", spec.profile, *spec.argv],
        cwd=spec.cwd,
        env=spec.env,
        shell=False,
        close_fds=True,
        user=spec.uid,
        group=spec.gid,
        extra_groups=(),
    )
    return process.pid


def launch(spec: LaunchSpec, *, launcher: _Launcher | None = None) -> _LaunchResult:
    verify_launch_spec(spec)
    return (launcher or _os_launcher)(spec)


def spawn_child(
    spec: LaunchSpec,
    *,
    pass_fds: Sequence[int],
    allow_unprofiled_test: bool = False,
) -> subprocess.Popen[bytes]:
    """Spawn one verified child without inheriting controller state.

    This is deliberately an explicit hermetic-test seam.  Production must enter
    through the verified plane helper, which owns the privileged UID/Seatbelt exec.
    """

    verify_launch_spec(spec)
    inherited = tuple(pass_fds)
    if inherited != tuple(sorted(set(inherited))) or any(not isinstance(fd, int) or fd < 3 for fd in inherited):
        raise LaunchError("child descriptors must be unique sorted integers >= 3")
    if not allow_unprofiled_test or spec.uid != os.getuid() or spec.gid != os.getgid():
        raise LaunchError("unprofiled child launch is test-only and requires the current UID/GID")
    argv = spec.argv
    kwargs: dict[str, object] = {}
    return subprocess.Popen(
        argv,
        cwd=spec.cwd,
        env=spec.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        pass_fds=inherited,
        start_new_session=True,
        **kwargs,
    )
