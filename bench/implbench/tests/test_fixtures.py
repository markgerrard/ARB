from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from implbench.harness.fixtures import (
    ForgeHostUnsupported,
    materialize,
)
from implbench.harness.ref_protection import bakeoff_result_ref, bakeoff_run_ref, write_bakeoff_ref
from implbench.harness.ref_protection import prune_protected_refs
from implbench.harness.tasks import TaskSchemaError, corpus_version, load_corpus, load_task, static_prefix


def _write_task(root: Path, **overrides: object) -> Path:
    (root / "batteries").mkdir(parents=True, exist_ok=True)
    (root / "batteries" / "battery.enc").write_text("implbench:method=openssl-aes-256-cbc-pbkdf2\nx")
    task_dir = root / "fixtures" / "task-one"
    (task_dir / "tree").mkdir(parents=True)
    (task_dir / "tree" / "bucket.py").write_text("VALUE = 1\n")
    data = {
        "task_id": "task-one",
        "cluster": "C1",
        "brief": "Implement the thing with tests first.",
        "tdd": True,
        "battery_id": "battery",
        "worker_test_command": "python -m pytest tests -q --junitxml=report.xml",
        "worker_test_report": "report.xml",
        "expected_artifacts": ["bucket.py"],
        "allowed_paths": ["bucket.py", "tests/**"],
        "baseline_test_paths": ["tests/test_baseline.py"],
        "worker_test_paths": ["tests/test_worker.py"],
        "prohibitions": [{"kind": "regex", "pattern": "requests"}],
        "timeout_s": 900,
    }
    data.update(overrides)
    path = task_dir / "task.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_load_valid_task_roundtrips(tmp_path: Path) -> None:
    """RED: without tasks.py, a valid manifest cannot roundtrip into a frozen Task."""
    task = load_task(_write_task(tmp_path))
    assert task.task_id == "task-one"
    assert task.cluster == "C1"
    assert task.tdd is True
    assert task.expected_artifacts == ("bucket.py",)
    with pytest.raises(Exception):
        task.cluster = "C2"  # type: ignore[misc]


def test_unknown_top_level_key_raises(tmp_path: Path) -> None:
    """RED: extra manifest keys must be refused at load."""
    path = _write_task(tmp_path, extra=True)
    with pytest.raises(TaskSchemaError):
        load_task(path)


def test_cluster_enum_enforced(tmp_path: Path) -> None:
    """RED: clusters outside C1..C7 must be schema errors."""
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path, cluster="C8"))


def test_baseline_worker_paths_disjoint(tmp_path: Path) -> None:
    """RED: baseline and worker test paths must be disjoint."""
    with pytest.raises(TaskSchemaError):
        load_task(
            _write_task(
                tmp_path,
                baseline_test_paths=["tests/shared.py"],
                worker_test_paths=["tests/shared.py"],
            )
        )


def test_tdd_requires_worker_paths_and_report(tmp_path: Path) -> None:
    """RED: tdd tasks must name worker paths and a report-emitting command."""
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path, worker_test_paths=[]))
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path / "b", worker_test_command="python -m pytest tests"))


def test_expected_artifact_outside_allowlist_raises(tmp_path: Path) -> None:
    """RED: expected artifacts outside allowed_paths are unreachable and refused."""
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path, expected_artifacts=["outside.py"]))


def test_prohibition_kind_enum(tmp_path: Path) -> None:
    """RED: unknown prohibition kinds must be refused."""
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path, prohibitions=[{"kind": "shell"}]))


def test_rootless_glob_refused_at_load(tmp_path: Path) -> None:
    """RED: empty static-prefix globs would reject every bridge path and must fail."""
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path, allowed_paths=["**"]))
    with pytest.raises(TaskSchemaError):
        load_task(_write_task(tmp_path / "b", allowed_paths=["*.py"]))
    assert load_task(_write_task(tmp_path / "c", allowed_paths=["bucket.py", "tests/**"]))


def test_missing_battery_and_plaintext_raise(tmp_path: Path) -> None:
    """RED: battery_id must name ciphertext and plaintext twins are forbidden."""
    path = _write_task(tmp_path)
    (tmp_path / "batteries" / "battery.enc").unlink()
    with pytest.raises(TaskSchemaError):
        load_task(path)
    path = _write_task(tmp_path / "b")
    (tmp_path / "b" / "batteries" / "battery.py").write_text("plain")
    with pytest.raises(TaskSchemaError):
        load_task(path)


def test_static_prefix_table() -> None:
    assert {
        "tests/**": "tests/",
        "src/*/x.py": "src/",
        "bucket.py": "bucket.py",
        "**": "",
        "*.py": "",
    } == {g: static_prefix(g) for g in ["tests/**", "src/*/x.py", "bucket.py", "**", "*.py"]}


def test_corpus_version_is_content_hash(tmp_path: Path) -> None:
    _write_task(tmp_path)
    first = corpus_version(tmp_path)
    second = corpus_version(tmp_path)
    assert first == second
    (tmp_path / "fixtures" / "task-one" / "tree" / "bucket.py").write_text("VALUE = 2\n")
    assert corpus_version(tmp_path) != first


