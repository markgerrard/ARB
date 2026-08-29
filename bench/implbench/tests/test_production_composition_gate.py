"""Authoritative production-composition gate (never replaces a named plane)."""

from __future__ import annotations

import os
import base64
import hashlib
import inspect
import json
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_redis_bridge.engines.openinterpreter import CellToolPlaneBroker
from agent_redis_bridge.scored_plane import MAX_MODEL_RESULT_BYTES, _control_inputs
from implbench.harness.readiness import GATE_IDS, run_production_preflight
from implbench.harness.runtime import ProductionRuntimeUnavailable, build_production_controller, build_production_runtime
from implbench.harness.schedule import expand_schedule
from implbench.harness.tasks import load_task

from test_production_scorer_composition_gate import _scorer_binary
from test_r22b_production_entry import _manifest


EXPECTED_MODEL_RESULT_BYTES = 4096


def _bind_control_secrets(monkeypatch: pytest.MonkeyPatch, root: Path, manifest: dict) -> None:
    for arm in manifest["arms"]:
        name = "IMPLBENCH_CONTROL_SECRET_" + arm["arm"].upper().replace("-", "_")
        path = root / f"{arm['arm']}.secret.json"
        payload = {
            "schema": "implbench-control-secret-v1",
            "arm": arm["arm"],
            "provider": arm["provider"],
            "secret_names": ["DUMMY_API_KEY"],
            "environment": {"DUMMY_API_KEY": "structural-only-secret"},
            "files": {},
        }
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(name, str(path.resolve()))


def _interpreter_binary(path: Path) -> None:
    """Small app-server that makes one real tool call through production control."""

    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('interpreter 0.0.21'); raise SystemExit(0)\n"
        "provider = model = harness = None\n"
        "def send(value):\n"
        "    print(json.dumps(value, separators=(',', ':')), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    method = request.get('method')\n"
        "    params = request.get('params', {})\n"
        "    if method == 'initialize':\n"
        "        provider, model, harness = params['provider'], params['model'], params['harness']\n"
        "        ack = {'provider': provider, 'model': model, 'harness': harness, 'reasoning': "
        "{'requested': 'medium', 'effective': 'medium', 'verified_via': 'runtime'}, 'source': 'runtime'}\n"
        "        send({'id': request['id'], 'result': {'controlAck': ack}})\n"
        "    elif method == 'thread/start':\n"
        "        send({'id': request['id'], 'result': {'thread': {'id': 'thread-1'}}})\n"
        "    elif method == 'turn/start':\n"
        "        send({'id': request['id'], 'result': {'turn': {'id': 'turn-1'}}})\n"
        "        send({'id': 9001, 'method': 'file/read', 'params': {'path': 'README.md'}})\n"
        "        tool_response = json.loads(next(sys.stdin))\n"
        "        if tool_response.get('id') != 9001 or not isinstance(tool_response.get('result'), dict):\n"
        "            raise SystemExit(9)\n"
        "        ack = {'provider': provider, 'model': model, 'harness': harness, 'reasoning': "
        "{'requested': 'medium', 'effective': 'medium', 'verified_via': 'runtime'}, 'source': 'runtime'}\n"
        "        send({'method': 'control/ack', 'params': ack})\n"
        "        send({'method': 'turn/completed', 'params': {'turn': "
        "{'id': 'turn-1', 'status': 'completed', 'text': 'CONTROL_DISPATCH_OK'}}})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _descriptor(value: object) -> int:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, json.dumps(value, sort_keys=True).encode("utf-8"))
    finally:
        os.close(write_fd)
    return read_fd


@contextmanager
def _real_acl_server():
    """Run an actual Redis ACL server, never a dictionary/fake backend."""

    binary = shutil.which("redis-server")
    assert binary is not None, "the production-composition gate requires redis-server"
    root = Path(tempfile.mkdtemp(prefix="implbench-acl-"))
    endpoint = root / "redis.sock"
    process = subprocess.Popen(
        [binary, "--save", "", "--appendonly", "no", "--port", "0",
         "--unixsocket", str(endpoint), "--unixsocketperm", "700"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        import redis

        url = f"unix://{endpoint}?db=0"
        client = redis.Redis.from_url(url, decode_responses=True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(0.02)
        else:
            raise AssertionError("real ACL server did not become ready")
        yield url, client
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        shutil.rmtree(root, ignore_errors=True)


def _pid_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _candidate_source(root: Path) -> tuple[Path, str]:
    """Commit the exact dirty candidate into an isolated source repository."""

    source = Path(__file__).parents[3]
    candidate = root / "candidate-source"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(candidate)], check=True)
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=source)
    if patch:
        subprocess.run(
            ["git", "-C", str(candidate), "apply", "--binary", "--whitespace=nowarn", "-"],
            input=patch, check=True,
        )
        subprocess.run(["git", "-C", str(candidate), "add", "--update"], check=True)
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": "implbench-test", "GIT_AUTHOR_EMAIL": "implbench-test@localhost",
            "GIT_COMMITTER_NAME": "implbench-test", "GIT_COMMITTER_EMAIL": "implbench-test@localhost",
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
        })
        subprocess.run(
            ["git", "-C", str(candidate), "commit", "--quiet", "-m", "candidate snapshot"],
            check=True, env=env,
        )
    commit = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
    return candidate, commit


