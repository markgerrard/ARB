from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

from implbench.harness.classifier import ClassificationInput, classify
from implbench.harness.importer import ImportLimits, ImporterError, import_from_descriptor_child
from implbench.harness.sandbox import SandboxPaths, build_launch_spec


ROOT = Path(__file__).parents[3]


def _paths(root: Path) -> SandboxPaths:
    root.mkdir(mode=0o700)
    values = {name: root / name for name in (
        "work", "git", "evidence", "base", "sibling", "credentials", "key", "home", "runtime",
    )}
    for path in values.values():
        path.mkdir(mode=0o700)
    return SandboxPaths(
        root, values["work"], values["git"], values["evidence"], values["base"], values["sibling"],
        values["credentials"], values["key"], values["home"], values["runtime"],
    )


def _source(root: Path) -> Path:
    body = b"r22c importer boundary\n"
    raw = b"blob " + str(len(body)).encode() + b"\0" + body
    oid = hashlib.sha1(raw).hexdigest()
    loose = root / ".git" / "objects" / oid[:2] / oid[2:]
    loose.parent.mkdir(parents=True)
    loose.write_bytes(zlib.compress(raw))
    return root


def _assert_infrastructure_unknown() -> None:
    classification = classify(ClassificationInput(infrastructure_failure="importer-child-boundary"))
    assert classification.reason == "infrastructure"
    assert set(classification.values()) == {"UNKNOWN"}


def _assert_pid_absent(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"importer child PID {pid} survived cleanup")


def _script_spawner(
    mode: str,
    *,
    destination: Path,
    limits: ImportLimits,
    pids: list[int],
    marker: Path,
):
    """Return an actual child spawner with one closed-response fault per mode."""

    response = {
        "version": "implbench-import-child-v1",
        "ok": True,
        "pid": 0,
        "uid": os.getuid(),
        "profile_digest": "profile",
        "template_digest": "template",
        "limits": limits.__dict__,
        "result": {
            "bundle": str(destination), "bundle_digest": "d" * 64,
            "files": 0, "bytes": 0, "object_ids": [],
        },
    }
    script = """
import json
import os
import signal
import sys
import time
from pathlib import Path

request_fd = int(sys.argv[1])
mode = sys.argv[2]
response = json.loads(sys.argv[3])
marker = Path(sys.argv[4])
if mode == "pre-request-exit":
    os.close(request_fd)
    marker.write_text("closed", encoding="utf-8")
    time.sleep(60)
else:
    os.read(request_fd, 65536)
if mode == "timeout":
    time.sleep(60)
elif mode == "signal":
    os.kill(os.getpid(), signal.SIGTERM)
elif mode == "oversized":
    sys.stdout.write("x" * 65537)
elif mode == "truncated":
    sys.stdout.write("{\\\"version\\\":")
else:
    response["pid"] = os.getpid()
    if mode == "wrong-pid":
        response["pid"] += 1
    elif mode == "wrong-uid":
        response["uid"] += 1
    elif mode == "wrong-profile":
        response["profile_digest"] = "0" * 64
    elif mode == "wrong-template":
        response["template_digest"] = "0" * 64
    elif mode == "wrong-limits":
        response["limits"] = {}
    sys.stdout.write(json.dumps(response, sort_keys=True))
sys.stdout.flush()
"""

    def spawn(spec, fds):
        request_fd = int(spec.argv[-1])
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(request_fd), mode, json.dumps(response), str(marker)],
            cwd=spec.cwd, env=spec.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, pass_fds=fds, start_new_session=True,
        )
        pids.append(process.pid)
        if mode == "pre-request-exit":
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), "fault child did not close its request descriptor"
        return process

    return spawn


def _real_spawner(pids: list[int]):
    def spawn(spec, fds):
        process = subprocess.Popen(
            spec.argv, cwd=spec.cwd, env=spec.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, pass_fds=fds, start_new_session=True,
        )
        pids.append(process.pid)
        return process

    return spawn


@pytest.mark.parametrize(
    "mode",
    (
        "timeout", "signal", "oversized", "truncated", "wrong-pid", "wrong-uid",
        "wrong-profile", "wrong-template", "wrong-limits", "pre-request-exit", "resource-exhaustion",
    ),
)
def test_r22c_importer_failure_matrix_fails_closed_and_reaps_exact_child(tmp_path: Path, mode: str) -> None:
    """Every malformed importer outcome is infrastructure UNKNOWN with no child/spool/socket residue."""

    paths = _paths(tmp_path / "cell")
    source = _source(tmp_path / "source")
    destination = paths.runtime / "bundle"
    limits = (
        ImportLimits(max_wall_time_s=0.05)
        if mode == "timeout"
        else ImportLimits(max_total_bytes=1)
        if mode == "resource-exhaustion"
        else ImportLimits()
    )
    spec = build_launch_spec(
        "importer", paths, uid=os.getuid(), gid=os.getgid(), argv=("unused",),
        extra_env={"PYTHONPATH": str(ROOT / "bench")},
    )
    pids: list[int] = []
    marker = tmp_path / "request-closed"
    spawner = (
        _real_spawner(pids)
        if mode == "resource-exhaustion"
        else _script_spawner(mode, destination=destination, limits=limits, pids=pids, marker=marker)
    )
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ImporterError):
            import_from_descriptor_child(
                source_fd, destination, launch_spec=spec, limits=limits,
                allow_unprofiled_test=False, child_spawner=spawner,
            )
    finally:
        os.close(source_fd)

    assert len(pids) == 1
    _assert_pid_absent(pids[0])
    assert not destination.exists()
    assert not list(destination.parent.glob(".implbench-import-*"))
    assert not list(paths.runtime.rglob("*.sock"))
    _assert_infrastructure_unknown()
