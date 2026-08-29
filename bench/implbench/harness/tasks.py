from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TaskSchemaError(ValueError):
    """Raised when a task.yaml violates the pinned implbench schema."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise TaskSchemaError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class Task:
    task_id: str
    cluster: str
    brief: str
    tdd: bool
    battery_id: str
    worker_test_command: str
    worker_test_report: str
    expected_artifacts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    baseline_test_paths: tuple[str, ...]
    worker_test_paths: tuple[str, ...]
    prohibitions: tuple[dict, ...]
    timeout_s: int


_FIELDS = {
    "task_id",
    "cluster",
    "brief",
    "tdd",
    "battery_id",
    "worker_test_command",
    "worker_test_report",
    "expected_artifacts",
    "allowed_paths",
    "baseline_test_paths",
    "worker_test_paths",
    "prohibitions",
    "timeout_s",
}
_CLUSTERS = {f"C{i}" for i in range(1, 8)}
_PROHIBITION_KINDS = {"regex", "ast_import", "ast_call"}
_TASK_ID_RE = re.compile(r"^[a-z0-9-]+$")


def static_prefix(glob: str) -> str:
    first_wild = min((idx for ch in "*?[" if (idx := glob.find(ch)) != -1), default=-1)
    if first_wild == -1:
        return glob
    prefix = glob[:first_wild]
    slash = prefix.rfind("/")
    return "" if slash == -1 else prefix[: slash + 1]


def load_task(path: str | Path) -> Task:
    task_path = Path(path)
    try:
        data = yaml.load(task_path.read_text(), Loader=_UniqueKeyLoader) or {}
    except yaml.YAMLError as exc:
        raise TaskSchemaError(f"invalid task.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskSchemaError("task.yaml must be a mapping")
    unknown = set(data) - _FIELDS
    if unknown:
        raise TaskSchemaError(f"unknown top-level key(s): {sorted(unknown)}")

    missing = _FIELDS - set(data)
    if missing:
        raise TaskSchemaError(f"missing required key(s): {sorted(missing)}")

    task_id = _string(data["task_id"], "task_id")
    if not _TASK_ID_RE.match(task_id):
        raise TaskSchemaError("task_id must match ^[a-z0-9-]+$")

    cluster = _string(data["cluster"], "cluster")
    if cluster not in _CLUSTERS:
        raise TaskSchemaError("cluster must be one of C1..C7")

    baseline = tuple(_string_list(data["baseline_test_paths"], "baseline_test_paths"))
    worker_paths = tuple(_string_list(data["worker_test_paths"], "worker_test_paths"))
    if set(baseline) & set(worker_paths):
        raise TaskSchemaError("baseline_test_paths and worker_test_paths must be disjoint")

    tdd = _bool(data["tdd"], "tdd")
    worker_cmd = _string(data["worker_test_command"], "worker_test_command")
    worker_report = _string(data["worker_test_report"], "worker_test_report")
    if tdd and (not worker_paths or worker_report not in worker_cmd):
        raise TaskSchemaError("tdd:true requires worker_test_paths and command naming worker_test_report")

    expected = tuple(_string_list(data["expected_artifacts"], "expected_artifacts"))
    allowed = tuple(_string_list(data["allowed_paths"], "allowed_paths"))
    if not expected or not allowed:
        raise TaskSchemaError("expected_artifacts and allowed_paths must be non-empty")
    for glob in allowed:
        if static_prefix(glob) == "":
            raise TaskSchemaError("allowed_paths globs must have a non-empty static prefix")
    for artifact in expected:
        if not any(fnmatch.fnmatchcase(artifact, glob) for glob in allowed):
            raise TaskSchemaError(f"expected artifact outside allowed_paths: {artifact}")

    prohibitions = tuple(_dict_list(data["prohibitions"], "prohibitions"))
    for prohibition in prohibitions:
        if prohibition.get("kind") not in _PROHIBITION_KINDS:
            raise TaskSchemaError("prohibition kind must be regex, ast_import, or ast_call")

    battery_id = _string(data["battery_id"], "battery_id")
    corpus_root = task_path.parents[2]
    enc_path = corpus_root / "batteries" / f"{battery_id}.enc"
    if not enc_path.exists():
        raise TaskSchemaError(f"battery ciphertext missing: {enc_path}")
    for suffix in (".py", ".txt", ".sh", ""):
        plain = corpus_root / "batteries" / f"{battery_id}{suffix}"
        if plain.exists() and plain != enc_path:
            raise TaskSchemaError(f"plaintext battery forbidden: {plain}")

    timeout = data["timeout_s"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise TaskSchemaError("timeout_s must be a positive integer")
    task = Task(
        task_id=task_id,
        cluster=cluster,
        brief=_string(data["brief"], "brief"),
        tdd=tdd,
        battery_id=battery_id,
        worker_test_command=worker_cmd,
        worker_test_report=worker_report,
        expected_artifacts=expected,
        allowed_paths=allowed,
        baseline_test_paths=baseline,
        worker_test_paths=worker_paths,
        prohibitions=prohibitions,
        timeout_s=timeout,
    )
    object.__setattr__(task, "_manifest_path", task_path)
    return task


def corpus_version(corpus_root: str | Path) -> str:
    root = Path(corpus_root)
    paths: list[Path] = []
    paths.extend(root.glob("fixtures/*/task.yaml"))
    for tree in root.glob("fixtures/*/tree"):
        paths.extend(p for p in tree.rglob("*") if p.is_file())
    paths.extend(root.glob("batteries/*.enc"))
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.as_posix()):
        rel = path.relative_to(root).as_posix().encode()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def load_corpus(corpus_root: str | Path) -> tuple[Task, ...]:
    """Load the closed eight-task v1 corpus in its authoritative order."""
    root = Path(corpus_root)
    fixture_root = root / "fixtures" if (root / "fixtures").is_dir() else root
    expected = (
        "c1-permissive-boundary",
        "c1-token-bucket",
        "c2-parser",
        "c3-refactor",
        "c4-rail",
        "c5-artifact",
        "c6-scope",
        "c7-provenance",
    )
    if not fixture_root.is_dir() or any(path.is_symlink() for path in fixture_root.iterdir()):
        raise TaskSchemaError("corpus fixture root must be a real directory")
    entries = list(fixture_root.iterdir())
    if any(not path.is_dir() or path.is_symlink() for path in entries):
        raise TaskSchemaError("corpus root may contain only real task directories")
    for task_dir in entries:
        if any(path.is_symlink() for path in task_dir.rglob("*")):
            raise TaskSchemaError(f"symlink in fixture: {task_dir}")
    actual = tuple(sorted((path.name for path in entries), key=lambda value: value.encode("utf-8")))
    if actual != tuple(sorted(expected, key=lambda value: value.encode("utf-8"))):
        raise TaskSchemaError("corpus must contain exactly the eight frozen task IDs")
    return tuple(load_task(fixture_root / task_id / "task.yaml") for task_id in expected)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TaskSchemaError(f"{field} must be a string")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TaskSchemaError(f"{field} must be a bool")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TaskSchemaError(f"{field} must be a list of strings")
    return value


def _dict_list(value: Any, field: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TaskSchemaError(f"{field} must be a list of mappings")
    return value
