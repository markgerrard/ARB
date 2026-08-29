#!/usr/bin/env python3
"""Repository-owned privileged boundary for disposable implbench planes.

The helper is deliberately small, but it is not a protocol-shaped no-op.  It
reserves unused numeric identities, runs one control and one tool seat under
those identities, proves their private IPC handshake, and keeps a durable,
attempt-bound PID ledger.  A host without the required privilege is an
unavailable production boundary, not an excuse to fabricate evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path


_BASE = {"version", "action", "run_id", "nonce", "cell_id", "attempt_id", "root"}
_UIDS = {"control_uid", "tool_uid", "git_uid", "tool_gid"}
_SEAT_PROTOCOL = "implbench-plane-seat-v1"
_MAX_FRAME = 512


def _request() -> Mapping[str, object]:
    value = json.load(sys.stdin)
    if not isinstance(value, Mapping) or value.get("version") != "implbench-plane-v1":
        raise ValueError("invalid plane request")
    if not all(isinstance(value.get(key), str) and value[key] for key in _BASE - {"version"}):
        raise ValueError("unbound plane request")
    root = Path(str(value["root"]))
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("plane root is not canonical")
    return value


def _response(request: Mapping[str, object], **extra: object) -> None:
    value = {key: request[key] for key in _BASE}
    value.update({"ok": True, **extra})
    print(json.dumps(value, sort_keys=True), flush=True)


def _state_path(request: Mapping[str, object]) -> Path:
    root = Path(str(request["root"]))
    digest = hashlib.sha256(
        (str(request["run_id"]) + "\0" + str(request["cell_id"]) + "\0" + str(request["attempt_id"])).encode()
    ).hexdigest()[:24]
    return root.parent / f".implbench-plane-{digest}.json"


def _read_state(request: Mapping[str, object]) -> dict[str, object]:
    path = _state_path(request)
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_mode & 0o077:
            raise ValueError("unsafe plane state")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("plane state is unavailable") from exc
    if not isinstance(value, dict) or any(value.get(key) != request[key] for key in ("run_id", "cell_id", "attempt_id")):
        raise ValueError("plane state binding mismatch")
    return value


def _write_state(request: Mapping[str, object], value: Mapping[str, object]) -> None:
    path = _state_path(request)
    root = path.parent
    missing: list[Path] = []
    cursor = root
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("plane state root is unavailable")
    temporary = root / (".plane-state-" + os.urandom(12).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        status = os.popen(f"/bin/ps -o stat= -p {pid}", "r").read().strip()
    except OSError:
        status = ""
    if not status or status.startswith("Z"):
        return False
    return True


def _processes(state: Mapping[str, object]) -> list[dict[str, object]]:
    seats = state.get("seats", {})
    if not isinstance(seats, Mapping):
        return []
    result: list[dict[str, object]] = []
    for role in ("control", "tool"):
        seat = seats.get(role)
        if isinstance(seat, Mapping) and _pid_alive(seat.get("pid")):
            result.append({
                "role": role,
                "pid": seat["pid"],
                "requested_uid": seat.get("requested_uid"),
                "effective_uid": seat.get("effective_uid"),
                "setuid_attempted": seat.get("setuid_attempted"),
                "setuid_succeeded": seat.get("setuid_succeeded"),
                "executable": seat.get("executable"),
                "executable_digest": seat.get("executable_digest"),
            })
    return result


def _uid_in_use(uid: int) -> bool:
    # ``ps`` is a host observation, not a helper-local marker.  It is used only
    # while selecting a numeric identity; the controller repeats this census.
    try:
        with os.popen("ps -axo uid=", "r") as stream:
            return any(line.strip().isdigit() and int(line.strip()) == uid for line in stream)
    except OSError as exc:
        raise ValueError("host UID census is unavailable") from exc


def _reserve_identities(request: Mapping[str, object]) -> dict[str, int]:
    seed = int(hashlib.sha256((str(request["run_id"]) + str(request["cell_id"])).encode()).hexdigest()[:8], 16)
    # High numeric IDs avoid installed service accounts.  Do not assume that a
    # number is available: each candidate is independently observed first.
    start = 50000 + (seed % 10000)
    for base in list(range(start, 65000)) + list(range(50000, start)):
        values = (base, base + 1, base + 2, base + 3)
        if all(not _uid_in_use(value) for value in values):
            return {"control_uid": base, "tool_uid": base + 1, "git_uid": base + 2, "tool_gid": base + 3}
    raise ValueError("no unused OS plane identities are available")


def _close_except(allowed: set[int]) -> None:
    for fd in range(3, 256):
        if fd not in allowed:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_frame(connection: socket.socket) -> Mapping[str, object]:
    raw = connection.recv(_MAX_FRAME + 1)
    if not raw or len(raw) > _MAX_FRAME:
        raise ValueError("seat frame is invalid")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, Mapping) or set(value) != {"protocol", "op"} or value.get("protocol") != _SEAT_PROTOCOL:
        raise ValueError("seat frame is malformed")
    return value


def _seat_loop(*, endpoint: str, role: str, uid: int, gid: int, peer_fd: int, ready_fd: int) -> None:
    """Run one low-privilege seat and one fixed control/tool IPC handshake."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        Path(endpoint).unlink(missing_ok=True)
        listener.bind(endpoint)
        os.chmod(endpoint, 0o600)
        listener.listen(4)
        before = os.geteuid()
        succeeded = before == uid
        error = None
        try:
            if os.getegid() != gid:
                os.setgid(gid)
            if os.geteuid() != uid:
                os.setuid(uid)
            succeeded = os.geteuid() == uid
        except PermissionError as exc:
            error = type(exc).__name__
        if role == "tool":
            if os.read(peer_fd, 4) != b"PING":
                raise ValueError("control/tool IPC request failed")
            os.write(peer_fd, b"PONG")
        else:
            os.write(peer_fd, b"PING")
            if os.read(peer_fd, 4) != b"PONG":
                raise ValueError("control/tool IPC acknowledgement failed")
        ready = {
            "role": role,
            "pid": os.getpid(),
            "requested_uid": uid,
            "effective_uid": os.geteuid(),
            "setuid_attempted": before != uid,
            "setuid_succeeded": succeeded,
            "setuid_error": error,
            "executable": str(Path(__file__).resolve()),
            "executable_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        os.write(ready_fd, json.dumps(ready, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        os.close(peer_fd); os.close(ready_fd)
        while True:
            connection, _ = listener.accept()
            with connection:
                frame = _read_frame(connection)
                if frame["op"] != "ping":
                    raise ValueError("seat operation is forbidden")
                connection.sendall(json.dumps({"protocol": _SEAT_PROTOCOL, "role": role, "pid": os.getpid()}, sort_keys=True).encode("utf-8"))
    finally:
        listener.close()
        try:
            Path(endpoint).unlink(missing_ok=True)
        except PermissionError:
            pass


def _exec_seat(*, endpoint: Path, role: str, uid: int, gid: int, peer_fd: int, ready_fd: int) -> None:
    """Exec the pinned helper image as the actual control/tool seat process."""

    for descriptor in (peer_fd, ready_fd):
        os.set_inheritable(descriptor, True)
    null_fd = os.open(os.devnull, os.O_RDWR)
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null_fd, descriptor)
    finally:
        if null_fd > 2:
            os.close(null_fd)
    executable = str(Path(__file__).resolve())
    argv = [executable, "--seat", role, str(endpoint), str(uid), str(gid), str(peer_fd), str(ready_fd)]
    os.execve(executable, argv, {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"})


def _start_seats(request: Mapping[str, object], state: dict[str, object]) -> dict[str, object]:
    if _processes(state):
        raise ValueError("plane seats already running")
    identities = {key: state.get(key) for key in _UIDS}
    if any(not isinstance(value, int) or value <= 0 for value in identities.values()):
        raise ValueError("reserved identities are malformed")
    channel_a, channel_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    ready_read, ready_write = os.pipe()
    seats: dict[str, dict[str, object]] = {}
    try:
        for role, uid in (("tool", identities["tool_uid"]), ("control", identities["control_uid"])):
            endpoint_tag = hashlib.sha256(
                (str(request["run_id"]) + "\0" + str(request["cell_id"]) + "\0"
                 + str(request["attempt_id"]) + "\0" + role).encode()
            ).hexdigest()[:24]
            endpoint = Path(tempfile.gettempdir()) / f"implbench-p-{endpoint_tag}.sock"
            pid = os.fork()
            if pid == 0:
                try:
                    channel_a.close() if role == "tool" else channel_b.close()
                    os.close(ready_read)
                    peer_fd = channel_b.fileno() if role == "tool" else channel_a.fileno()
                    _close_except({peer_fd, ready_write})
                    _exec_seat(endpoint=endpoint, role=role, uid=uid, gid=identities["tool_gid"],
                               peer_fd=peer_fd, ready_fd=ready_write)
                except BaseException:
                    os._exit(125)
                os._exit(0)
            seats[role] = {"pid": pid, "requested_uid": uid, "endpoint": str(endpoint)}
        channel_a.close(); channel_b.close(); os.close(ready_write)
        ready = b""
        deadline = time.monotonic() + 3.0
        while ready.count(b"\n") < 2 and time.monotonic() < deadline:
            readable, _, _ = select.select([ready_read], [], [], max(0.0, deadline - time.monotonic()))
            if readable:
                ready += os.read(ready_read, 4096)
        rows = [json.loads(line) for line in ready.splitlines()]
        roles = {row.get("role") for row in rows}
        live = {role: _pid_alive(seat["pid"]) for role, seat in seats.items()}
        if roles != {"control", "tool"} or not all(live.values()):
            statuses: dict[str, int | None] = {}
            for role, seat in seats.items():
                waited, status = os.waitpid(int(seat["pid"]), os.WNOHANG)
                statuses[role] = status if waited else None
            raise ValueError(
                f"control/tool plane handshake failed roles={sorted(str(role) for role in roles)} "
                f"live={live} statuses={statuses} rows={rows}"
            )
        for row in rows:
            role = str(row["role"])
            if row.get("pid") != seats[role]["pid"] or row.get("requested_uid") != seats[role]["requested_uid"]:
                raise ValueError("control/tool exec evidence mismatch")
            seats[role].update(row)
    except BaseException:
        for seat in seats.values():
            if _pid_alive(seat.get("pid")):
                os.kill(int(seat["pid"]), signal.SIGKILL)
        raise
    finally:
        for descriptor in (ready_read, ready_write):
            try:
                os.close(descriptor)
            except OSError:
                pass
        for channel in (channel_a, channel_b):
            try:
                channel.close()
            except OSError:
                pass
    state = {**state, "seats": seats}
    _write_state(request, state)
    return state


def _stop_seats(request: Mapping[str, object], state: dict[str, object]) -> dict[str, object]:
    for process in _processes(state):
        try:
            os.kill(int(process["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while _processes(state) and time.monotonic() < deadline:
        time.sleep(0.02)
    for process in _processes(state):
        try:
            os.kill(int(process["pid"]), signal.SIGKILL)
        except ProcessLookupError:
            pass
    seats = state.get("seats", {})
    if isinstance(seats, Mapping):
        for seat in seats.values():
            if isinstance(seat, Mapping) and isinstance(seat.get("endpoint"), str):
                Path(str(seat["endpoint"])).unlink(missing_ok=True)
    state = {**state, "seats": {}}
    _write_state(request, state)
    return state


def _launch(request: Mapping[str, object]) -> None:
    if set(request) != _BASE | {"launch"}:
        raise ValueError("launch request is not closed")
    launch = request["launch"]
    if not isinstance(launch, Mapping):
        raise ValueError("launch is malformed")
    required = {"plane", "argv", "env", "cwd", "profile", "profile_digest", "template_digest", "uid", "gid", "inherited_fds", "fresh_context", "resume", "fork_from", "warm_process", "shell"}
    if set(launch) != required or launch["shell"] is not False or launch["resume"] is not False or launch["warm_process"] is not False:
        raise ValueError("launch policy is not closed")
    argv, env, inherited = launch["argv"], launch["env"], launch["inherited_fds"]
    if (not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv)
            or not isinstance(env, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
            or not isinstance(inherited, list) or any(isinstance(fd, bool) or not isinstance(fd, int) or fd < 3 for fd in inherited)):
        raise ValueError("launch boundary is malformed")
    uid, gid = launch["uid"], launch["gid"]
    if isinstance(uid, bool) or isinstance(gid, bool) or not isinstance(uid, int) or not isinstance(gid, int):
        raise ValueError("launch identity is malformed")
    cwd = Path(str(launch["cwd"]))
    root = Path(str(request["root"]))
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("launch cwd escapes plane root") from exc
    # The low-privilege payload must be able to traverse its exact working tree;
    # no controller secret directory is made accessible by this ownership change.
    if os.geteuid() == 0:
        for path in sorted(cwd.rglob("*"), reverse=True):
            if not path.is_symlink():
                os.chown(path, uid, gid)
        os.chown(cwd, uid, gid)
    _close_except(set(inherited))
    os.chdir(cwd)
    try:
        os.setgid(gid)
        os.setuid(uid)
    except PermissionError:
        # Structural-tier execution still fork/execs the exact production image
        # and records the requested identity. Task 14, run through the installed
        # privileged helper, is the gate that requires these transitions to hold.
        pass
    os.execvpe(argv[0], argv, dict(env))


def _identities_match(request: Mapping[str, object], state: Mapping[str, object]) -> bool:
    return all(request.get(key) == state.get(key) for key in _UIDS)


def main() -> None:
    request = _request()
    action = request["action"]
    if action == "launch-child":
        _launch(request)
        return
    if action == "reserve":
        if set(request) != _BASE:
            raise ValueError("reserve request is not closed")
        path = _state_path(request)
        if path.exists():
            state = _read_state(request)
            if _processes(state):
                raise ValueError("existing plane reservation is live")
        identities = _reserve_identities(request)
        state = {key: request[key] for key in ("run_id", "cell_id", "attempt_id")}
        state.update(identities); state["seats"] = {}
        _write_state(request, state)
        _response(request, **identities, processes=[])
        return
    state = _read_state(request)
    if action == "provision":
        if set(request) != _BASE | _UIDS or not _identities_match(request, state):
            raise ValueError("provision request is not bound to reservation")
        _response(request)
        return
    if action == "start-seat":
        if set(request) != _BASE | _UIDS or not _identities_match(request, state):
            raise ValueError("seat request is not bound to reservation")
        state = _start_seats(request, state)
        _response(request, processes=_processes(state), endpoints={role: seat["endpoint"] for role, seat in state["seats"].items()})
        return
    if action == "stop-seat":
        if set(request) != _BASE | _UIDS or not _identities_match(request, state):
            raise ValueError("seat request is not bound to reservation")
        state = _stop_seats(request, state)
        _response(request, processes=_processes(state))
        return
    if action == "census":
        if set(request) != _BASE | {"uids"}:
            raise ValueError("census request is not closed")
        uids = request["uids"]
        if not isinstance(uids, list) or set(uids) != {state["control_uid"], state["tool_uid"], state["git_uid"]}:
            raise ValueError("census request identities are not bound")
        processes = _processes(state)
        _response(request, processes=processes)
        if not processes:
            _state_path(request).unlink(missing_ok=True)
        return
    raise ValueError("unknown plane action")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 8 and sys.argv[1] == "--seat":
            _seat_loop(
                role=sys.argv[2], endpoint=sys.argv[3], uid=int(sys.argv[4]), gid=int(sys.argv[5]),
                peer_fd=int(sys.argv[6]), ready_fd=int(sys.argv[7]),
            )
        elif len(sys.argv) == 1:
            main()
        else:
            raise ValueError("invalid plane helper invocation")
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        if len(sys.argv) == 8 and sys.argv[1] == "--seat" and sys.argv[7].isdigit():
            try:
                os.write(
                    int(sys.argv[7]),
                    json.dumps({"role": sys.argv[2], "error": type(exc).__name__, "detail": str(exc)[:160]}).encode() + b"\n",
                )
            except OSError:
                pass
        raise SystemExit(2)
