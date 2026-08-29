from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import pytest

from implbench.harness.cell_runtime import PlaneIdentities
from implbench.harness.git_service import GitRPCError
from implbench.harness.runtime import (
    ProductionRuntimeUnavailable,
    _SystemPlaneProvisioner,
    build_production_controller,
)
from implbench.harness.sandbox import LaunchError, SandboxPaths, build_launch_spec


ROOT = Path(__file__).parents[3]


def _manifest(evidence: Path) -> dict[str, object]:
    base = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    controls = {
        name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
        for name in (
            "temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior",
            "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts",
        )
    }
    controls["reasoning"] = {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"}
    task_ids = ("c1-permissive-boundary", "c1-token-bucket", "c2-parser", "c3-refactor", "c4-rail", "c5-artifact", "c6-scope", "c7-provenance")
    return {
        "run_id": "oi-pi-bakeoff-r22b-production-entry",
        "source": {"realpath": str(ROOT), "commit": base},
        "base_sha": base,
        "seed": "00" * 32,
        "tasks": [{"task_id": task_id, "fixture_sha": base} for task_id in task_ids],
        "arms": [
            {"arm": "glm-pi", "engine": "pi-sdk", "provider": "zai", "harness": "pi", "model": "glm-5.2", "agent_prefix": "pi-glm"},
            {"arm": "glm-zcode", "engine": "openinterpreter", "provider": "zai-coding-plan", "harness": "zcode", "model": "glm-5.2", "agent_prefix": "oi-glm"},
            {"arm": "kimi-pi", "engine": "pi-sdk", "provider": "kimi-coding", "harness": "pi", "model": "k2p7", "agent_prefix": "pi-kimi"},
            {"arm": "kimi-cli", "engine": "openinterpreter", "provider": "kimi-for-coding", "harness": "kimi-cli", "model": "k2p7", "agent_prefix": "oi-kimi"},
        ],
        "controls": controls,
        "capabilities": {"classes": ["read", "write", "shell"]},
        "corpus_sha": "c" * 64,
        "evidence": {"root": str(evidence)},
    }


def _write_plane_helper(path: Path) -> None:
    """Executable structural helper: records one real request, then execs its exact argv."""

    root_helper = path.with_name("plane-root-helper.py")
    root_helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, resource, sys, tempfile\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "root = Path(request['root'])\n"
        "trace = root.parents[2] / 'helper-trace.ndjson'\n"
        "trace.parent.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
        "def descriptors():\n"
        "    values = []\n"
        "    for descriptor in range(256):\n"
        "        try: os.fstat(descriptor)\n"
        "        except OSError: continue\n"
        "        values.append(descriptor)\n"
        "    return values\n"
        "open_fds = descriptors()\n"
        "inherited = set(request.get('launch', {}).get('inherited_fds', ()))\n"
        "for descriptor in open_fds:\n"
        "    if descriptor > 2 and descriptor not in inherited:\n"
        "        try: os.close(descriptor)\n"
        "        except OSError: pass\n"
        "open_fds_after = descriptors()\n"
        "with trace.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'pid': os.getpid(), 'request': request, 'open_fds': open_fds, 'open_fds_after': open_fds_after}, sort_keys=True) + '\\n')\n"
        "    stream.flush(); os.fsync(stream.fileno())\n"
        "if request['action'] != 'launch-child':\n"
        "    response = {'version': 'implbench-plane-v1', 'ok': True}\n"
        "    if request['action'] == 'reserve':\n"
        "        response.update({'control_uid': os.getuid(), 'tool_uid': os.getuid() + 1, 'git_uid': os.getuid() + 2, 'tool_gid': os.getgid(), 'processes': []})\n"
        "    elif request['action'] == 'census':\n"
        "        response['processes'] = []\n"
        "    response.update({key: request[key] for key in ('action', 'run_id', 'cell_id', 'attempt_id', 'root', 'nonce')})\n"
        "    print(json.dumps(response, sort_keys=True))\n"
        "    raise SystemExit(0)\n"
        "launch = request['launch']\n"
        "if os.geteuid() == 0 and launch['uid'] != 0:\n"
        "    _target = root\n"
        "    _cursor, _stop = root.parent, Path(tempfile.gettempdir()).resolve()\n"
        "    while _cursor != _stop and os.stat(_cursor).st_uid == 0:\n"
        "        os.chown(_cursor, launch['uid'], launch['gid'])\n"
        "        _cursor = _cursor.parent\n"
        "    for _entry in sorted(_target.rglob('*'), reverse=True):\n"
        "        os.chown(_entry, launch['uid'], launch['gid'])\n"
        "    os.chown(_target, launch['uid'], launch['gid'])\n"
        "if launch['plane'] == 'importer':\n"
        "    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)\n"
        "    _address = min(256 * 1024 * 1024, _hard) if _hard != resource.RLIM_INFINITY else 256 * 1024 * 1024\n"
        "    try:\n"
        "        resource.setrlimit(resource.RLIMIT_AS, (_address, _hard))\n"
        "    except ValueError:\n"
        "        pass  # Darwin refuses RLIMIT_AS even from this root fixture; child fails closed.\n"
        "os.chdir(launch['cwd'])\n"
        "if os.getegid() != launch['gid']:\n"
        "    os.setgid(launch['gid'])\n"
        "if os.geteuid() != launch['uid']:\n"
        "    os.setuid(launch['uid'])\n"
        "os.execvpe(launch['argv'][0], launch['argv'], launch['env'])\n",
        encoding="utf-8",
    )
    root_helper.chmod(0o700)
    # The production spawner has already closed every descriptor except the
    # declared stdio/pass-fd set.  The hermetic runner records that set and
    # applies the requested uid/gid only when the test target differs.
    path.write_text(
        f"#!/bin/sh\nexec {root_helper}\n",
        encoding="utf-8",
    )
    path.chmod(0o750)


