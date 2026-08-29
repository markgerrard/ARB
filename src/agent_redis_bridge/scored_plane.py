"""One-attempt production control and tool processes for scored implbench cells.

Both entry points are intentionally one-cell, one-attempt daemons.  The controller
starts them through the pinned plane helper and communicates over bounded Unix
frames.  Git authority is read once from an inherited descriptor by the tool
process and is never placed in argv, environment, or the control request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping


PROTOCOL = "implbench-tool-plane-v1"
CONTROL_PROTOCOL = "implbench-control-plane-v1"
MAX_FRAME = 1024 * 1024
MAX_MODEL_RESULT_BYTES = 4096
_CREDENTIAL_ENV = re.compile(
    r"^[A-Z][A-Z0-9_]*(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|OAUTH_TOKEN|TOKEN|SECRET|CREDENTIALS?)$"
)
_RUNTIME_ENV_NAMES = {
    "HOME", "PATH", "PYTHONPATH", "SHELL", "TMPDIR", "PWD", "OLDPWD",
    "NODE_OPTIONS", "NODE_PATH", "RUBYLIB", "PERL5LIB",
}
_RUNTIME_ENV_PREFIXES = ("DYLD_", "LD_", "PYTHON", "NODE_", "BASH_", "ZDOTDIR")


def _credential_environment_name(name: str) -> bool:
    return (
        name not in _RUNTIME_ENV_NAMES
        and not name.startswith(_RUNTIME_ENV_PREFIXES)
        and _CREDENTIAL_ENV.fullmatch(name) is not None
    )


def _project_model_text(value: str) -> tuple[str, int, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= MAX_MODEL_RESULT_BYTES:
        return value, len(raw), False
    return raw[:MAX_MODEL_RESULT_BYTES].decode("utf-8", errors="ignore"), len(raw), True


def _project_model_streams(stdout: str, stderr: str) -> tuple[str, str, int, bool]:
    stdout_raw = stdout.encode("utf-8")
    stderr_raw = stderr.encode("utf-8")
    original_bytes = len(stdout_raw) + len(stderr_raw)
    if original_bytes <= MAX_MODEL_RESULT_BYTES:
        return stdout, stderr, original_bytes, False
    stdout_projected = stdout_raw[:MAX_MODEL_RESULT_BYTES]
    remaining = MAX_MODEL_RESULT_BYTES - len(stdout_projected)
    stderr_projected = stderr_raw[:remaining]
    return (
        stdout_projected.decode("utf-8", errors="ignore"),
        stderr_projected.decode("utf-8", errors="ignore"),
        original_bytes,
        True,
    )


def _model_result_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _project_model_result(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Bound the complete object that Pi/OI serialize into model context."""

    encoded = _model_result_bytes(value)
    if len(encoded) <= MAX_MODEL_RESULT_BYTES:
        return dict(value)
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("tool result keys must be strings")
        if isinstance(item, str) or item is None or isinstance(item, (bool, int, float)):
            projected[key] = item
        elif isinstance(item, (list, tuple, Mapping)):
            projected[f"{key}_count"] = len(item)
    projected["truncated"] = True
    projected.setdefault("original_bytes", len(encoded))
    while len(_model_result_bytes(projected)) > MAX_MODEL_RESULT_BYTES:
        strings = [(key, item) for key, item in projected.items() if isinstance(item, str) and item]
        if not strings:
            return {"truncated": True, "original_bytes": projected["original_bytes"]}
        key, item = max(strings, key=lambda row: len(row[1].encode("utf-8")))
        excess = len(_model_result_bytes(projected)) - MAX_MODEL_RESULT_BYTES
        raw = item.encode("utf-8")
        projected[key] = raw[:max(0, len(raw) - max(1, excess))].decode("utf-8", errors="ignore")
    return projected


def _read_descriptor(fd: int) -> Mapping[str, Any]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, min(65536, MAX_FRAME + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FRAME:
            raise ValueError("authority descriptor exceeds its bound")
    os.close(fd)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, Mapping):
        raise ValueError("authority descriptor is malformed")
    return value


