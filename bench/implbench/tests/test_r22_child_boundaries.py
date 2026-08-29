from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import tracemalloc
import zlib
from pathlib import Path

import pytest

from implbench.harness.git_service import ChildAttemptGitServiceServer, GitRPCError, GitService, RemoteGitService
from implbench.harness.importer import ImportLimits, ImporterError, _loose_object_oid, import_from_descriptor_child
from implbench.harness.receipts import ReceiptChain, ReceiptError
from implbench.harness.sandbox import SandboxPaths, build_launch_spec


def _paths(root: Path, *, worktree: Path, git_dir: Path, role: str, service_socket: Path | None = None) -> SandboxPaths:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("evidence", "base", "sibling", "credentials", "key", "home", "runtime", "work"):
        (root / name).mkdir(exist_ok=True)
    return SandboxPaths(root, worktree if role == "git-service" else root / "work", git_dir, root / "evidence",
                        root / "base", root / "sibling", root / "credentials", root / "key",
                        root / "home", root / "runtime", service_socket)


def test_r22_git_listener_is_a_child_and_controller_owns_receipt_ack(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "x.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    fixture = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    endpoint = Path(tempfile.gettempdir()) / ("implbench-r22-" + "a" * 20 + ".sock")
    paths = _paths(tmp_path, worktree=repo, git_dir=repo / ".git", role="git-service", service_socket=endpoint)
    spec = build_launch_spec("git-service", paths, uid=os.getuid(), gid=os.getgid(), argv=("unused",),
                             git_socket=endpoint, extra_env={"PYTHONPATH": str(Path(__file__).parents[2])})
    controls = {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
                for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")}
    identity = {
        "run_id": "oi-pi-bakeoff-r22-test", "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "a" * 64, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 1, "schedule_index": 0, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64,
        "capability_manifest_digest": "2" * 64, "reasoning_requested": "medium",
        "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-17T00:00:00Z", "ended_at": "2026-07-17T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1,
        "schema_version": "record-v2", "prior_record_digest": None, "controls": controls,
    }
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"x" * 32, identity=identity,
                         fixture_root_oid=fixture, allowed_paths=("*.txt",))
    server = ChildAttemptGitServiceServer(GitService(repo, fixture_root_oid=fixture, allowed_paths=("*.txt",), tool_gid=os.getgid()),
        root=tmp_path, attempt_id="attempt-" + "a" * 64, tool_gid=os.getgid(), peer_uids=(os.getuid(),),
        launch_spec=spec, receipt_chain=chain, allow_unprofiled_test=True)
    binding = server.start()
    try:
        assert server._process.pid != os.getpid()
        client = RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=os.getgid())
        assert client.handle({"op": "status"})["head"] == fixture
        (repo / "x.txt").write_text("changed\n")
        client.handle({"op": "add", "paths": ["x.txt"]})
        client.handle({"op": "commit", "message": "change"})
        assert chain.verify() == 1
        assert chain._rows()[0]["payload"]["controller_sequence"] == 1
    finally:
        server.close()
    assert not Path(binding["endpoint"]).exists()
    assert server._process is not None and server._process.poll() is not None


def test_r22_importer_is_a_bounded_short_lived_child(tmp_path: Path) -> None:
    source = tmp_path / "source"
    body = b"hello\n"
    header = b"blob " + str(len(body)).encode()
    oid = hashlib.sha1(header + b"\0" + body).hexdigest()
    loose = source / ".git" / "objects" / oid[:2] / oid[2:]
    loose.parent.mkdir(parents=True)
    loose.write_bytes(zlib.compress(header + b"\0" + body))
    paths = _paths(tmp_path / "importer", worktree=tmp_path / "importer" / "work",
                   git_dir=tmp_path / "importer" / "git", role="importer")
    spec = build_launch_spec("importer", paths, uid=os.getuid(), gid=os.getgid(), argv=("unused",),
                             extra_env={"PYTHONPATH": str(Path(__file__).parents[2])})
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        result, evidence = import_from_descriptor_child(
            source_fd, paths.runtime / "bundle", launch_spec=spec, allow_unprofiled_test=True,
        )
    finally:
        os.close(source_fd)
    assert result.object_ids == (oid,)
    assert evidence["pid"] != os.getpid()
    assert evidence["uid"] == os.getuid()
    assert evidence["profile_digest"] == spec.profile_digest
    assert evidence["limits"] == ImportLimits().__dict__
    assert not list(paths.runtime.glob(".implbench-import-*"))
    assert subprocess.run(["ps", "-p", str(evidence["pid"])], capture_output=True, check=False).returncode != 0