class _HermeticACL:
    """Keeps Valkey outside this structural-only, no-live-cell characterization."""

    def provision(self, _identity: object) -> None:
        return None

    def close(self, _identity: object) -> None:
        return None


class _StructuralProvisioner:
    """Delegates child exec to the pinned production helper without claiming Task-14 UID proof."""

    real = True

    def __init__(self, helper: _SystemPlaneProvisioner) -> None:
        self.helper = helper
        default = os.getuid() + 101
        self.identities = PlaneIdentities(
            int(os.environ.get("R22B_TEST_CONTROL_UID", default)),
            int(os.environ.get("R22B_TEST_TOOL_UID", default + 1)),
            int(os.environ.get("R22B_TEST_GIT_UID", default + 2)),
            int(os.environ.get("R22B_TEST_TOOL_GID", os.getgid())),
        )

    def reserve_identities(self, cell_id: str, *, attempt_id: str, root: Path) -> PlaneIdentities:
        self.helper._call("reserve", cell_id=cell_id, attempt_id=attempt_id, root=str(root))
        return self.identities

    def provision_planes(self, paths: object, identities: PlaneIdentities, *, attempt_id: str) -> None:
        self.helper._call(
            "provision", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root),
            control_uid=identities.control, tool_uid=identities.tool, git_uid=identities.git,
            tool_gid=identities.tool_gid,
        )

    def start_seat_daemon(self, paths: object, identities: PlaneIdentities, *, attempt_id: str) -> None:
        self.helper._call("start-seat", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root))

    def stop_seat_daemon(self, paths: object, identities: PlaneIdentities, *, attempt_id: str) -> None:
        self.helper._call("stop-seat", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root))

    def prove_absent(self, paths: object, identities: PlaneIdentities, *, attempt_id: str) -> bool:
        del identities
        self.helper._call("census", cell_id=paths.cell_id, attempt_id=attempt_id, root=str(paths.cell_root))
        return True

    def spawn_child(self, *args: object, **kwargs: object):
        return self.helper.spawn_child(*args, **kwargs)