def _control_inputs(config_fd: int, secret_fd: int) -> tuple[Mapping[str, Any], dict[str, str], tuple[str, ...]]:
    config = _read_descriptor(config_fd)
    required_config = {
        "schema", "run_id", "cell_id", "attempt_id", "arm", "engine", "provider", "model",
        "harness", "workdir", "interpreter_bin", "interpreter_sha256", "config_digest",
    }
    if set(config) != required_config or config.get("schema") != "implbench-control-config-v1":
        raise ValueError("control config fields are not exact")
    digest_payload = {key: config[key] for key in config if key != "config_digest"}
    actual_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if config.get("config_digest") != actual_digest:
        raise ValueError("control config digest mismatch")

    secret = _read_descriptor(secret_fd)
    required_secret = {"schema", "arm", "provider", "secret_names", "environment", "files"}
    if set(secret) != required_secret or secret.get("schema") != "implbench-control-secret-v1":
        raise ValueError("control secret fields are not exact")
    if (secret.get("arm"), secret.get("provider")) != (config.get("arm"), config.get("provider")):
        raise ValueError("control secret is not bound to the configured arm")
    names = secret.get("secret_names")
    environment = secret.get("environment")
    files = secret.get("files")
    if (
        not isinstance(names, list)
        or not names
        or names != sorted(set(names))
        or not all(isinstance(name, str) and name for name in names)
        or not isinstance(environment, Mapping)
        or not isinstance(files, Mapping)
    ):
        raise ValueError("control secret manifest is malformed")
    expected_names = sorted([*environment, *(f"file:{name}" for name in files)])
    if names != expected_names:
        raise ValueError("control secret names do not match its payload")
    env: dict[str, str] = {}
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not name.replace("_", "A").isalnum()
            or not name[0].isalpha()
            or not _credential_environment_name(name)
            or not isinstance(value, str)
            or not value
            or "\x00" in value
        ):
            raise ValueError("control secret environment is outside the credential allowlist")
        env[name] = value
    if files:
        raise ValueError("control secret files are forbidden; use credential environment names")
    return config, env, tuple(names)


