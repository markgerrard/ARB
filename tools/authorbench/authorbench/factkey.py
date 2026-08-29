from __future__ import annotations

import re
import subprocess
import tarfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class FactKey:
    path: Path
    data: dict[str, Any]

    @property
    def version(self) -> int:
        return int(self.data["version"])

    @property
    def brief_id(self) -> str:
        return self.data["brief_id"]

    @property
    def base_sha(self) -> str:
        return self.data["base_sha"]

    @property
    def facts(self) -> list[dict[str, Any]]:
        return list(self.data.get("facts") or [])

    @property
    def traps(self) -> list[dict[str, Any]]:
        return list(self.data.get("traps") or [])


@dataclass(frozen=True)
class FactKeyError:
    message: str
    item_id: str | None = None


def load_factkey(path) -> FactKey:
    path = Path(path)
    return FactKey(path=path, data=yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def export_at(base_sha: str, dest, *, repo=None) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", base_sha],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    tar_path = dest / ".archive.tar"
    tar_path.write_bytes(archive)
    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest)
    finally:
        tar_path.unlink(missing_ok=True)
    return dest


def _line_matches(path: Path, line_no: int, pattern: str) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    if line_no < 1 or line_no > len(lines):
        return False
    line = lines[line_no - 1]
    return pattern in line or re.search(pattern, line) is not None


def _symbol_exists(path: Path, symbol: str) -> bool:
    text = path.read_text(encoding="utf-8")
    escaped = re.escape(symbol)
    return re.search(rf"\b{escaped}\b", text) is not None


def _outcome_globs(factkey: FactKey) -> list[str]:
    manifest = factkey.path.with_name("bundle.yaml")
    if not manifest.exists():
        return []
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return list(data.get("outcome_globs") or [])


def _head_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.splitlines()


def _export_paths(export_dir: Path) -> list[str]:
    paths: list[str] = []
    for path in export_dir.rglob("*"):
        if path.is_file():
            paths.append(path.relative_to(export_dir).as_posix())
    return paths


def _matches(paths: list[str], pattern: str) -> list[str]:
    return [path for path in paths if fnmatch(path, pattern)]


def validate(factkey: FactKey, export_dir) -> list[FactKeyError]:
    export_dir = Path(export_dir)
    errors: list[FactKeyError] = []
    for fact in factkey.facts:
        item_id = fact.get("id")
        rel = fact.get("file")
        path = export_dir / rel
        if not path.exists():
            errors.append(FactKeyError(f"{item_id}: missing file {rel}", item_id))
            continue
        try:
            line_no = int(fact.get("line"))
        except (TypeError, ValueError):
            errors.append(FactKeyError(f"{item_id}: invalid line {fact.get('line')}", item_id))
            continue
        if not _line_matches(path, line_no, str(fact.get("pattern", ""))):
            errors.append(FactKeyError(f"{item_id}: line {rel}:{line_no} does not match pattern", item_id))

    for trap in factkey.traps:
        item_id = trap.get("id")
        rel = trap.get("file")
        symbol = trap.get("symbol")
        if not trap.get("precondition"):
            errors.append(FactKeyError(f"{item_id}: missing precondition", item_id))
        path = export_dir / rel
        if not path.exists():
            errors.append(FactKeyError(f"{item_id}: missing trap file {rel}", item_id))
            continue
        if not symbol or not _symbol_exists(path, str(symbol)):
            errors.append(FactKeyError(f"{item_id}: missing trap symbol {symbol}", item_id))

    globs = _outcome_globs(factkey)
    if globs:
        head_paths = _head_paths()
        export_paths = _export_paths(export_dir)
        for pattern in globs:
            if not _matches(head_paths, pattern):
                errors.append(FactKeyError(f"outcome_globs: {pattern} matches no HEAD paths"))
            if _matches(export_paths, pattern):
                errors.append(FactKeyError(f"outcome_globs: {pattern} matches base_sha export"))
    return errors