class _ForgedPidProcess:
    """Test-only proxy: the real child runs, but its claimed PID cannot match."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self.pid = process.pid + 1

    def __getattr__(self, name: str):
        return getattr(self._process, name)


class _FaultingStructuralProvisioner(_StructuralProvisioner):
    def __init__(self, helper: _SystemPlaneProvisioner, fault: str) -> None:
        super().__init__(helper)
        self.fault = fault
        self.child_pids: list[int] = []

    def spawn_child(self, specification: object, **kwargs: object):
        spec = specification
        if self.fault == "uid":
            spec = type(spec)(**{**spec.__dict__, "uid": self.identities.control})
        process = self.helper.spawn_child(spec, **kwargs)
        self.child_pids.append(process.pid)
        return _ForgedPidProcess(process) if self.fault == "pid" else process


def _trace(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (root / "helper-trace.ndjson").read_text(encoding="utf-8").splitlines()]


def _assert_secret_absent(value: object, secret: str) -> None:
    assert secret not in json.dumps(value, sort_keys=True)


def _assert_pid_absent(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"PID {pid} survived cleanup")


def _named_process_census(*markers: str) -> list[str]:
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,uid=,command="], text=True)
    return [line for line in output.splitlines() if any(marker in line for marker in markers)]


def test_r22b_production_cell_uses_pinned_helper_and_excludes_controller_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """The production cell—not a direct child seam—drives both isolated child crossings."""

    if os.geteuid() != 0:
        # Re-enter this exact test as root so the helper can honour the
        # requested uid/gid transition.  Darwin's unavailable RLIMIT_AS is
        # intentionally checked as a fail-closed production-child result.
        nested = tmp_path / "root-pytest"
        command = [
            "/usr/bin/sudo", "-n", "/usr/bin/env",
            f"PYTHONPATH={ROOT / 'bench'}",
            f"GIT_CONFIG_COUNT=1", f"GIT_CONFIG_KEY_0=safe.directory", f"GIT_CONFIG_VALUE_0={ROOT}",
            f"R22B_TEST_CONTROL_UID={os.getuid() + 2}",
            f"R22B_TEST_TOOL_UID={os.getuid() + 1}",
            f"R22B_TEST_GIT_UID={os.getuid()}",
            f"R22B_TEST_TOOL_GID={os.getgid()}",
            sys.executable, "-m", "pytest",
            f"{Path(__file__).resolve()}::test_r22b_production_cell_uses_pinned_helper_and_excludes_controller_secrets",
            "-q", "--basetemp", str(nested),
        ]
        cleanup_state = os.environ.get("R22C_CLEANUP_STATE")
        if cleanup_state:
            command.insert(3, f"R22C_CLEANUP_STATE={cleanup_state}")
        pid_report_state = os.environ.get("R22C_PID_REPORT_STATE")
        if pid_report_state:
            command.insert(3, f"R22C_PID_REPORT_STATE={pid_report_state}")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            ownership = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/sbin/chown", "-R", f"{os.getuid()}:{os.getgid()}", str(nested)],
                capture_output=True, text=True, check=False,
            )
            assert ownership.returncode == 0, ownership.stdout + ownership.stderr
            if cleanup_state:
                assert result.returncode != 0, result.stdout + result.stderr
                state = json.loads(Path(cleanup_state).read_text(encoding="utf-8"))
                _assert_pid_absent(int(state["git_pid"]))
                _assert_pid_absent(int(state["sentinel_pid"]))
                assert not Path(state["endpoint"]).exists()
                assert not Path(state["importer_destination"]).exists()
                assert not list(Path(state["importer_spool_parent"]).glob(".implbench-import-*"))
                assert not _named_process_census(
                    "implbench.harness.git_service_child",
                    "implbench.harness.importer_child",
                    str(state["sentinel_marker"]),
                )
            else:
                assert result.returncode == 0, result.stdout + result.stderr
        finally:
            if nested.exists():
                shutil.rmtree(nested)
            assert not nested.exists()
        return

    helper_path = tmp_path / "plane-helper"
    _write_plane_helper(helper_path)
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER", str(helper_path))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_MODE", "0750")
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper_path.read_bytes()).hexdigest())
    monkeypatch.setenv("IMPLBENCH_CELL_ROOT_BASE", str((tmp_path / "cells").resolve()))
    sentinel = "r22b-controller-secret-sentinel"
    monkeypatch.setenv("R22B_CONTROLLER_SECRET", sentinel)

    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    helper = _SystemPlaneProvisioner(run_id="oi-pi-bakeoff-r22b-production-entry")
    controller = build_production_controller(_manifest(evidence), provisioner=_StructuralProvisioner(helper), acl=_HermeticACL())
    scheduled = next(cell for cell in controller.cells.values() if cell.arm == "glm-pi")
    # Keep the real endpoint unique across interrupted repetitions; a stale
    # endpoint must remain a production failure rather than be unlinked here.
    attempt_id = "attempt-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()
    cell = controller._cell_for_cell(scheduled, attempt_id)

    # ``ensure_clone`` enters the real _ProductionCell lifecycle and previously failed
    # because controller secrets created the disposable cell before allocation.
    repo = cell.ensure_clone()
    assert repo.is_dir()
    task = controller.task_for_cell(scheduled)
    candidate_path = task.expected_artifacts[0]
    candidate = repo / candidate_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("production child receipt\n", encoding="utf-8")
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    binding = cell.open_attempt_git_service(attempt_id, allowed_paths=task.allowed_paths)
    assert binding["endpoint"].startswith("/")
    server = cell.git_rpc_server
    assert server is not None and server._process is not None
    assert server._process.pid != os.getpid()
    assert cell.identities is not None
    # This finalizer is deliberately registered immediately after readiness.  Any
    # later assertion, client failure, or injected red path closes the exact child
    # and unlinks its exact endpoint before pytest tears down the fixture.
    def close_exact_server() -> None:
        try:
            server.close()
        except GitRPCError as exc:
            # This structural fixture deliberately reuses the outer test UID, so
            # its root-tier UID census is expected to be non-empty.  Endpoint and
            # exact-PID cleanup already completed before this evidence-tier error.
            if "UID is not empty" not in str(exc):
                raise

    request.addfinalizer(close_exact_server)

    cleanup_state = os.environ.get("R22C_CLEANUP_STATE")
    if cleanup_state:
        marker = "r22c-cleanup-sentinel-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
        try:
            # Prove the independently executed parent detector goes red for a
            # surviving process before the deliberate assertion failure.
            with pytest.raises(AssertionError, match="survived cleanup"):
                _assert_pid_absent(sleeper.pid)
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)
        Path(cleanup_state).write_text(json.dumps({
            "git_pid": server._process.pid,
            "sentinel_pid": sleeper.pid,
            "sentinel_marker": marker,
            "endpoint": binding["endpoint"],
            "importer_destination": str(cell.paths.runtime / "importer" / "runtime" / "bundle"),
            "importer_spool_parent": str(cell.paths.runtime / "importer" / "runtime"),
        }, sort_keys=True), encoding="utf-8")
        raise AssertionError("r22c deliberate failure after Git readiness")

    # A separately credentialed tool peer drives the production client, child Git
    # server, controller receipt loop, and post-fsync acknowledgement end to end.
    accepted_client = (
        "import json, sys; from implbench.harness.git_service import RemoteGitService; "
        "binding=json.loads(sys.argv[1]); service=RemoteGitService(endpoint=binding['endpoint'], "
        "capability=binding['capability'], tool_gid=int(sys.argv[2])); "
        "status=service.handle({'op':'status'}); "
        "staged=service.handle({'op':'add','paths':[sys.argv[3]]}); "
        "receipt=service.handle({'op':'commit','message':'production receipt'}); "
        "print(json.dumps({'status':status,'staged':staged,'receipt':receipt}, sort_keys=True))"
    )

    def as_tool_peer() -> None:
        os.setgid(cell.identities.tool_gid)
        os.setuid(cell.identities.tool)

    accepted = subprocess.run(
        [sys.executable, "-c", accepted_client, json.dumps(binding), str(cell.identities.tool_gid), candidate_path],
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "bench")},
        capture_output=True, text=True, check=False, preexec_fn=as_tool_peer,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    receipt = json.loads(accepted.stdout)["receipt"]
    assert receipt["changed_paths"] == [candidate_path]
    assert cell.receipt_chain is not None and cell.receipt_chain.verify() == 1
    assert cell.receipt_chain._rows()[0]["payload"]["controller_sequence"] == 1

    # The current UID owns the service process in this structural fixture but is
    # not one of the declared control/tool peers; it must fail closed at the real
    # socket rather than be admitted because it happens to own the endpoint.
    client = (
        "import json, sys; from implbench.harness.git_service import RemoteGitService; "
        "binding=json.loads(sys.argv[1]); service=RemoteGitService(endpoint=binding['endpoint'], "
        "capability=binding['capability'], tool_gid=int(sys.argv[2])); "
        "service.handle({'op':'status'})"
    )
    client_result = subprocess.run(
        [sys.executable, "-c", client, json.dumps(binding), str(cell.identities.tool_gid)],
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT / "bench")},
        capture_output=True, text=True, check=False,
    )
    assert client_result.returncode != 0

    source = tmp_path / "hostile-git"
    body = b"hello\\n"
    raw = b"blob " + str(len(body)).encode() + b"\\0" + body
    oid = hashlib.sha1(raw).hexdigest()
    loose = source / ".git" / "objects" / oid[:2] / oid[2:]
    loose.parent.mkdir(parents=True)
    loose.write_bytes(zlib.compress(raw))
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(ProductionRuntimeUnavailable, match="importer child boundary failed"):
            cell.import_descriptor_child(source_fd, tmp_path / "ignored", None)
    finally:
        os.close(source_fd)
    assert not (cell.paths.runtime / "importer" / "runtime" / "bundle").exists()

    launch_rows = [row for row in _trace(cell.paths.run_root) if row["request"]["action"] == "launch-child"]
    assert len(launch_rows) == 2
    assert all(row["pid"] != os.getpid() for row in launch_rows)
    assert len({row["pid"] for row in launch_rows}) == len(launch_rows)
    pid_report_state = os.environ.get("R22C_PID_REPORT_STATE")
    if pid_report_state:
        Path(pid_report_state).write_text(json.dumps({
            "child_pids": sorted(int(row["pid"]) for row in launch_rows),
            "endpoint": binding["endpoint"],
            "importer_destination": str(cell.paths.runtime / "importer" / "runtime" / "bundle"),
            "importer_spool_parent": str(cell.paths.runtime / "importer" / "runtime"),
        }, sort_keys=True), encoding="utf-8")
    for row in launch_rows:
        inherited = row["request"]["launch"]["inherited_fds"]
        assert row["open_fds_after"] == [0, 1, 2, *inherited]
    launches = [row["request"] for row in launch_rows]
    assert {launch["launch"]["plane"] for launch in launches} == {"git-service", "importer"}
    expected_launch_fields = {
        "plane", "argv", "env", "cwd", "profile", "profile_digest", "template_digest", "uid", "gid",
        "inherited_fds", "fresh_context", "resume", "fork_from", "warm_process", "shell",
    }
    expected_env = {
        "git-service": {
            "HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "GIT_DIR",
            "GIT_WORK_TREE", "GIT_CONFIG_NOSYSTEM", "GIT_SERVICE_SOCKET",
        },
        "importer": {"HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"},
    }
    forbidden_child_surfaces = (
        str(cell.receipt_chain.log.path), str(cell.paths.config_root), str(controller.evidence_root),
        str(cell.paths.control_home), str(cell.paths.run_root / "controller-secrets"),
    )
    for launch in launches:
        child = launch["launch"]
        assert set(launch) == {"version", "action", "run_id", "cell_id", "attempt_id", "root", "nonce", "launch"}
        assert set(child) == expected_launch_fields
        assert launch["run_id"] == controller.run_id
        assert launch["cell_id"] == scheduled.cell_id
        assert launch["attempt_id"] == attempt_id
        assert launch["root"] == str(cell.paths.cell_root)
        assert child["uid"] == cell.identities.git
        assert child["gid"] == cell.identities.tool_gid
        assert child["cwd"].startswith(str(cell.paths.cell_root))
        assert child["argv"][:3] == [sys.executable, "-u", "-m"]
        assert child["argv"][3] in {"implbench.harness.git_service_child", "implbench.harness.importer_child"}
        assert child["profile_digest"] == hashlib.sha256(child["profile"].encode()).hexdigest()
        assert child["template_digest"]
        assert child["env"] == {key: child["env"][key] for key in sorted(child["env"])}
        assert set(child["env"]) == expected_env[child["plane"]]
        _assert_secret_absent(child, sentinel)
        child_argv_env = " ".join((*child["argv"], json.dumps(child["env"], sort_keys=True)))
        assert not any(surface in child_argv_env for surface in forbidden_child_surfaces)

    git_launch = next(launch["launch"] for launch in launches if launch["launch"]["plane"] == "git-service")
    secret_path = cell.paths.run_root / "controller-secrets"
    assert str(secret_path) not in " ".join(git_launch["argv"])
    assert str(secret_path) not in json.dumps(git_launch["env"], sort_keys=True)
    assert f'(deny file-read* (subpath "{secret_path}"))' in git_launch["profile"]
    assert f'(deny file-write* (subpath "{secret_path}"))' in git_launch["profile"]
    assert not (cell.paths.runtime / "controller-secrets.json").exists()

    # The detector is itself adversarially checked: a synthetic leak is red.
    with pytest.raises(AssertionError):
        _assert_secret_absent({"env": {"R22B_CONTROLLER_SECRET": sentinel}}, sentinel)

    # The host UID is intentionally shared by this structural helper fixture.  The
    # production census therefore goes red after reaping, demonstrating that it
    # cannot be replaced by the helper's claim; Task 14 owns the root-distinct tier.
    with pytest.raises(GitRPCError, match="UID is not empty"):
        cell.close_attempt_git_service()
    assert server._process.poll() is not None
    assert not Path(binding["endpoint"]).exists()


def test_r22c_parent_observes_production_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """A separate parent process verifies the red production probe left no residue."""

    state = tmp_path / "r22c-cleanup-state.json"
    monkeypatch.setenv("R22C_CLEANUP_STATE", str(state))
    test_r22b_production_cell_uses_pinned_helper_and_excludes_controller_secrets(
        tmp_path, monkeypatch, request,
    )
    value = json.loads(state.read_text(encoding="utf-8"))
    _assert_pid_absent(int(value["git_pid"]))
    _assert_pid_absent(int(value["sentinel_pid"]))
    assert not Path(value["endpoint"]).exists()
    assert not Path(value["importer_destination"]).exists()
    assert not list(Path(value["importer_spool_parent"]).glob(".implbench-import-*"))
    assert not _named_process_census(
        "implbench.harness.git_service_child",
        "implbench.harness.importer_child",
        str(value["sentinel_marker"]),
    )


def test_r22c_parent_observes_all_production_child_pids_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """A separate parent proves both normal production child PIDs became absent."""

    state = tmp_path / "r22c-child-pids.json"
    monkeypatch.setenv("R22C_PID_REPORT_STATE", str(state))
    test_r22b_production_cell_uses_pinned_helper_and_excludes_controller_secrets(
        tmp_path, monkeypatch, request,
    )
    value = json.loads(state.read_text(encoding="utf-8"))
    assert len(value["child_pids"]) == 2
    for pid in value["child_pids"]:
        _assert_pid_absent(int(pid))
    assert not Path(value["endpoint"]).exists()
    assert not Path(value["importer_destination"]).exists()
    assert not list(Path(value["importer_spool_parent"]).glob(".implbench-import-*"))
    assert not _named_process_census(
        "implbench.harness.git_service_child",
        "implbench.harness.importer_child",
    )


def test_r22c_production_composition_rejects_forged_git_identity_and_missing_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper/child claim is insufficient without matching independent process evidence."""

    if os.geteuid() != 0:
        nested = tmp_path / "root-pytest"
        command = [
            "/usr/bin/sudo", "-n", "/usr/bin/env",
            f"PYTHONPATH={ROOT / 'bench'}",
            f"GIT_CONFIG_COUNT=1", f"GIT_CONFIG_KEY_0=safe.directory", f"GIT_CONFIG_VALUE_0={ROOT}",
            f"R22B_TEST_CONTROL_UID={os.getuid() + 2}",
            f"R22B_TEST_TOOL_UID={os.getuid() + 1}",
            f"R22B_TEST_GIT_UID={os.getuid()}",
            f"R22B_TEST_TOOL_GID={os.getgid()}",
            sys.executable, "-m", "pytest",
            f"{Path(__file__).resolve()}::test_r22c_production_composition_rejects_forged_git_identity_and_missing_census",
            "-q", "--basetemp", str(nested),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            ownership = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/sbin/chown", "-R", f"{os.getuid()}:{os.getgid()}", str(nested)],
                capture_output=True, text=True, check=False,
            )
            assert ownership.returncode == 0, ownership.stdout + ownership.stderr
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            if nested.exists():
                shutil.rmtree(nested)
            assert not nested.exists()
        return

    helper_path = tmp_path / "plane-helper"
    _write_plane_helper(helper_path)
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER", str(helper_path))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_MODE", "0750")
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper_path.read_bytes()).hexdigest())
    monkeypatch.setenv("IMPLBENCH_CELL_ROOT_BASE", str((tmp_path / "cells").resolve()))

    for fault in ("pid", "uid", "census"):
        with monkeypatch.context() as fault_patch:
            fault_patch.setenv("IMPLBENCH_CELL_ROOT_BASE", str((tmp_path / f"cells-{fault}").resolve()))
            if fault == "census":
                fault_patch.setattr("implbench.harness.runtime._SystemProcessTable.census_uid", lambda _self, _uid: set())
            evidence = tmp_path / f"evidence-{fault}"; evidence.mkdir(mode=0o700)
            helper = _SystemPlaneProvisioner(run_id="oi-pi-bakeoff-r22c-production-entry")
            provisioner = _FaultingStructuralProvisioner(helper, fault)
            controller = build_production_controller(_manifest(evidence), provisioner=provisioner, acl=_HermeticACL())
            scheduled = next(cell for cell in controller.cells.values() if cell.arm == "glm-pi")
            attempt_id = "attempt-" + hashlib.sha256(f"{tmp_path}:{fault}".encode()).hexdigest()
            cell = controller._cell_for_cell(scheduled, attempt_id)
            repo = cell.ensure_clone()
            task = controller.task_for_cell(scheduled)
            candidate = repo / task.expected_artifacts[0]
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("forged production child evidence\n", encoding="utf-8")
            endpoint = Path(tempfile.gettempdir()) / (
                "implbench-g-" + hashlib.sha256(
                    f"{controller.run_id}\0{scheduled.cell_id}\0{attempt_id}".encode("utf-8")
                ).hexdigest()[:24] + ".sock"
            )
            with pytest.raises(ProductionRuntimeUnavailable, match="attempt Git service could not start"):
                cell.open_attempt_git_service(attempt_id, allowed_paths=task.allowed_paths)
            assert len(provisioner.child_pids) == 1
            _assert_pid_absent(provisioner.child_pids[0])
            assert not endpoint.exists()


