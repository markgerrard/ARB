"""Fresh, single-threaded supervisor for privileged scorer launches.

The controller hands this process one bounded configuration pipe.  It closes that
pipe before either untrusted role is execed.  In paired mode it starts a fresh
trusted broker boundary.  That boundary is the submitted role's kernel parent,
waitpid-observes both role payloads, and holds the only status channel back to
the controller.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping


SUPERVISOR_REGISTRATION_MAX_BYTES = 512
# `RLIMIT_NOFILE` is mutable at runtime, so its current soft value is not a
# safe authority boundary for inherited descriptors.  This covers the maximum
# descriptor number supported by the platforms on which the scorer runs
# (Linux's fs.nr_open default/ceiling and the macOS open-file ceiling); a finite
# hard RLIMIT below it narrows the C-level close range further below.
_FALLBACK_FD_CEILING = 1 << 20


def _read_config(fd: int) -> dict[str, Any]:
    chunks = bytearray()
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > 256 * 1024:
                raise ValueError("launcher configuration is too large")
    finally:
        os.close(fd)
    value = json.loads(bytes(chunks).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("launcher configuration schema is invalid")
    return value


def _as_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or "\0" in item for item in value):
        raise ValueError("launcher argv is invalid")
    return value


def _as_env(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) or not isinstance(item, str) or "\0" in key or "\0" in item for key, item in value.items()):
        raise ValueError("launcher environment is invalid")
    return dict(value)


def _role_env(value: Any, *, allow_battery: bool = False) -> tuple[dict[str, str], str | None]:
    env = _as_env(value)
    battery_key = env.pop("IMPLBENCH_BATTERY_KEY", None)
    forbidden = {"IMPLBENCH_GRAPH_AUTH", "IMPLBENCH_GRAPH_NONCE"}
    if any(key in env for key in forbidden) or (battery_key is not None and not allow_battery):
        raise ValueError("role environment contains controller-only secret material")
    return env, battery_key


def _uid_preexec(uid: int, *, structural_identity: bool = False):
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError("launcher UID is invalid")
    def apply_uid() -> None:
        if os.getuid() != uid:
            try:
                os.setuid(uid)
            except PermissionError:
                if not structural_identity:
                    raise
    return apply_uid


def _secret_fd(secret: str | None) -> tuple[int | None, int | None]:
    if secret is None:
        return None, None
    if not isinstance(secret, str) or not secret or len(secret.encode("utf-8")) > 4096:
        raise ValueError("launcher secret is invalid")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, secret.encode("utf-8"))
    finally:
        os.close(write_fd)
    return read_fd, None


def _spawn(argv: list[str], *, cwd: str, env: dict[str, str], uid: int,
           battery_key: str | None = None, structural_identity: bool = False) -> subprocess.Popen[bytes]:
    secret_fd, _ = _secret_fd(battery_key)
    child_env = dict(env)
    pass_fds: tuple[int, ...] = ()
    if secret_fd is not None:
        # This numeric descriptor is not secret.  Only the keyed runner receives
        # it; the descriptor is closed-on-exec for every other role.
        child_env["IMPLBENCH_BATTERY_KEY_FD"] = str(secret_fd)
        pass_fds = (secret_fd,)
    try:
        return subprocess.Popen(
            argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            preexec_fn=_uid_preexec(uid, structural_identity=structural_identity), pass_fds=pass_fds,
        )
    finally:
        if secret_fd is not None:
            os.close(secret_fd)


def _drain(processes: Mapping[str, subprocess.Popen[bytes]], limit: int, *, release: bool = False) -> dict[str, tuple[int, str, str]]:
    """Drain to unlinked sinks; only trusted launcher status may be released."""
    selector = selectors.DefaultSelector()
    counts: dict[tuple[str, str], int] = {}
    sinks: dict[tuple[str, str], object] = {}
    for name, process in processes.items():
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, (name, "stdout"))
        selector.register(process.stderr, selectors.EVENT_READ, (name, "stderr"))
        for stream in ("stdout", "stderr"):
            key = (name, stream)
            counts[key] = 0
            sinks[key] = tempfile.TemporaryFile(mode="w+b")
    terminating = False
    try:
        while selector.get_map():
            # A blocked peer can keep its pipes open forever.  Polling the direct
            # children between bounded drain waits makes an already-exited sibling
            # authoritative before the controller's total deadline expires.
            for process in processes.values():
                if process.poll() not in (None, 0) and not terminating:
                    terminating = True
                    for peer in processes.values():
                        if peer.poll() is None:
                            peer.terminate()
            for key, _ in selector.select(0.02):
                chunk = os.read(key.fileobj.fileno(), 131072)
                name, stream = key.data
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                counts[(name, stream)] += len(chunk)
                if counts[(name, stream)] > limit:
                    raise ValueError("scorer output cap exceeded")
                sinks[(name, stream)].write(chunk)
        result: dict[str, tuple[int, str, str]] = {}
        for name, process in processes.items():
            # Popen.wait is a direct waitpid of this supervisor's own child.
            code = process.wait()
            if release:
                sinks[(name, "stdout")].seek(0)
                sinks[(name, "stderr")].seek(0)
                result[name] = (code, sinks[(name, "stdout")].read().decode(errors="replace"), sinks[(name, "stderr")].read().decode(errors="replace"))
            else:
                result[name] = (code, "", "")
        return result
    except BaseException:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        raise
    finally:
        selector.close()
        for sink in sinks.values():
            sink.close()


def _normal(config: Mapping[str, Any]) -> dict[str, Any]:
    limit = config.get("max_output_bytes")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("launcher output budget is invalid")
    env, battery_key = _role_env(config["env"], allow_battery=True)
    structural_identity = config.get("structural_identity", False)
    if not isinstance(structural_identity, bool):
        raise ValueError("launcher structural identity mode is invalid")
    worker = _spawn(
        _as_argv(config["argv"]), cwd=str(config["cwd"]), env=env, uid=config["uid"],
        battery_key=battery_key, structural_identity=structural_identity,
    )
    result = _drain({"broker": worker}, limit)["broker"]
    return {"broker": {"exit_code": result[0]}, "role_pids": {"broker": worker.pid}}


def _close_fds_except(keep: set[int]) -> None:
    """Close inherited setup authority before a role payload can execute."""
    try:
        candidates = [int(item) for item in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        # Do not trust the current soft limit: a setup process can open a high
        # descriptor and lower that limit before this boundary runs.  Use a
        # safely capped C-level closerange instead of a Python per-FD loop.
        # Neither current RLIMIT is a close boundary: a descriptor opened
        # before a hard-limit reduction remains valid and must be closed.
        upper = _FALLBACK_FD_CEILING
        start = 3
        for fd in sorted(item for item in keep if 3 <= item < upper):
            os.closerange(start, fd)
            start = fd + 1
        os.closerange(start, upper)
        return
    for fd in candidates:
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def _write_supervisor_registration(fd: int, value: Mapping[str, int]) -> None:
    report = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(report) > SUPERVISOR_REGISTRATION_MAX_BYTES:
        raise RuntimeError("supervisor registration is oversized")
    os.write(fd, report)


def _role_stub(argv: list[str], *, cwd: str, env: dict[str, str], uid: int, start_fd: int,
               stdout_fd: int, stderr_fd: int, structural_identity: bool = False) -> None:
    """Become one payload only after the boundary has discarded setup privilege."""
    try:
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        _close_fds_except({start_fd, 1, 2})
        if os.getuid() != uid:
            try:
                os.setuid(uid)
            except PermissionError:
                if not structural_identity:
                    raise
        if os.read(start_fd, 1) != b"S":
            raise RuntimeError("launcher start barrier failed")
        os.close(start_fd)
        _close_fds_except({1, 2})
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    except BaseException as exc:
        try:
            os.write(2, (f"launcher role setup failed: {exc}\n").encode())
        finally:
            os._exit(125)


def _teardown_helper(command_fd: int, broker_pid: int, child_pid: int) -> None:
    """Minimal privileged TERM/KILL relay for exactly the two registered PIDs."""
    _close_fds_except({command_fd})
    try:
        while True:
            command = os.read(command_fd, 2)
            if command in (b"", b"XX"):
                return
            sig, pid = _helper_target(command, broker_pid, child_pid)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
    finally:
        os.close(command_fd)


def _helper_target(command: bytes, broker_pid: int, child_pid: int) -> tuple[int, int]:
    """The helper's whole command surface: four fixed operations, two fixed PIDs."""
    commands = {b"TB": (signal.SIGTERM, broker_pid), b"TC": (signal.SIGTERM, child_pid),
                b"KB": (signal.SIGKILL, broker_pid), b"KC": (signal.SIGKILL, child_pid)}
    if command not in commands:
        raise RuntimeError("invalid teardown helper command")
    return commands[command]


