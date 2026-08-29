from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from implbench.harness.git_service import (
    GIT_RPC_CONSTANTS,
    GitRPCError,
    GitService,
    TokenBucket,
    decode_frame,
    encode_frame,
    parse_porcelain_v2,
    validate_path,
)


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=input, text=True, capture_output=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_red_frame_has_exact_big_endian_length_and_incremental_canonical_json() -> None:
    payload = {"op": "status"}
    frame = encode_frame(payload)
    assert frame[:4] == len(frame[4:]).to_bytes(4, "big")
    assert decode_frame(frame) == payload
    with pytest.raises(GitRPCError):
        decode_frame((len(frame[4:]) + 1).to_bytes(4, "big") + frame[4:])
    with pytest.raises(GitRPCError):
        decode_frame(len(b'{"a":1}').to_bytes(4, "big") + b'{"a":1}x')


def test_red_rpc_constants_and_closed_actors_are_frozen() -> None:
    assert GIT_RPC_CONSTANTS == {
        "max_frame_bytes": 1048576,
        "max_path_bytes": 4096,
        "max_components_per_path": 256,
        "max_component_bytes": 255,
        "max_paths_per_request": 1024,
        "max_in_flight": 8,
        "status_rate_per_second": 4,
        "status_burst": 8,
    }
    service_ops = {"status", "hash", "stage", "tree", "commit"}
    for operation in service_ops:
        assert GitService.is_closed_operation(operation)
    with pytest.raises(GitRPCError):
        GitService.authorize_budget_candidate({"operation": "tool", "reason": "MODEL_BUDGET_EXCEEDED"})
    with pytest.raises(GitRPCError):
        GitService.authorize_request({"op": "status", "argv": ["-c", "core.hooksPath=x"]}, actor="tool")