def test_r22b_helper_refuses_forged_profile_root_or_secret_before_any_child_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper_path = tmp_path / "plane-helper"
    _write_plane_helper(helper_path)
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_MODE", "0750")
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper_path.read_bytes()).hexdigest())
    helper = _SystemPlaneProvisioner(helper=str(helper_path), run_id="oi-pi-bakeoff-r22b-production-entry")
    root = tmp_path / "root"; root.mkdir(mode=0o700)
    paths = SandboxPaths(root, root / "work", root / "git", root / "evidence", root / "base", root / "sibling", root / "credentials", root / "key", root / "home", root / "runtime")
    for path in (paths.worktree, paths.git_dir, paths.evidence_root, paths.base_checkout, paths.sibling_worktree, paths.credential_root, paths.key_root, paths.home, paths.runtime):
        path.mkdir(mode=0o700)
    spec = build_launch_spec("importer", paths, uid=os.getuid(), gid=os.getgid(), argv=(sys.executable, "-c", "pass"))
    forged = type(spec)(**{**spec.__dict__, "profile_digest": "0" * 64})
    with pytest.raises(LaunchError, match="profile digest"):
        helper.spawn_child(forged, cell_id="cell-" + "b" * 64, attempt_id="attempt-" + "b" * 64, root=root, pass_fds=())
    leaky = type(spec)(**{**spec.__dict__, "env": {**spec.env, "RECEIPT_KEY": "r22b-secret-sentinel"}})
    with pytest.raises(LaunchError, match="environment is outside"):
        helper.spawn_child(leaky, cell_id="cell-" + "b" * 64, attempt_id="attempt-" + "b" * 64, root=root, pass_fds=())
    other = tmp_path / "other"; other.mkdir(mode=0o700)
    with pytest.raises(ProductionRuntimeUnavailable, match="root does not bind"):
        helper.spawn_child(spec, cell_id="cell-" + "b" * 64, attempt_id="attempt-" + "b" * 64, root=other, pass_fds=())
    with pytest.raises(ProductionRuntimeUnavailable, match="request is not closed"):
        helper._call("reserve", cell_id="cell-" + "b" * 64, attempt_id="attempt-" + "b" * 64, root=str(root), extra="forged")

    extra_response = tmp_path / "extra-response-helper"
    extra_response.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "response = {'version': 'implbench-plane-v1', 'ok': True, 'control_uid': 900001, 'tool_uid': 900002, 'git_uid': 900003, 'tool_gid': 900004, 'processes': [], 'extra': 'forged'}\n"
        "response.update({key: request[key] for key in ('action', 'run_id', 'cell_id', 'attempt_id', 'root', 'nonce')})\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
    )
    extra_response.chmod(0o750)
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(extra_response.read_bytes()).hexdigest())
    with pytest.raises(ProductionRuntimeUnavailable, match="did not acknowledge success"):
        _SystemPlaneProvisioner(helper=str(extra_response), run_id="oi-pi-bakeoff-r22b-production-entry").reserve_identities(
            "cell-" + "b" * 64, attempt_id="attempt-" + "b" * 64, root=root,
        )