def test_r22_incremental_decompression_rejects_before_expanded_body_allocation() -> None:
    body = b"x" * (8 * 1024 * 1024)
    raw = b"blob " + str(len(body)).encode() + b"\0" + body
    payload = zlib.compress(raw, level=9)
    tracemalloc.start()
    with pytest.raises(ImporterError, match="ratio"):
        _loose_object_oid("0" * 40, payload, ImportLimits(max_compression_ratio=10))
    _current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    with pytest.raises(ImporterError):
        _loose_object_oid("0" * 40, payload[:-1], ImportLimits(max_compression_ratio=10))
    with pytest.raises(ImporterError):
        _loose_object_oid("0" * 40, payload + b"trailing", ImportLimits(max_compression_ratio=10))


def _r22c_identity() -> dict[str, object]:
    controls = {
        name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
        for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")
    }
    return {
        "run_id": "oi-pi-bakeoff-r22c-receipt-boundary", "cell_id": "cell-" + "c" * 64,
        "attempt_id": "attempt-" + "c" * 64, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 1, "schedule_index": 0, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64,
        "capability_manifest_digest": "2" * 64, "reasoning_requested": "medium",
        "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-17T00:00:00Z", "ended_at": "2026-07-17T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1,
        "schema_version": "record-v2", "prior_record_digest": None, "controls": controls,
    }


def _r22c_receipt_server(tmp_path: Path, chain: object):
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    repo = tmp_path / "repo"; repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "x.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    fixture = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    endpoint = Path(tempfile.gettempdir()) / ("implbench-r22c-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:20] + ".sock")
    endpoint.unlink(missing_ok=True)
    paths = _paths(tmp_path, worktree=repo, git_dir=repo / ".git", role="git-service", service_socket=endpoint)
    spec = build_launch_spec("git-service", paths, uid=os.getuid(), gid=os.getgid(), argv=("unused",),
                             git_socket=endpoint, extra_env={"PYTHONPATH": str(Path(__file__).parents[2])})
    server = ChildAttemptGitServiceServer(
        GitService(repo, fixture_root_oid=fixture, allowed_paths=("x.txt",), tool_gid=os.getgid()),
        root=tmp_path, attempt_id="attempt-" + "c" * 64, tool_gid=os.getgid(), peer_uids=(os.getuid(),),
        launch_spec=spec, receipt_chain=chain, allow_unprofiled_test=True,
    )
    binding = server.start()
    return repo, fixture, server, binding


def test_r22c_rejected_candidate_never_receives_a_receipt_ack(tmp_path: Path) -> None:
    identity = _r22c_identity()

    class RejectingChain:
        def __init__(self) -> None:
            self.identity = identity
            self.candidates: list[dict[str, object]] = []

        def append(self, candidate: dict[str, object]) -> None:
            self.candidates.append(dict(candidate))
            raise ReceiptError("rejected candidate")

    chain = RejectingChain()
    repo, fixture, server, binding = _r22c_receipt_server(tmp_path, chain)
    try:
        client = RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=os.getgid())
        (repo / "x.txt").write_text("candidate\n", encoding="utf-8")
        client.handle({"op": "add", "paths": ["x.txt"]})
        with pytest.raises(GitRPCError, match="rejected receipt candidate"):
            client.handle({"op": "commit", "message": "rejected candidate"})
        assert len(chain.candidates) == 1
        assert chain.candidates[0]["controller_sequence"] is None
        assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == fixture
    finally:
        server.close()
    assert server._process is not None and server._process.poll() is not None
    assert not Path(binding["endpoint"]).exists()