def test_authoritative_corpus_has_exact_eight_tasks() -> None:
    tasks = load_corpus(Path(__file__).resolve().parents[1])
    assert [task.task_id for task in tasks] == [
        "c1-permissive-boundary",
        "c1-token-bucket",
        "c2-parser",
        "c3-refactor",
        "c4-rail",
        "c5-artifact",
        "c6-scope",
        "c7-provenance",
    ]


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = _write_task(tmp_path)
    path.write_text(path.read_text().replace("task_id: task-one\n", "task_id: task-one\ntask_id: task-two\n"))
    with pytest.raises(TaskSchemaError):
        load_task(path)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_materialize_deterministic(tmp_path: Path) -> None:
    """RED: without pinned identity/date the same fixture tree can hash differently."""
    task = load_task(_write_task(tmp_path / "corpus"))
    repo = _repo(tmp_path)
    first = materialize(task, repo)
    second = materialize(task, repo)
    assert first == second
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "branch", "--list") == ""


def test_materialize_subdirs_and_exec_bit(tmp_path: Path) -> None:
    """RED: recursive mktree must preserve subdirs and tree.mode executable bits."""
    path = _write_task(tmp_path / "corpus")
    tree = path.parent / "tree"
    (tree / "bin").mkdir()
    (tree / "bin" / "run").write_text("#!/bin/sh\n")
    (tree / "tree.mode").write_text("100755 bin/run\n")
    task = load_task(path)
    repo = _repo(tmp_path)
    sha = materialize(task, repo)
    listing = _git(repo, "ls-tree", "-r", sha)
    assert "100755 blob" in listing and "\tbin/run" in listing
    assert "\tbucket.py" in listing


def test_materialize_refuses_forge_host_repo(tmp_path: Path) -> None:
    """RED: v1 fixture materialization must explicitly reject forge-host repos."""
    task = load_task(_write_task(tmp_path / "corpus"))
    with pytest.raises(ForgeHostUnsupported):
        materialize(task, "ssh://forge.example/repo.git")


def test_bakeoff_run_ref_is_cell_and_attempt_scoped(tmp_path: Path) -> None:
    """RED: scored fixture reachability uses the immutable cell/attempt namespace."""
    task = load_task(_write_task(tmp_path / "corpus"))
    repo = _repo(tmp_path)
    fixture_sha = materialize(task, repo)
    run_id = "oi-pi-bakeoff-test-20260714T000000Z"
    cell_id = "cell-" + "a" * 64
    attempt_id = "attempt-" + "b" * 32
    write_bakeoff_ref(repo, "runs", run_id, cell_id, attempt_id, fixture_sha)
    assert _git(repo, "rev-parse", bakeoff_run_ref(run_id, cell_id, attempt_id)) == fixture_sha


def test_bakeoff_result_ref_keeps_worker_commit_reachable(tmp_path: Path) -> None:
    """RED: the authenticated cell/attempt result ref keeps imported output reachable."""
    task = load_task(_write_task(tmp_path / "corpus"))
    repo = _repo(tmp_path)
    fixture_sha = materialize(task, repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), fixture_sha)
    (wt / "result.txt").write_text("kept\n")
    _git(wt, "add", "result.txt")
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-q", "-m", "worker result"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "worker",
            "GIT_AUTHOR_EMAIL": "worker@localhost",
            "GIT_COMMITTER_NAME": "worker",
            "GIT_COMMITTER_EMAIL": "worker@localhost",
        },
    )
    head_after = _git(wt, "rev-parse", "HEAD")
    run_id = "oi-pi-bakeoff-test-20260714T000000Z"
    cell_id = "cell-" + "a" * 64
    attempt_id = "attempt-" + "b" * 32
    write_bakeoff_ref(repo, "results", run_id, cell_id, attempt_id, head_after)
    _git(repo, "worktree", "remove", str(wt))
    _git(repo, "reflog", "expire", "--expire-unreachable=now", "--all")
    _git(repo, "gc", "--prune=now")
    _git(repo, "cat-file", "-e", head_after)


def test_prune_removes_only_before_date(tmp_path: Path) -> None:
    """RED: prune must delete only dated run/result refs older than the cutoff."""
    task = load_task(_write_task(tmp_path / "corpus"))
    repo = _repo(tmp_path)
    fixture_sha = materialize(task, repo)
    _git(repo, "update-ref", f"refs/implbench/runs/implbench-seat-20260708T010203Z/{task.task_id}", fixture_sha)
    _git(repo, "update-ref", f"refs/implbench/results/implbench-seat-20260708T010203Z/{task.task_id}", fixture_sha)
    _git(repo, "update-ref", f"refs/implbench/runs/implbench-seat-20260710T010203Z/{task.task_id}", fixture_sha)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    removed = prune_protected_refs(repo, "2026-07-09", evidence_root=evidence)
    assert f"refs/implbench/runs/implbench-seat-20260708T010203Z/{task.task_id}" in removed
    assert _git(repo, "rev-parse", f"refs/implbench/runs/implbench-seat-20260710T010203Z/{task.task_id}") == fixture_sha


def test_bakeoff_ref_writer_rejects_invalid_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_id = "oi-pi-bakeoff-test-20260714T000000Z"
    _git(repo, "commit", "--allow-empty", "-qm", "root")
    oid = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(Exception):
        write_bakeoff_ref(repo, "runs", run_id, "cell-x", "attempt-y", oid)
    with pytest.raises(Exception):
        write_bakeoff_ref(repo, "results", run_id, "cell-" + "a" * 64, "attempt-" + "b" * 32, "bad")
