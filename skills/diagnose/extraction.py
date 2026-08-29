"""Deterministic extraction for the read-only diagnose skill."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import hashlib
from pathlib import Path

from skills._diagnose_common import capture_artifact
from skills._diagnose_common.git_blob import blob_exists_at, read_blob_at


TRACEBACK_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')


def derive_scope(failing_test: str, recorded_traceback: dict, repo_root: str | Path, repo_sha: str | None = None) -> dict:
    """Derive an extraction scope from the failing test trigger only."""
    test_path = _test_path_from_nodeid(failing_test)
    root = Path(repo_root)
    paths = _import_closure(root, test_path, repo_sha)
    return {
        "paths": sorted(paths),
        "window": dict(recorded_traceback["window"]),
    }


def extract(repo_root: str | Path, extraction_scope: dict, repo_sha: str | None = None) -> list[dict]:
    """Capture real files listed by the extraction scope."""
    root = Path(repo_root)
    observables: list[dict] = []
    for rel_path in extraction_scope["paths"]:
        if repo_sha:
            content = read_blob_at(root, repo_sha, rel_path)
            artifact = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        else:
            artifact = capture_artifact(root / rel_path)
        observables.append(
            {
                "id": f"{rel_path}:1",
                "path": rel_path,
                "sha256": artifact["sha256"],
                "size": artifact["size"],
            }
        )
    return observables


def _test_path_from_nodeid(nodeid: str) -> str:
    return _normalise(nodeid.split("::", 1)[0])


def _import_closure(root: Path, start_path: str, repo_sha: str | None = None) -> set[str]:
    pending = [start_path]
    seen: set[str] = set()
    while pending:
        rel_path = _normalise(pending.pop())
        if rel_path in seen:
            continue
        source = _read_source(root, rel_path, repo_sha)
        if source is None:
            continue
        seen.add(rel_path)
        for module in _local_imports(source):
            module_path = _module_to_path(root, module, repo_sha)
            if module_path and module_path not in seen:
                pending.append(module_path)
    return seen


def _local_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _module_to_path(root: Path, module: str, repo_sha: str | None = None) -> str | None:
    if not module or module.split(".", 1)[0] in sys.stdlib_module_names:
        return None
    parts = module.split(".")
    file_path = root.joinpath(*parts).with_suffix(".py")
    if repo_sha and blob_exists_at(root, repo_sha, _normalise(file_path.relative_to(root))):
        return _normalise(file_path.relative_to(root))
    if not repo_sha and file_path.is_file():
        return _normalise(file_path.relative_to(root))
    package_path = root.joinpath(*parts, "__init__.py")
    if repo_sha and blob_exists_at(root, repo_sha, _normalise(package_path.relative_to(root))):
        return _normalise(package_path.relative_to(root))
    if not repo_sha and package_path.is_file():
        return _normalise(package_path.relative_to(root))
    return None


def _read_source(root: Path, rel_path: str, repo_sha: str | None) -> str | None:
    if repo_sha:
        try:
            return read_blob_at(root, repo_sha, rel_path).decode("utf-8")
        except (subprocess.SubprocessError, UnicodeDecodeError):
            return None
    source_path = root / rel_path
    if not source_path.is_file():
        return None
    return source_path.read_text(encoding="utf-8")


def _traceback_window(error_log: str) -> dict:
    lines = [int(match.group(2)) for match in TRACEBACK_LINE_RE.finditer(error_log)]
    if not lines:
        return {"start": 1, "end": 1}
    return {"start": max(1, min(lines) - 2), "end": max(lines)}


def _normalise(path: str | Path) -> str:
    return str(path).replace("\\", "/").lstrip("./")