def _recv(connection: socket.socket) -> Mapping[str, Any]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(65536, MAX_FRAME + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FRAME:
            raise ValueError("plane request exceeds its bound")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ValueError("plane request is not one bounded frame")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("plane request is malformed")
    return value


def _send(connection: socket.socket, *, result: Any = None, error: str | None = None) -> None:
    value = {"ok": error is None, "result": result, "error": error}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME:
        raise ValueError("plane response exceeds its bound")
    connection.sendall(encoded)


def _serve(endpoint: Path, handler: Any, *, mode: int = 0o600, listener_fd: int | None = None) -> None:
    if mode not in {0o600, 0o660}:
        raise ValueError("plane socket mode is forbidden")
    owns_endpoint = listener_fd is None
    if owns_endpoint:
        endpoint.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    else:
        listener = socket.socket(fileno=listener_fd)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        listener.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if owns_endpoint:
            listener.bind(str(endpoint))
            os.chmod(endpoint, mode)
            listener.listen(8)
        elif (
            listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
            or listener.getsockname() != str(endpoint)
        ):
            # Portable validation: SO_ACCEPTCONN is not supported on every
            # target platform (macOS raises ENOPROTOOPT).  The listening state
            # is established by the controller's bind/listen before fork, and a
            # non-listening descriptor fails loudly at the first accept().
            raise ValueError("inherited control listener is malformed")
        while not stopping:
            try:
                connection, _ = listener.accept()
            except OSError:
                if stopping:
                    break
                raise
            with connection:
                try:
                    request = _recv(connection)
                    _send(connection, result=handler(request))
                except Exception as exc:
                    _send(connection, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        listener.close()
        if owns_endpoint:
            endpoint.unlink(missing_ok=True)


def _tool(authority_fd: int, endpoint: Path) -> None:
    authority = _read_descriptor(authority_fd)
    required = {"git_endpoint", "git_capability", "socket_gid", "cell_id", "attempt_id", "workdir"}
    if set(authority) != required:
        raise ValueError("tool authority fields are not exact")
    git_endpoint = authority["git_endpoint"]
    git_capability = authority["git_capability"]
    socket_gid = authority["socket_gid"]
    workdir = Path(str(authority["workdir"]))
    identity = {"cell_id": authority["cell_id"], "attempt_id": authority["attempt_id"]}
    if (
        not isinstance(git_endpoint, str)
        or not os.path.isabs(git_endpoint)
        or not isinstance(git_capability, str)
        or len(git_capability) != 64
        or isinstance(socket_gid, bool)
        or not isinstance(socket_gid, int)
        or socket_gid <= 0
        or not all(isinstance(value, str) and value for value in identity.values())
        or not workdir.is_absolute()
        or workdir.resolve(strict=True) != workdir
        or not workdir.is_dir()
    ):
        raise ValueError("tool authority is malformed")

    from implbench.harness.git_service import RemoteGitService
    from .engines.openinterpreter import CellToolPlaneBroker

    service = RemoteGitService(endpoint=git_endpoint, capability=git_capability, tool_gid=socket_gid)
    service.receipt_chain = SimpleNamespace(identity=identity)
    broker = CellToolPlaneBroker.from_git_service(service)

    def confined_path(raw: Any, *, create_parent: bool = False) -> Path:
        if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
            raise ValueError("tool path must be relative")
        parts = Path(raw).parts
        if any(part in {"", ".", ".."} for part in parts) or raw == ".git" or ".git" in parts:
            raise ValueError("tool path is forbidden")
        current = workdir
        for part in parts[:-1]:
            current = current / part
            if current.exists():
                if not current.is_dir() or current.is_symlink():
                    raise ValueError("tool path parent is not a real directory")
            elif create_parent:
                # Git trees preserve only the executable bit.  New directories
                # therefore use the canonical archive/materialization mode so
                # the controller's live-tree digest can equal its independent
                # post-import reconstruction.
                current.mkdir(mode=0o755)
                os.chmod(current, 0o755)
            else:
                raise ValueError("tool path parent is absent")
        target = current / parts[-1]
        if target.exists() and target.is_symlink():
            raise ValueError("tool path cannot be a symlink")
        return target

    def execute_tool(payload: Mapping[str, Any]) -> Any:
        op = payload.get("op")
        if op in {"status", "add", "commit"}:
            return broker.handle_tool_request(dict(payload))
        if op == "read" and set(payload) == {"op", "path"}:
            target = confined_path(payload["path"])
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                content = os.read(descriptor, MAX_FRAME + 1)
                if len(content) > MAX_FRAME:
                    raise ValueError("tool read exceeds its bound")
                projected, original_bytes, truncated = _project_model_text(content.decode("utf-8"))
                result = {"path": payload["path"], "content": projected}
                if truncated:
                    result.update({"truncated": True, "original_bytes": original_bytes})
                return result
            finally:
                os.close(descriptor)
        if op == "write" and set(payload) == {"op", "path", "content"}:
            content = payload["content"]
            if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FRAME:
                raise ValueError("tool write content is malformed")
            target = confined_path(payload["path"], create_parent=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o644)
            try:
                os.fchmod(descriptor, 0o644)
                os.write(descriptor, content.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return {"path": payload["path"], "bytes": len(content.encode("utf-8"))}
        if op == "edit" and set(payload) == {"op", "path", "old_text", "new_text"}:
            target = confined_path(payload["path"])
            old_text, new_text = payload["old_text"], payload["new_text"]
            if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
                raise ValueError("tool edit content is malformed")
            descriptor = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
            try:
                original_bytes = os.read(descriptor, MAX_FRAME + 1)
                if len(original_bytes) > MAX_FRAME:
                    raise ValueError("tool edit exceeds its bound")
                original = original_bytes.decode("utf-8")
                if original.count(old_text) != 1:
                    raise ValueError("tool edit requires one exact match")
                updated = original.replace(old_text, new_text, 1)
                if len(updated.encode("utf-8")) > MAX_FRAME:
                    raise ValueError("tool edit exceeds its bound")
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, updated.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return {"path": payload["path"], "replacements": 1}
        if op == "bash" and set(payload) == {"op", "command"}:
            command = payload["command"]
            if not isinstance(command, str) or not command or len(command.encode("utf-8")) > 65536:
                raise ValueError("tool command is malformed")
            try:
                words = shlex.split(command)
            except ValueError as exc:
                raise ValueError("tool command cannot be parsed") from exc
            if any(Path(word).name in {"git", "xcrun"} for word in words):
                raise ValueError("real Git execution is forbidden in the tool plane")
            result = subprocess.run(
                ["/bin/sh", "-lc", command], cwd=workdir,
                env={"HOME": str(Path(os.environ["HOME"])), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=120, check=False,
            )
            output_bytes = len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
            if output_bytes > MAX_FRAME:
                raise ValueError("tool command output exceeds its bound")
            stdout, stderr, original_bytes, truncated = _project_model_streams(result.stdout, result.stderr)
            projected = {"exit_code": result.returncode, "stdout": stdout, "stderr": stderr}
            if truncated:
                projected.update({"truncated": True, "original_bytes": original_bytes})
            return projected
        raise ValueError("tool operation is forbidden")

    def normalize(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if "op" in payload:
            return payload
        method = payload.get("method")
        params = payload.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            raise ValueError("tool request shape is malformed")
        tool = params.get("tool") or params.get("name")
        if method in {"shell/execute", "code/execute"} or tool in {"shell", "bash"}:
            return {"op": "bash", "command": params.get("command") or params.get("code")}
        if method == "file/read" or tool == "read":
            return {"op": "read", "path": params.get("path")}
        if method == "file/write" or tool == "write":
            return {"op": "write", "path": params.get("path"), "content": params.get("content")}
        if tool == "edit":
            return {"op": "edit", "path": params.get("path"), "old_text": params.get("old_text"), "new_text": params.get("new_text")}
        raise ValueError("tool request cannot be normalized")

    def handle(request: Mapping[str, Any]) -> Any:
        op = request.get("op")
        expected = {"protocol", "op"} if op in {"ping", "completion"} else {"protocol", "op", "payload"}
        if request.get("protocol") != PROTOCOL or set(request) != expected:
            raise ValueError("tool-plane frame fields are not exact")
        if op == "ping":
            return {
                "protocol": PROTOCOL,
                "pid": os.getpid(),
                "effective_uid": os.geteuid(),
                "socket_gid": socket_gid,
                "identity": identity,
                "executable": sys.executable,
            }
        if op == "completion":
            return broker.completion_projection()
        if op == "tool" and isinstance(request.get("payload"), Mapping):
            return _project_model_result(execute_tool(normalize(request["payload"])))
        raise ValueError("tool-plane operation is forbidden")

    try:
        _serve(endpoint, handle, mode=0o660)
    finally:
        broker.clear()


def _control(authority_fd: int, config_fd: int, secret_fd: int, listener_fd: int, endpoint: Path) -> None:
    authority = _read_descriptor(authority_fd)
    required = {"tool_endpoint", "socket_gid", "cell_id", "attempt_id"}
    if set(authority) != required:
        raise ValueError("control authority fields are not exact")
    tool_endpoint = authority["tool_endpoint"]
    socket_gid = authority["socket_gid"]
    identity = {"cell_id": authority["cell_id"], "attempt_id": authority["attempt_id"]}
    if not isinstance(tool_endpoint, str) or not os.path.isabs(tool_endpoint):
        raise ValueError("control tool endpoint is malformed")
    config, provider_env, secret_names = _control_inputs(config_fd, secret_fd)
    if (config.get("cell_id"), config.get("attempt_id")) != (identity["cell_id"], identity["attempt_id"]):
        raise ValueError("control config identity mismatch")

    from .engines.openinterpreter import CellToolPlaneBroker
    from .engines._stdio import scrub_env_dict
    from .bridge import ScoredBridgeControl

    broker = CellToolPlaneBroker.from_endpoint(
        tool_endpoint, socket_gid=socket_gid, identity=identity,
    )
    # Daemon→engine choke: merge provider keys onto the daemon env, then scrub
    # bus + gate-daemon credentials so pi-sdk / openinterpreter cells never see
    # ARB_GATE_LANE_WRITER_* or bus secrets (engines also scrub process_env).
    control = ScoredBridgeControl(
        config=config,
        tool_broker=broker,
        provider_env=scrub_env_dict({**os.environ, **provider_env}),
    )

    def handle(request: Mapping[str, Any]) -> Any:
        op = request.get("op")
        expected = {"protocol", "op"} if op == "ping" else {"protocol", "op", "payload"}
        if request.get("protocol") != CONTROL_PROTOCOL or set(request) != expected:
            raise ValueError("control-plane frame fields are not exact")
        if op == "ping":
            tool = control.probe_tool_plane()
            return {
                "protocol": CONTROL_PROTOCOL,
                "pid": os.getpid(),
                "effective_uid": os.geteuid(),
                "identity": identity,
                "tool_crossed": isinstance(tool, Mapping),
                "config_digest": config["config_digest"],
                "secret_names": list(secret_names),
                "secret_descriptor_consumed": True,
                "executable": sys.executable,
            }
        if op != "run" or not isinstance(request.get("payload"), Mapping):
            raise ValueError("control-plane operation is forbidden")
        payload = request["payload"]
        required_payload = {"task", "timeout"}
        if set(payload) != required_payload:
            raise ValueError("control run payload fields are not exact")
        return control.run(str(payload["task"]), timeout=int(payload["timeout"]))

    try:
        _serve(endpoint, handle, listener_fd=listener_fd)
    finally:
        broker.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("control", "tool"))
    parser.add_argument("--authority-fd", required=True, type=int)
    parser.add_argument("--config-fd", type=int)
    parser.add_argument("--secret-fd", type=int)
    parser.add_argument("--listener-fd", type=int)
    parser.add_argument("--endpoint", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.endpoint.is_absolute():
        raise ValueError("plane endpoint must be absolute")
    if args.role == "tool":
        if args.config_fd is not None or args.secret_fd is not None or args.listener_fd is not None:
            raise ValueError("tool plane cannot receive control descriptors")
        _tool(args.authority_fd, args.endpoint)
    else:
        if args.config_fd is None or args.secret_fd is None or args.listener_fd is None:
            raise ValueError("control plane requires config, secret and listener descriptors")
        _control(args.authority_fd, args.config_fd, args.secret_fd, args.listener_fd, args.endpoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