def test_red_scored_git_service_requires_the_provisioned_tool_group(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    kwargs = {
        "fixture_root_oid": fixture,
        "allowed_paths": ("src/**",),
        "receipt_chain": object(),
        "completion_provider": lambda: {},
        "scored": True,
    }
    with pytest.raises(GitRPCError, match="tool-plane GID"):
        GitService(repo, **kwargs)
    service = GitService(repo, tool_gid=os.getgid(), **kwargs)
    assert service.tool_gid == os.getgid()


def test_red_incremental_limits_reject_paths_traversal_components_and_status_rate() -> None:
    assert validate_path("src/main.py") == "src/main.py"
    for path in ("../escape", "/absolute", "a\\b", "a//b", "a/./b", "a/../b", ""):
        with pytest.raises(GitRPCError):
            validate_path(path)
    bucket = TokenBucket(rate=4, burst=8, clock=lambda: 1.0)
    assert all(bucket.take() for _ in range(8))
    assert not bucket.take()


def test_red_porcelain_v2_parser_is_canonical_and_closed() -> None:
    raw = b"1 .M N... 100644 100644 100644 abcdef0 abcdef0 src/main.py\0? new.txt\0"
    parsed = parse_porcelain_v2(raw)
    assert parsed.changed_paths == ("src/main.py", "new.txt")
    assert parsed.dirty is True
    with pytest.raises(GitRPCError):
        parse_porcelain_v2(b"x invalid\0")


def test_red_service_add_commit_uses_fixed_plumbing_and_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('ok')\n")
    calls: list[list[str]] = []
    original = subprocess.run

    def wrapped(args, *rest, **kwargs):
        if isinstance(args, list) and args and args[0] == "git":
            calls.append(list(args))
            assert "add" not in args
            assert "-C" not in args
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", wrapped)
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    assert service.handle({"op": "status"})["head"] == fixture
    service.handle({"op": "add", "paths": ["src/main.py"]})
    result = service.handle({"op": "commit", "message": "model change"})
    assert result["commit_oid"] != fixture
    assert result["ordered_parent_oids"] == [fixture]
    assert result["changed_paths"] == ["src/main.py"]
    assert result["dirty"] is False
    assert calls
    with pytest.raises(GitRPCError):
        service.handle({"op": "add", "paths": ["README.md"]})


def test_red_receipt_append_failure_leaves_head_at_parent(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('ok')\n")

    class FailingReceiptChain:
        identity = {"cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 64}

        def append(self, payload):
            raise RuntimeError("receipt append failed")

    service = GitService(
        repo,
        fixture_root_oid=fixture,
        allowed_paths=("src/**",),
        receipt_chain=FailingReceiptChain(),
        completion_provider=lambda: {},
        scored=True,
        tool_gid=os.getgid(),
    )
    service.handle({"op": "add", "paths": ["src/main.py"]})

    with pytest.raises(RuntimeError, match="receipt append failed"):
        service.handle({"op": "commit", "message": "model change"})
    assert _git(repo, "rev-parse", "HEAD") == fixture


def test_red_update_ref_failure_appends_authenticated_infrastructure_compensation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('ok')\n")

    class RecordingReceiptChain:
        identity = {"cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 64}

        def __init__(self):
            self.receipts: list[dict[str, object]] = []
            self.failures: list[dict[str, object]] = []

        def append(self, payload):
            self.receipts.append(payload)

        def append_infrastructure_failure(self, **payload):
            self.failures.append(payload)

    chain = RecordingReceiptChain()
    service = GitService(
        repo,
        fixture_root_oid=fixture,
        allowed_paths=("src/**",),
        receipt_chain=chain,
        completion_provider=lambda: {},
        scored=True,
        tool_gid=os.getgid(),
    )
    service.handle({"op": "add", "paths": ["src/main.py"]})
    original_run = service._run

    def fail_update_ref(*args: str, **kwargs: object) -> bytes:
        if args and args[0] == "update-ref":
            raise GitRPCError("injected update-ref failure")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(service, "_run", fail_update_ref)
    with pytest.raises(GitRPCError, match="receipt admission"):
        service.handle({"op": "commit", "message": "model change"})

    assert _git(repo, "rev-parse", "HEAD") == fixture
    assert chain.receipts
    assert chain.failures == [{"operation": "update-ref", "reason": "UPDATE_REF_FAILED", "parent_oid": fixture, "commit_oid": chain.receipts[0]["commit_oid"]}]
    assert service.completion_projection()["infrastructure_failure"] == "update-ref"


def test_red_hardlink_staging_is_denied(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    source = repo / "src" / "source.py"
    source.write_text("secret\n")
    os.link(source, repo / "src" / "alias.py")
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    with pytest.raises(GitRPCError, match="hardlink"):
        service.handle({"op": "add", "paths": ["src/alias.py"]})


def _blob_oid(content: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def test_red_add_stages_only_bytes_read_through_the_no_follow_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-validation symlink swap cannot move outside bytes into the object store.

    The swap is timed after the no-follow read (deterministically, just before
    Git hashing): staging must still record the guarded bytes' oid, and the
    outside target's blob must never be persisted.
    """
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    approved = repo / "src" / "approved.txt"
    guarded_bytes = b"guarded bytes\n"
    approved.write_bytes(guarded_bytes)
    outside = tmp_path / "outside.bin"
    outside_bytes = b"outside target bytes\n"
    outside.write_bytes(outside_bytes)
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    original_run = service._run

    def swap_before_hash(*args, **kwargs):
        if "hash-object" in args:
            approved.unlink()
            approved.symlink_to(outside)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(service, "_run", swap_before_hash)
    result = service.handle({"op": "add", "paths": ["src/approved.txt"]})
    assert result["object_oids"] == [_blob_oid(guarded_bytes)]
    foreign = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", _blob_oid(outside_bytes)],
        capture_output=True, check=False,
    )
    assert foreign.returncode != 0, "outside symlink-target bytes were persisted into the object store"


def test_red_add_maps_each_distinct_path_to_its_own_blob_in_the_index(tmp_path: Path) -> None:
    """Distinct paths must land in the index with their own blob oids.

    The 1,024-path production case dedupes to one unique path, so per-path
    index correctness needs an explicit distinct-path assertion: `ls-files
    --stage` must pair every path with the blob of its own content, and the
    committed tree must carry those exact contents.  Repeated entries in the
    same request must dedupe onto the first occurrence's blob mapping.
    """
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    contents = {
        "src/one.txt": b"first file content\n",
        "src/two.txt": b"second file content, deliberately different\n",
        "src/three.txt": b"third file content, different again\n",
    }
    for name, data in contents.items():
        (repo / name).write_bytes(data)
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    request = list(contents) + ["src/one.txt", "src/three.txt"]
    result = service.handle({"op": "add", "paths": request})
    expected_oids = {name: _blob_oid(data) for name, data in contents.items()}
    assert result["object_oids"] == [expected_oids[name] for name in request]
    staged = _git(repo, "ls-files", "--stage")
    for name, data in contents.items():
        assert f" {_blob_oid(data)} " in staged and name in staged
    commit = service.handle({"op": "commit", "message": "distinct paths"})
    assert commit["commit_oid"] != fixture
    for name, data in contents.items():
        assert _git(repo, "show", f"{commit['commit_oid']}:{name}") == data.decode().rstrip("\n")


def test_red_add_stages_the_maximum_batch_of_distinct_long_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal maximum-size add of distinct long paths must complete.

    The RPC contract admits 1,024 paths of up to 4,096 bytes each, so per-path
    index data must travel to Git over stdin: expanding one ``--cacheinfo``
    argv triple per path exceeds ``ARG_MAX`` (1 MiB on this host) for a legal
    request of ~980-byte paths.  Every one of the 1,024 entries is unique, so
    the declared maximum batch of distinct paths is exercised exactly.
    """
    repo, fixture = _repo(tmp_path)
    deep_dir = "/".join(("src", "a" * 250, "b" * 250, "c" * 250))
    monkeypatch.chdir(repo)
    os.makedirs(deep_dir)
    contents: dict[str, bytes] = {}
    for index in range(GIT_RPC_CONSTANTS["max_paths_per_request"]):
        name = f"{deep_dir}/leaf-{index:04d}-" + "x" * 213
        data = f"distinct content {index}\n".encode("utf-8")
        with open(name, "wb") as handle:
            handle.write(data)
        contents[name] = data
    paths = list(contents)
    request = paths
    assert len(request) == GIT_RPC_CONSTANTS["max_paths_per_request"]
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    result = service.handle({"op": "add", "paths": request})
    expected = {name: _blob_oid(data) for name, data in contents.items()}
    assert result["object_oids"] == [expected[name] for name in request]
    staged: dict[str, str] = {}
    for line in _git(repo, "ls-files", "--stage").splitlines():
        meta, staged_path = line.split("\t", 1)
        staged[staged_path] = meta.split()[1]
    assert {name: staged[name] for name in expected} == expected
    commit = service.handle({"op": "commit", "message": "maximum distinct batch"})
    assert commit["commit_oid"] != fixture
    for name in (paths[0], paths[511], paths[1023]):
        assert _git(repo, "show", f"{commit['commit_oid']}:{name}") == contents[name].decode().rstrip("\n")


def test_red_add_preserves_guarded_bytes_under_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POSIX-legal short ``write(2)`` must not lose guarded bytes.

    ``os.write`` may consume only part of a buffer; the private-copy writer
    must loop until every byte lands so a legal add still stages exactly the
    guarded stream's blob.
    """
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    data = bytes(range(256)) * 782  # ~200 KB: spans two 128 KiB read chunks
    (repo / "src" / "large.bin").write_bytes(data)
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    real_write = os.write

    def half_write(fd: int, buffer) -> int:
        raw = bytes(buffer)
        if len(raw) >= 8192:
            return real_write(fd, raw[: len(raw) // 2])
        return real_write(fd, raw)

    monkeypatch.setattr(os, "write", half_write)
    result = service.handle({"op": "add", "paths": ["src/large.bin"]})
    monkeypatch.undo()
    assert result["object_oids"] == [_blob_oid(data)]
    staged_bytes = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", _blob_oid(data)],
        capture_output=True, check=True,
    ).stdout
    assert staged_bytes == data


def test_red_add_records_the_executable_mode_in_index_and_tree(tmp_path: Path) -> None:
    """The staged index and committed tree must carry each file's real mode.

    The stdin ``--index-info`` records embed the mode the service derived from
    the guarded ``fstat`` (``100755`` for owner-executable regular files,
    ``100644`` otherwise); a formatting or derivation defect would silently
    flatten executables, so both modes are asserted end to end.
    """
    repo, fixture = _repo(tmp_path)
    (repo / "src").mkdir()
    script = repo / "src" / "run.sh"
    script_bytes = b"#!/bin/sh\nexit 0\n"
    script.write_bytes(script_bytes)
    script.chmod(0o755)
    plain = repo / "src" / "data.txt"
    plain_bytes = b"plain data\n"
    plain.write_bytes(plain_bytes)
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("src/**",))
    result = service.handle({"op": "add", "paths": ["src/run.sh", "src/data.txt"]})
    assert result["object_oids"] == [_blob_oid(script_bytes), _blob_oid(plain_bytes)]
    staged = {}
    for line in _git(repo, "ls-files", "--stage").splitlines():
        meta, staged_path = line.split("\t", 1)
        mode, oid, _stage = meta.split()
        staged[staged_path] = (mode, oid)
    assert staged["src/run.sh"] == ("100755", _blob_oid(script_bytes))
    assert staged["src/data.txt"] == ("100644", _blob_oid(plain_bytes))
    commit = service.handle({"op": "commit", "message": "executable mode"})
    tree = {}
    for line in _git(repo, "ls-tree", "-r", str(commit["commit_oid"])).splitlines():
        meta, tree_path = line.split("\t", 1)
        mode, _kind, oid = meta.split()
        tree[tree_path] = (mode, oid)
    assert tree["src/run.sh"] == ("100755", _blob_oid(script_bytes))
    assert tree["src/data.txt"] == ("100644", _blob_oid(plain_bytes))