def _status(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 125


def _broker_pair(config: Mapping[str, Any], supervisor_fd: int = -1) -> dict[str, Any]:
    """Privileged setup, then an irreversible broker-UID supervision boundary."""
    limit = config.get("max_output_bytes")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("launcher output budget is invalid")
    timeout = config.get("execution_timeout_s")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("launcher execution timeout is invalid")
    structural_identity = config.get("structural_identity", False)
    if not isinstance(structural_identity, bool):
        raise ValueError("launcher structural identity mode is invalid")
    broker_env, _ = _role_env(config["env"])
    child_env, _ = _role_env(config["child_env"])
    roles = (("broker", _as_argv(config["argv"]), broker_env, config["uid"]),
             ("child", _as_argv(config["child_argv"]), child_env, config["child_uid"]))
    launched: dict[str, tuple[int, int, int, int]] = {}
    helper_pid: int | None = None
    command_write = -1
    selector: selectors.BaseSelector | None = None
    statuses: dict[str, int] = {}
    output_counts: dict[tuple[str, str], int] = {}
    output_sinks: dict[tuple[str, str], object] = {}
    execution_timeout_roles: tuple[str, ...] = ()
    output_limit_role: str | None = None

    def command(value: bytes) -> None:
        if command_write < 0:
            raise RuntimeError("teardown helper is unavailable")
        try:
            os.write(command_write, value)
        except OSError as exc:
            raise RuntimeError("teardown helper command failed") from exc

    def collect_statuses() -> None:
        for name, (pid, *_rest) in launched.items():
            if name in statuses:
                continue
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                statuses[name] = _status(status)

    def drain_ready(wait: float) -> str | None:
        if selector is None:
            return None
        for key, _ in selector.select(wait):
            chunk = os.read(key.fileobj, 131072)
            name, stream = key.data
            if not chunk:
                selector.unregister(key.fileobj)
                os.close(key.fileobj)
                continue
            output_counts[(name, stream)] += len(chunk)
            if output_counts[(name, stream)] > limit:
                # The paired boundary owns this pipe and therefore knows the
                # exact role.  Return only the closed label, never text.
                return name
            output_sinks[(name, stream)].write(chunk)
        return None

    def terminate_and_reap() -> None:
        """Use the privileged helper, never a blocking waitpid, for live roles."""
        nonlocal command_write
        if not launched:
            return
        try:
            command(b"TB"); command(b"TC")
        except RuntimeError:
            # A dead helper is a closed failure: the outer launcher's small
            # safety margin kills its process group rather than letting this
            # boundary wait forever for a distinct-UID child.
            pass
        grace_end = time.monotonic() + 0.10
        while len(statuses) != len(launched) and time.monotonic() < grace_end:
            collect_statuses(); drain_ready(0.01)
        if len(statuses) != len(launched):
            try:
                command(b"KB"); command(b"KC")
            except RuntimeError:
                pass
            kill_end = time.monotonic() + 0.25
            while len(statuses) != len(launched) and time.monotonic() < kill_end:
                collect_statuses(); drain_ready(0.01)
        if len(statuses) != len(launched):
            raise RuntimeError("teardown helper could not reap role processes")
        if command_write >= 0:
            try:
                os.write(command_write, b"XX")
            except OSError:
                pass
            os.close(command_write)
            command_write = -1
        if helper_pid is not None:
            helper_end = time.monotonic() + 0.25
            while time.monotonic() < helper_end:
                waited, _ = os.waitpid(helper_pid, os.WNOHANG)
                if waited:
                    return
                time.sleep(0.01)
            raise RuntimeError("teardown helper did not exit")

    try:
        # Fork both stubs while setup still holds privilege.  They cannot execute
        # payload code until the boundary drops to its broker identity and opens
        # their individual start barriers.
        for name, argv, env, uid in roles:
            start_read, start_write = os.pipe()
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            pid = os.fork()
            if pid == 0:
                for fd in (start_write, stdout_read, stderr_read):
                    os.close(fd)
                _role_stub(argv, cwd=str(config["cwd"]), env=env, uid=uid, start_fd=start_read,
                           stdout_fd=stdout_write, stderr_fd=stderr_write,
                           structural_identity=structural_identity)
            os.close(start_read); os.close(stdout_write); os.close(stderr_write)
            launched[name] = (pid, start_write, stdout_read, stderr_read)
        command_read, command_write = os.pipe()
        helper_pid = os.fork()
        if helper_pid == 0:
            os.close(command_write)
            _teardown_helper(command_read, launched["broker"][0], launched["child"][0])
            os._exit(0)
        os.close(command_read)
        # This is a one-way, trusted report to the still-privileged outer
        # launcher.  It is deliberately sent before the broker drops UID, so a
        # helper failure cannot strand a distinct-UID role beyond cleanup.
        if supervisor_fd >= 0:
            _write_supervisor_registration(supervisor_fd, {"boundary": os.getpid(), "broker": launched["broker"][0],
                                                            "child": launched["child"][0], "helper": helper_pid})
            os.close(supervisor_fd)
            supervisor_fd = -1
        if os.getuid() != config["uid"]:
            try:
                os.setuid(config["uid"])
            except PermissionError:
                if not structural_identity:
                    raise
        for _, start_write, *_ in launched.values():
            os.write(start_write, b"S")
            os.close(start_write)

        selector = selectors.DefaultSelector()
        for name, (_, _, stdout_fd, stderr_fd) in launched.items():
            selector.register(stdout_fd, selectors.EVENT_READ, (name, "stdout"))
            selector.register(stderr_fd, selectors.EVENT_READ, (name, "stderr"))
            for stream in ("stdout", "stderr"):
                output_counts[(name, stream)] = 0
                output_sinks[(name, stream)] = tempfile.TemporaryFile(mode="w+b")
        terminating = False
        kill_at: float | None = None
        deadline = time.monotonic() + float(timeout)
        while len(statuses) != len(launched) or selector.get_map():
            collect_statuses()
            if any(status != 0 for status in statuses.values()) and not terminating:
                terminating = True; kill_at = time.monotonic() + 0.10
                command(b"TB"); command(b"TC")
            if time.monotonic() >= deadline and not terminating:
                execution_timeout_roles = tuple(name for name in launched if name not in statuses)
                terminating = True; kill_at = time.monotonic() + 0.10
                command(b"TB"); command(b"TC")
            if terminating and kill_at is not None and time.monotonic() >= kill_at:
                command(b"KB"); command(b"KC")
                kill_at = None
            flooded = drain_ready(0.02)
            if flooded is not None and not terminating:
                output_limit_role = flooded
                terminating = True; kill_at = time.monotonic() + 0.10
                command(b"TB"); command(b"TC")
        terminate_and_reap()
        helper_pid = None
        return {"broker_pid": os.getpid(),
                "role_pids": {"broker": launched["broker"][0], "child": launched["child"][0]},
                "broker": {"exit_code": statuses["broker"]},
                "child": {"exit_code": statuses["child"]},
                "execution_timeout_roles": list(execution_timeout_roles),
                "output_limit_role": output_limit_role}
    finally:
        if supervisor_fd >= 0:
            try:
                os.close(supervisor_fd)
            except OSError:
                pass
        if command_write >= 0:
            try:
                terminate_and_reap()
            except RuntimeError:
                try:
                    os.close(command_write)
                except OSError:
                    pass
                command_write = -1
        if selector is not None:
            selector.close()
        for sink in output_sinks.values():
            sink.close()
        for name, (pid, *fds) in launched.items():
            for fd in fds:
                try: os.close(fd)
                except OSError: pass
            # The bounded loops above own reaping.  Do not turn an exception
            # path into an unbounded wait on a TERM-ignoring payload.
        if helper_pid is not None:
            helper_end = time.monotonic() + 0.25
            while time.monotonic() < helper_end:
                try:
                    waited, _ = os.waitpid(helper_pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if waited:
                    break
                time.sleep(.01)


def _pair(config: Mapping[str, Any], supervisor_fd: int = -1) -> dict[str, Any]:
    """Create a single-threaded broker boundary without controller-side fork hooks."""
    limit = config.get("max_output_bytes")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("launcher output budget is invalid")
    raw = json.dumps(dict(config), sort_keys=True, separators=(",", ":")).encode("utf-8")
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw)
    finally:
        os.close(write_fd)
    try:
        command = [sys.executable, "-m", "implbench.harness.scorer_launcher", "--broker-pair", "--config-fd", str(read_fd)]
        pass_fds = (read_fd,)
        if supervisor_fd >= 0:
            command.extend(("--supervisor-fd", str(supervisor_fd)))
            pass_fds += (supervisor_fd,)
        boundary = subprocess.Popen(
            command,
            cwd=str(config["cwd"]), env={"PATH": os.defpath, "PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # This fresh, single-threaded boundary must retain setup privilege
            # until it has forked both role stubs and the teardown helper.
            shell=False, pass_fds=pass_fds,
        )
    finally:
        os.close(read_fd)
        if supervisor_fd >= 0:
            os.close(supervisor_fd)
    # The boundary's stdout is the fixed launcher status schema, never a role's
    # stdout/stderr.  This is the one permitted release through this supervisor.
    boundary_result = _drain({"boundary": boundary}, limit, release=True)["boundary"]
    if boundary_result[0] != 0:
        raise RuntimeError(boundary_result[2].strip() or "trusted broker boundary failed")
    value = json.loads(boundary_result[1])
    if not isinstance(value, Mapping) or value.get("ok") is not True or not isinstance(value.get("result"), Mapping):
        raise RuntimeError("trusted broker boundary reported invalid status")
    return dict(value["result"])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        broker_pair = (len(argv) in (3, 5) and argv[:2] == ["--broker-pair", "--config-fd"] and argv[2].isdigit()
                       and (len(argv) == 3 or (argv[3] == "--supervisor-fd" and argv[4].isdigit())))
        direct = (len(argv) in (2, 4) and argv[0] == "--config-fd" and argv[1].isdigit()
                  and (len(argv) == 2 or (argv[2] == "--supervisor-fd" and argv[3].isdigit())))
        if not broker_pair and not direct:
            raise ValueError("launcher requires one inherited configuration descriptor")
        config = _read_config(int(argv[2] if broker_pair else argv[1]))
        supervisor_fd = int(argv[-1]) if len(argv) in (4, 5) else -1
        result = (_broker_pair(config, supervisor_fd) if broker_pair
                  else (_pair(config, supervisor_fd) if "child_argv" in config else _normal(config)))
        print(json.dumps({"ok": True, "result": result}, sort_keys=True, separators=(",", ":")))
        return 0
    except BaseException as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True, separators=(",", ":")))
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