def _predict_boundary_commit(root: Path, source: Path, parent: str, artifact: str) -> str:
    """Pre-pin the deterministic commit the production Git service will create."""

    repo = root / "predicted-commit"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", "--detach", parent], check=True)
    target = repo / artifact
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target.write_text("boundary = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--force", artifact], check=True)
    tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "implbench", "GIT_AUTHOR_EMAIL": "implbench@localhost",
        "GIT_COMMITTER_NAME": "implbench", "GIT_COMMITTER_EMAIL": "implbench@localhost",
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
    })
    return subprocess.check_output(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", parent],
        input="production boundary proof\n", text=True, env=env,
    ).strip()


def _tree_entries(root: Path) -> dict[str, tuple[str, int, str]]:
    entries: dict[str, tuple[str, int, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries[str(relative)] = ("directory", stat.S_IMODE(info.st_mode), "")
        elif stat.S_ISLNK(info.st_mode):
            entries[str(relative)] = ("symlink", stat.S_IMODE(info.st_mode), os.readlink(path))
        else:
            entries[str(relative)] = (
                "file", stat.S_IMODE(info.st_mode), hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return entries


def _assert_current_worktree_modules(root: Path) -> None:
    for value in (
        CellToolPlaneBroker, _control_inputs, build_production_controller,
        build_production_runtime, run_production_preflight, expand_schedule, load_task,
    ):
        module = inspect.getmodule(value)
        assert module is not None and module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(root)


@pytest.mark.parametrize("fail_dispatch_commit", [False, True], ids=("normal", "journal-failure"))
def test_production_composition_executes_real_plane_lifecycle_and_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_dispatch_commit: bool,
) -> None:
    """Enter the unmodified production factory and execute its real plane/ACL lifecycle."""

    source_root = Path(__file__).parents[3].resolve()
    _assert_current_worktree_modules(source_root)
    assert MAX_MODEL_RESULT_BYTES == EXPECTED_MODEL_RESULT_BYTES

    monkeypatch.setenv("IMPLBENCH_CELL_ROOT_BASE", str((tmp_path / "cells").resolve()))
    monkeypatch.setenv("IMPLBENCH_BUS_ENDPOINT", "127.0.0.1:6379")
    for provider in ("ZAI", "ZAI_CODING_PLAN", "KIMI_CODING", "KIMI_FOR_CODING"):
        monkeypatch.setenv(f"IMPLBENCH_PROVIDER_ENDPOINT_{provider}", "api.example.test:443")
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    manifest = _manifest(evidence)
    candidate_source, candidate_commit = _candidate_source(tmp_path)
    manifest["source"] = {"realpath": str(candidate_source), "commit": candidate_commit}
    manifest["base_sha"] = candidate_commit
    for task_row in manifest["tasks"]:
        task_row["fixture_sha"] = candidate_commit
    openinterpreter_arms = {
        row["arm"] for row in manifest["arms"] if row["engine"] == "openinterpreter"
    }
    scheduled_first = next(
        row for row in expand_schedule(manifest["seed"], manifest["tasks"])
        if row.arm in openinterpreter_arms
    )
    scheduled_task = load_task(
        candidate_source / "bench" / "implbench" / "fixtures" /
        scheduled_first.task_id / "task.yaml"
    )
    predicted_commit = _predict_boundary_commit(
        tmp_path, candidate_source, manifest["base_sha"], scheduled_task.expected_artifacts[0]
    )
    scorer_binary = tmp_path / "scorer"
    _scorer_binary(scorer_binary, commit_oid=predicted_commit)
    manifest["pins"] = {
        "scorer": {
            "version": "scorer-structural-v1",
            "digest": "sha256:" + hashlib.sha256(scorer_binary.read_bytes()).hexdigest(),
        },
        "public_suite": {"digest": "sha256:" + "b" * 64, "digest_version": "public-v1"},
    }
    manifest["budgets"] = {"scorer_max_output_bytes": 4096}
    monkeypatch.setenv("IMPLBENCH_SCORER_BIN", str(scorer_binary))
    interpreter_binary = tmp_path / "interpreter"
    _interpreter_binary(interpreter_binary)
    monkeypatch.setenv("IMPLBENCH_INTERPRETER_BIN", str(interpreter_binary))
    monkeypatch.setenv("IMPLBENCH_INTERPRETER_SHA256", hashlib.sha256(interpreter_binary.read_bytes()).hexdigest())
    monkeypatch.setenv("IMPLBENCH_PUBLIC_SUITE_OID", "c" * 40)
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "structural-hidden-key")
    for offset, name in enumerate((
        "IMPLBENCH_SCORER_KEYED_RUNNER_UID", "IMPLBENCH_SCORER_BROKER_UID",
        "IMPLBENCH_SCORER_SUBMITTED_PROGRAM_UID", "IMPLBENCH_SCORER_COORDINATOR_UID",
        "IMPLBENCH_SCORER_SUITE_RUNNER_BROKER_UID", "IMPLBENCH_SCORER_SUBMITTED_CODE_UID",
    ), start=42001):
        monkeypatch.setenv(name, str(offset))
    _bind_control_secrets(monkeypatch, tmp_path, manifest)
    with _real_acl_server() as (url, acl_client):
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", url)
        controller = build_production_controller(manifest)
        runtime = build_production_runtime(manifest, controller=controller)
        assert runtime.scored_dispatch.dispatch_fn.__name__ == "run_task"
        assert controller.provisioner.helper.name == "helper"
        assert controller.acl.backend.__class__.__name__ == "ValkeyACLBackend"
        readiness = run_production_preflight(manifest, runtime=runtime)
        assert readiness.status == "UNKNOWN"
        assert tuple(record.gate_id for record in readiness.gates) == GATE_IDS

        scheduled = controller.cells[scheduled_first.cell_id]
        attempt_id = "attempt-" + "a" * 64
        cell = controller.cell_for_cell(scheduled, attempt_id)
        assert cell.identities is not None and cell.runtime.acl_identity is not None
        acl_user = cell.runtime.acl_identity.user
        if fail_dispatch_commit:
            cell.ensure_clone()
            task = controller.task_for_cell(scheduled)
            binding = cell.open_attempt_git_service(attempt_id, allowed_paths=task.allowed_paths)
            append = cell.runtime.journal.append

            def fail_final_dispatch(operation: str, state: str, **fields: object) -> None:
                if (operation, state) == ("dispatch", "committed"):
                    raise OSError("injected dispatch journal failure")
                append(operation, state, **fields)

            monkeypatch.setattr(cell.runtime.journal, "append", fail_final_dispatch)
            try:
                with pytest.raises(OSError, match="injected dispatch journal failure"):
                    cell.start_attempt_planes(binding)
                assert cell.tool_process is not None and cell.control_process is not None
                assert _pid_absent(cell.tool_process.pid)
                assert _pid_absent(cell.control_process.pid)
                assert cell.tool_endpoint is not None and not cell.tool_endpoint.exists()
                assert cell.control_endpoint is not None and not cell.control_endpoint.exists()
            finally:
                cell.close_attempt_git_service()
                if cell.runtime.state != "DESTROYED":
                    cell.runtime.close()
            assert cell.runtime.state == "DESTROYED"
            assert not cell.paths.cell_root.exists()
            return
        processes: list[dict[str, object]] = []
        commit: dict[str, object] = {}
        try:
            cell.ensure_clone()
            task = controller.task_for_cell(scheduled)
            binding = cell.open_attempt_git_service(attempt_id, allowed_paths=task.allowed_paths)
            evidence = cell.start_attempt_planes(binding)
            processes = [
                {"role": role, "pid": row["pid"], "launch": row["launch"], "probe": row["probe"]}
                for role, row in evidence["processes"].items()
            ]
            assert {row["role"] for row in processes} == {"control", "tool"}
            assert len({row["pid"] for row in processes}) == 2
            assert all(row["pid"] != os.getpid() for row in processes)
            assert {row["launch"].uid for row in processes} == {cell.identities.control, cell.identities.tool}
            assert all(row["launch"].argv[0] == os.sys.executable for row in processes)
            assert all(row["launch"].profile_digest and row["launch"].template_digest for row in processes)
            assert all(row["probe"]["pid"] == row["pid"] for row in processes)
            assert next(row for row in processes if row["role"] == "control")["probe"]["tool_crossed"] is True
            control = next(row for row in processes if row["role"] == "control")
            assert control["probe"]["secret_descriptor_consumed"] is True
            assert control["probe"]["secret_names"] == ["DUMMY_API_KEY"]
            assert len(control["launch"].inherited_fds) == 4
            assert all(flag in control["launch"].argv for flag in ("--config-fd", "--secret-fd", "--listener-fd"))
            assert "structural-only-secret" not in repr(control["launch"])
            assert "DUMMY_API_KEY" not in control["launch"].env
            assert stat.S_IMODE(cell.tool_endpoint.stat().st_mode) == 0o660
            assert stat.S_IMODE(cell.control_endpoint.stat().st_mode) == 0o600
            assert cell.control_endpoint.stat().st_uid == os.getuid()
            assert {row["launch"].gid for row in processes} == {cell.tool_gid}
            expected_socket_gid = os.getgid() if controller.provisioner.structural_only else cell.tool_gid
            assert cell.tool_endpoint.stat().st_gid == expected_socket_gid
            dispatch_result = cell.dispatch_through_control(
                task, controller.engine_for_cell(scheduled), timeout=10,
            )
            assert dispatch_result["status"] == "ok"
            assert dispatch_result["text"] == "CONTROL_DISPATCH_OK"
            broker = CellToolPlaneBroker.from_endpoint(
                str(cell.tool_endpoint), socket_gid=cell.tool_gid,
                identity={"cell_id": scheduled.cell_id, "attempt_id": attempt_id},
            )
            artifact = task.expected_artifacts[0]
            assert broker.handle_tool_request({"op": "write", "path": artifact, "content": "x" * 20000})["bytes"] == 20000
            large_read = broker.handle_tool_request({"op": "read", "path": artifact})
            assert large_read["truncated"] is True and large_read["original_bytes"] == 20000
            assert len(json.dumps(large_read, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= EXPECTED_MODEL_RESULT_BYTES
            large_bash = broker.handle_tool_request({
                "op": "bash", "command": "python3 -c 'import sys; sys.stdout.write(\"x\" * 20000); sys.stderr.write(\"y\" * 20000)'",
            })
            assert large_bash["truncated"] is True and large_bash["original_bytes"] == 40000
            assert len(json.dumps(large_bash, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= EXPECTED_MODEL_RESULT_BYTES
            assert broker.handle_tool_request({"op": "write", "path": artifact, "content": "boundary = 1\n"})["bytes"] == 13
            large_git = broker.handle_tool_request({"op": "add", "paths": [artifact] * 1024})
            assert large_git["truncated"] is True
            assert large_git["paths_count"] == 1024 and large_git["object_oids_count"] == 1024
            assert len(json.dumps(large_git, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) <= EXPECTED_MODEL_RESULT_BYTES
            assert broker.handle_tool_request({"op": "read", "path": artifact})["content"] == "boundary = 1\n"
            assert broker.handle_tool_request({
                "op": "edit", "path": artifact, "old_text": "boundary = 1", "new_text": "boundary = 2",
            })["replacements"] == 1
            assert broker.handle_tool_request({"op": "bash", "command": "printf production-tool-crossed"})["stdout"] == "production-tool-crossed"
            broker.handle_tool_request({"op": "add", "paths": [artifact]})
            commit = broker.handle_tool_request({"op": "commit", "message": "production boundary proof"})
            assert commit["changed_paths"] == [artifact]
            assert commit["commit_oid"] == predicted_commit
            reconstructed = tmp_path / "reconstructed-tree"
            reconstructed.mkdir()
            archive = subprocess.check_output([
                "git", "-C", str(cell.repo), "archive", "--format=tar", str(commit["commit_oid"]),
            ])
            subprocess.run(["tar", "-xf", "-", "-C", str(reconstructed)], input=archive, check=True)
            assert _tree_entries(cell.repo) == _tree_entries(reconstructed)
            projection = broker.completion_projection()
            assert projection["receipt_oids"] == []
            assert projection["infrastructure_failure"] == "awaiting-controller-close"
            # Retained-child RED observation uses the actual exec PIDs at the
            # structural tier; Task 14 repeats the independent requested-UID census.
            assert all(not _pid_absent(int(row["pid"])) for row in processes)
            assert acl_client.execute_command("ACL", "GETUSER", cell.runtime.acl_identity.user) is not None
            assert controller.acl.pre_empty(cell.runtime.acl_identity)
            assert controller.acl.cross_prefix_denied(cell.runtime.acl_identity)
        finally:
            cell.close_attempt_git_service()
            try:
                cell.stop_tools()
                cell.drain_rpc()
                cell.kill_planes()
                cell.close_acl()
                cell.final_status()
                cell.kill_git()
                cell.census_snapshot()
                if commit:
                    closed_projection = cell.completion_projection()
                    assert closed_projection["receipt_oids"] == [commit["commit_oid"]]
                    scored_close = controller.scored_runtime_factory(
                        cell_id=scheduled.cell_id,
                        attempt_id=attempt_id,
                        completion=closed_projection,
                    )
                    try:
                        score = scored_close.import_and_score()
                        assert score["g1"] == "PASS" and score["g4"] == "PASS"
                        rows = cell.receipt_chain._rows()
                        record_types = [row["record_type"] for row in rows]
                        assert record_types == [
                            "git-receipt", "pre-scorer-attestation", "g4-receipt",
                            "post-g4-attestation",
                        ]
                        launch_rows = controller.scored_scorer_factory.last_launch_evidence
                        role_pids = [
                            pid for row in launch_rows for pid in row.get("role_pids", {}).values()
                        ]
                        assert len(role_pids) == 6 and len(set(role_pids)) == 6
                        assert all(_pid_absent(pid) for pid in role_pids)
                    finally:
                        scored_close.destroy()
            finally:
                if cell.runtime.state != "DESTROYED":
                    cell.runtime.close()

        assert cell.runtime.state == "DESTROYED"
        assert not cell.paths.cell_root.exists()
        assert all(_pid_absent(int(row["pid"])) for row in processes)
        assert acl_client.execute_command("ACL", "GETUSER", acl_user) is None
        assert not list(cell.paths.cell_root.parent.glob(".implbench-plane-*.json"))


@pytest.mark.parametrize("name", ["PATH", "PYTHONPATH", "DYLD_INSERT_LIBRARIES"])
def test_control_secret_environment_rejects_runtime_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = {
        "schema": "implbench-control-config-v1", "run_id": "run", "cell_id": "cell",
        "attempt_id": "attempt", "arm": "glm-zcode", "engine": "openinterpreter",
        "provider": "zai-coding-plan", "model": "glm-5.2", "harness": "zcode",
        "workdir": str(tmp_path), "interpreter_bin": "/usr/bin/true",
        "interpreter_sha256": "a" * 64,
    }
    config["config_digest"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    secret = {
        "schema": "implbench-control-secret-v1", "arm": config["arm"],
        "provider": config["provider"], "secret_names": [name],
        "environment": {name: "attacker-controlled"}, "files": {},
    }
    with pytest.raises(ValueError, match="allowlist"):
        _control_inputs(_descriptor(config), _descriptor(secret))


@pytest.mark.parametrize("relative", [
    "Library/Python/3.9/lib/python/site-packages/usercustomize.py",
    ".pi/agent/models.json",
    ".zshrc",
])
def test_control_secret_files_cannot_become_runtime_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = {
        "schema": "implbench-control-config-v1", "run_id": "run", "cell_id": "cell",
        "attempt_id": "attempt", "arm": "glm-pi", "engine": "pi-sdk",
        "provider": "zai", "model": "glm-5.2", "harness": "pi",
        "workdir": str(tmp_path), "interpreter_bin": "/usr/bin/true",
        "interpreter_sha256": "a" * 64,
    }
    config["config_digest"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    secret = {
        "schema": "implbench-control-secret-v1", "arm": config["arm"],
        "provider": config["provider"],
        "secret_names": sorted(["DUMMY_API_KEY", f"file:{relative}"]),
        "environment": {"DUMMY_API_KEY": "secret"},
        "files": {relative: base64.b64encode(b"runtime authority").decode("ascii")},
    }
    with pytest.raises(ValueError, match="secret files are forbidden"):
        _control_inputs(_descriptor(config), _descriptor(secret))
    assert not (tmp_path / relative).exists()


def test_production_composition_refuses_missing_managed_acl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    monkeypatch.setenv("IMPLBENCH_CELL_ROOT_BASE", str((tmp_path / "cells").resolve()))
    with pytest.raises(ProductionRuntimeUnavailable, match="ARB_MEMORY_REDIS_URL"):
        build_production_controller(_manifest((tmp_path / "evidence").resolve()))