def test_r22c_controller_fsync_failure_rejects_before_child_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import implbench.harness.authlog as authlog

    identity = _r22c_identity()
    key = b"k" * 32
    log = tmp_path / "receipts.ndjson"
    chain = ReceiptChain(log, key, identity=identity, fixture_root_oid="0" * 40, allowed_paths=("x.txt",))
    repo, fixture, server, binding = _r22c_receipt_server(tmp_path / "server", chain)
    chain.fixture_root_oid = fixture
    real_fsync = authlog.os.fsync
    calls: list[int] = []

    def fail_fsync(fd: int) -> None:
        calls.append(fd)
        raise OSError("r22c injected receipt fsync failure")

    monkeypatch.setattr(authlog.os, "fsync", fail_fsync)
    try:
        client = RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=os.getgid())
        (repo / "x.txt").write_text("fsync failure\n", encoding="utf-8")
        client.handle({"op": "add", "paths": ["x.txt"]})
        with pytest.raises(GitRPCError, match="rejected receipt candidate"):
            client.handle({"op": "commit", "message": "fsync failure"})
        assert calls
        assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == fixture
        with pytest.raises(ReceiptError, match="state is missing"):
            ReceiptChain(log, key, identity=identity, fixture_root_oid=fixture, allowed_paths=("x.txt",))
    finally:
        server.close()
    assert server._process is not None and server._process.poll() is not None
    assert not Path(binding["endpoint"]).exists()


def test_r22c_child_exit_awaiting_ack_recovers_only_the_durable_controller_receipt(tmp_path: Path) -> None:
    identity = _r22c_identity()
    key = b"k" * 32
    log = tmp_path / "receipts.ndjson"
    durable = ReceiptChain(log, key, identity=identity, fixture_root_oid="0" * 40, allowed_paths=("x.txt",))
    entered = threading.Event()
    release = threading.Event()
    appended = threading.Event()

    class BlockingChain:
        def __init__(self) -> None:
            self.identity = identity

        def append(self, candidate: dict[str, object]) -> dict[str, object]:
            entered.set()
            assert release.wait(timeout=5), "controller receipt append did not resume"
            row = durable.append(candidate)
            appended.set()
            return row

    repo, fixture, server, binding = _r22c_receipt_server(tmp_path / "server", BlockingChain())
    durable.fixture_root_oid = fixture
    errors: list[Exception] = []
    client = RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=os.getgid())
    (repo / "x.txt").write_text("awaiting ack\n", encoding="utf-8")
    client.handle({"op": "add", "paths": ["x.txt"]})

    def commit() -> None:
        try:
            client.handle({"op": "commit", "message": "child exits before ack"})
        except Exception as exc:  # the child side must observe the lost acknowledgement as failure
            errors.append(exc)

    worker = threading.Thread(target=commit)
    worker.start()
    assert entered.wait(timeout=5)
    assert server._process is not None
    server._process.terminate()
    server._process.wait(timeout=5)
    release.set()
    worker.join(timeout=5)
    try:
        assert errors
        assert appended.wait(timeout=5)
        assert durable.verify() == 1
        restarted = ReceiptChain(log, key, identity=identity, fixture_root_oid=fixture, allowed_paths=("x.txt",))
        assert restarted.verify() == 1
        row = restarted._rows()[0]["payload"]
        assert row["controller_sequence"] == 1
        assert row["cell_id"] == identity["cell_id"] and row["attempt_id"] == identity["attempt_id"]
    finally:
        server.close()
    assert not Path(binding["endpoint"]).exists()
