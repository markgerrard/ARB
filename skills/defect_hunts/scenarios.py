"""Build sealed defect-hunt scenarios without exposing fixture anchors."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from skills.defect_hunts.types import Scenario


_NORMALIZED_LINE_COUNT = 16
_SCENARIO_ID = "sealed-scenario"


def seal_files(
    files: dict[str, str],
    diff: str,
    *,
    anchors: dict[str, list[str]] | None = None,
) -> Scenario:
    """Return a self-contained, anchor-stripped scenario from in-memory files."""

    anchors = {key: list(values) for key, values in (anchors or {}).items()}
    env_keys = set(anchors.get("env_keys", []))
    env_keys.update(_env_keys_from_files(files))
    env_keys.update(_env_keys_from_text(diff))
    if env_keys:
        anchors["env_keys"] = sorted(env_keys)
    module_renames = _module_renames(files, anchors)
    path_renames = _path_renames(files, module_renames)
    symbol_renames = _symbol_renames(files, anchors)
    text_renames = _text_renames(anchors, module_renames, symbol_renames)

    sealed_files: dict[str, str] = {}
    for path, content in files.items():
        sealed_path = path_renames[path]
        sealed_content = _seal_python(content, module_renames, symbol_renames, text_renames)
        sealed_files[sealed_path] = sealed_content

    sealed_diff = _seal_text(diff, path_renames, text_renames)
    sealed_files = _normalize_file_metadata(sealed_files)
    return Scenario(scenario_id=_SCENARIO_ID, files=sealed_files, diff=sealed_diff)


def seal(pre_fix_sha: str, *, repo_root: str | Path = ".") -> Scenario:
    """Build a sealed scenario from a clean archive of ``pre_fix_sha``."""

    _require_clean_worktree(repo_root)
    changed_paths = _changed_paths(pre_fix_sha, repo_root)
    with tempfile.TemporaryDirectory(prefix="defect-scenario-") as temp_dir:
        archive_path = Path(temp_dir) / "tree.tar"
        tree_path = Path(temp_dir) / "tree"
        tree_path.mkdir()
        with archive_path.open("wb") as archive:
            subprocess.run(
                ["git", "-C", str(repo_root), "archive", "--format=tar", pre_fix_sha],
                check=True,
                stdout=archive,
            )
        shutil.unpack_archive(str(archive_path), str(tree_path), "tar")
        full_files = _read_tree_files(tree_path)
        files = _scope_files(full_files, changed_paths)
        diff = _git_show(pre_fix_sha, repo_root)
        anchors = _discover_anchors(files, changed_paths)
        anchors["commit_shas"] = [pre_fix_sha]
        anchors["scenario_ids"] = [pre_fix_sha]
    return seal_files(
        files,
        diff,
        anchors=anchors,
    )


def _require_clean_worktree(repo_root: str | Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise RuntimeError("seal requires a clean git worktree")


def _read_tree_files(tree_path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(tree_path.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel_path = path.relative_to(tree_path).as_posix()
        files[rel_path] = content
    return files


def _changed_paths(pre_fix_sha: str, repo_root: str | Path) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--root",
            pre_fix_sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _scope_files(files: dict[str, str], changed_paths: set[str]) -> dict[str, str]:
    selected = {path for path in changed_paths if path in files}
    selected.update(_local_import_closure(files, selected))
    return {path: files[path] for path in sorted(selected)}


def _local_import_closure(files: dict[str, str], roots: set[str]) -> set[str]:
    selected = set(roots)
    while True:
        next_paths = set(selected)
        for path in selected:
            next_paths.update(_local_import_paths(files, path))
        if next_paths == selected:
            return selected
        selected = next_paths


def _local_import_paths(files: dict[str, str], path: str) -> set[str]:
    content = files.get(path)
    if content is None or not path.endswith(".py"):
        return set()
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return set()

    known_paths = {PurePosixPath(candidate) for candidate in files if candidate.endswith(".py")}
    current = PurePosixPath(path)
    package_parts = current.parent.parts
    imports: set[PurePosixPath] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                imports.update(_absolute_import_matches(known_paths, parts))
                imports.update(_sibling_import_matches(known_paths, current.parent, parts))
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.update(_absolute_import_matches(known_paths, node.module.split(".")))
            for alias in node.names:
                if alias.name != "*":
                    imports.update(
                        _absolute_import_matches(
                            known_paths,
                            [*node.module.split("."), alias.name],
                        )
                    )
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            base_parts = package_parts[: max(0, len(package_parts) - node.level + 1)]
            module_parts = tuple(node.module.split(".")) if node.module else ()
            if node.module:
                imports.update(_relative_import_matches(known_paths, base_parts + module_parts))
            else:
                for alias in node.names:
                    if alias.name != "*":
                        imports.update(
                            _relative_import_matches(known_paths, base_parts + (alias.name,))
                        )
    return {candidate.as_posix() for candidate in imports}


def _absolute_import_matches(
    known_paths: set[PurePosixPath],
    parts: list[str],
) -> set[PurePosixPath]:
    candidate = PurePosixPath(*parts).with_suffix(".py")
    return {candidate} if candidate in known_paths else set()


def _sibling_import_matches(
    known_paths: set[PurePosixPath],
    parent: PurePosixPath,
    parts: list[str],
) -> set[PurePosixPath]:
    candidate = parent.joinpath(*parts).with_suffix(".py")
    return {candidate} if candidate in known_paths else set()


def _relative_import_matches(
    known_paths: set[PurePosixPath],
    parts: tuple[str, ...],
) -> set[PurePosixPath]:
    direct = PurePosixPath(*parts).with_suffix(".py")
    init = PurePosixPath(*parts, "__init__.py")
    return {candidate for candidate in (direct, init) if candidate in known_paths}


def _discover_anchors(files: dict[str, str], changed_paths: set[str]) -> dict[str, list[str]]:
    changed_py = sorted(path for path in changed_paths if path in files and path.endswith(".py"))
    filenames = {PurePosixPath(path).name for path in changed_py}
    filenames.update(_package_anchors(files, changed_py))
    symbols: set[str] = set()
    class_names: set[str] = set()

    for path in changed_py:
        try:
            tree = ast.parse(files[path], filename=path)
        except SyntaxError:
            continue
        for statement in tree.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(statement.name)
                class_names.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    symbols.update(_target_names(target))
            elif isinstance(statement, ast.AnnAssign):
                symbols.update(_target_names(statement.target))
            elif isinstance(statement, ast.AugAssign):
                symbols.update(_target_names(statement.target))

    return {
        "filenames": sorted(filenames),
        "symbols": sorted(symbols),
        "class_names": sorted(class_names),
    }


def _package_anchors(files: dict[str, str], changed_py: list[str]) -> set[str]:
    packages: set[str] = set()
    package_dirs = {
        PurePosixPath(path).parent
        for path in files
        if PurePosixPath(path).name == "__init__.py"
    }
    for path in changed_py:
        for parent in PurePosixPath(path).parents:
            if parent in package_dirs and "_" in parent.name:
                packages.add(parent.name)
    return packages


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in node.elts:
            names.update(_target_names(element))
        return names
    return set()


def _git_show(pre_fix_sha: str, repo_root: str | Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", "--format=", "--find-renames", pre_fix_sha],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout or "sealed diff unavailable\n"


def _module_renames(files: dict[str, str], anchors: dict[str, list[str]]) -> dict[str, str]:
    anchored_filenames = set(anchors.get("filenames", []))
    rename_stems = {
        PurePosixPath(path).stem
        for path in files
        if path.endswith(".py")
        and PurePosixPath(path).name != "__init__.py"
        and (
            PurePosixPath(path).name in anchored_filenames
            or PurePosixPath(path).stem in anchored_filenames
        )
    }
    path_parts = {part for path in files for part in PurePosixPath(path).parts}
    rename_stems.update(anchor for anchor in anchored_filenames if anchor in path_parts)
    rename_stems.update(_local_import_modules(files))
    return {stem: f"mod_{index:02d}" for index, stem in enumerate(sorted(rename_stems))}


def _path_renames(files: dict[str, str], module_renames: dict[str, str]) -> dict[str, str]:
    renames: dict[str, str] = {}
    used: set[str] = set()
    for index, path in enumerate(sorted(files)):
        posix = PurePosixPath(path)
        if posix.name == "__init__.py":
            filename = f"init_{index:02d}.py"
        elif posix.suffix == ".py":
            stem = module_renames.get(posix.stem, f"mod_{index:02d}")
            filename = f"{stem}.py"
        else:
            filename = f"FILE_{index:02d}{posix.suffix}"
        if filename in used:
            filename = f"{PurePosixPath(filename).stem}_{index:02d}{PurePosixPath(filename).suffix}"
        used.add(filename)
        renames[path] = PurePosixPath("neutral_pkg", filename).as_posix()
    return renames


def _symbol_renames(files: dict[str, str], anchors: dict[str, list[str]]) -> dict[str, str]:
    symbols = set(anchors.get("symbols", []))
    known_stems = {PurePosixPath(path).stem for path in files if path.endswith(".py")}
    for content in files.values():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                for alias in node.names:
                    if alias.name != "*" and not (
                        node.module is None and alias.name in known_stems
                    ):
                        symbols.add(alias.name)
    return {name: f"SYM_{index:02d}" for index, name in enumerate(sorted(symbols))}


def _local_import_modules(files: dict[str, str]) -> set[str]:
    modules: set[str] = set()
    known_stems = {PurePosixPath(path).stem for path in files if path.endswith(".py")}
    for content in files.values():
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module:
                modules.update(part for part in node.module.split(".") if part in known_stems)
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module is None:
                modules.update(alias.name for alias in node.names if alias.name in known_stems)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.update(part for part in node.module.split(".") if part in known_stems)
                modules.update(alias.name for alias in node.names if alias.name in known_stems)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.update(part for part in alias.name.split(".") if part in known_stems)
    return modules


def _env_keys_from_files(files: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError:
            keys.update(_env_keys_from_text(content))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_os_environ_get(node.func):
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    keys.add(node.args[0].value)
            if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    keys.add(node.slice.value)
    return keys


def _env_keys_from_text(text: str) -> set[str]:
    return {
        match.group("key")
        for match in re.finditer(
            r"os\.environ(?:\.get\(|\[)\s*(?P<quote>['\"])(?P<key>[^'\"]+)(?P=quote)",
            text,
        )
    }


def _is_os_environ_get(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "get"
        and _is_os_environ(node.value)
    )


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _text_renames(
    anchors: dict[str, list[str]],
    module_renames: dict[str, str],
    symbol_renames: dict[str, str],
) -> dict[str, str]:
    renames: dict[str, str] = {}
    for old, new in module_renames.items():
        renames[old] = new
        renames[f"{old}.py"] = f"{new}.py"
    renames.update(symbol_renames)
    for index, value in enumerate(anchors.get("class_names", [])):
        renames[value] = f"CLASS_{index:02d}"
    for index, value in enumerate(anchors.get("commit_shas", [])):
        renames[value] = f"SHA_{index:02d}"
    for index, value in enumerate(anchors.get("scenario_ids", [])):
        renames[value] = f"SCENARIO_{index:02d}"
    for index, value in enumerate(anchors.get("env_keys", [])):
        renames[value] = f"ENV_{index:02d}"
    for index, value in enumerate(anchors.get("filenames", [])):
        stem = value.removesuffix(".py")
        if stem in module_renames:
            renames[value] = f"{module_renames[stem]}.py"
            renames[stem] = module_renames[stem]
        else:
            renames[value] = f"FILE_{index:02d}.py" if value.endswith(".py") else f"FILE_{index:02d}"
    return dict(sorted(renames.items(), key=lambda item: len(item[0]), reverse=True))


def _seal_python(
    content: str,
    module_renames: dict[str, str],
    symbol_renames: dict[str, str],
    text_renames: dict[str, str],
) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _replace_many(content, text_renames)

    tree = _AnchorStripper(module_renames, symbol_renames, text_renames).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


class _AnchorStripper(ast.NodeTransformer):
    def __init__(
        self,
        module_renames: dict[str, str],
        symbol_renames: dict[str, str],
        text_renames: dict[str, str],
    ) -> None:
        self._module_renames = module_renames
        self._symbol_renames = symbol_renames
        self._text_renames = text_renames

    def visit_Import(self, node: ast.Import) -> ast.AST:
        self.generic_visit(node)
        for alias in node.names:
            alias.name = ".".join(
                self._module_renames.get(part, part) for part in alias.name.split(".")
            )
            if alias.asname in self._symbol_renames:
                alias.asname = self._symbol_renames[alias.asname]
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        self.generic_visit(node)
        if node.module:
            node.module = ".".join(
                self._module_renames.get(part, part) for part in node.module.split(".")
            )
        for alias in node.names:
            if node.level > 0 and node.module is None and alias.name in self._module_renames:
                alias.name = self._module_renames[alias.name]
            elif alias.name in self._symbol_renames:
                alias.name = self._symbol_renames[alias.name]
            if alias.asname in self._symbol_renames:
                alias.asname = self._symbol_renames[alias.asname]
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self._module_renames:
            node.id = self._module_renames[node.id]
        elif node.id in self._symbol_renames:
            node.id = self._symbol_renames[node.id]
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return None
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name in self._symbol_renames:
            node.name = self._symbol_renames[node.name]
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if node.name in self._symbol_renames:
            node.name = self._symbol_renames[node.name]
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if node.name in self._symbol_renames:
            node.name = self._symbol_renames[node.name]
        self.generic_visit(node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if node.attr in self._symbol_renames:
            node.attr = self._symbol_renames[node.attr]
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str):
            return ast.copy_location(
                ast.Constant(_replace_many(node.value, self._text_renames)),
                node,
            )
        return node


def _seal_text(
    text: str,
    path_renames: dict[str, str],
    text_renames: dict[str, str],
) -> str:
    renames = dict(text_renames)
    for old, new in path_renames.items():
        renames[old] = new
    sorted_renames = dict(
        sorted(renames.items(), key=lambda item: len(item[0]), reverse=True)
    )
    return _replace_many(text, sorted_renames)


def _replace_many(text: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        if old:
            text = text.replace(old, new)
    return text


def _normalize_file_metadata(files: dict[str, str]) -> dict[str, str]:
    if not files:
        return files
    line_target = max(
        _NORMALIZED_LINE_COUNT,
        max(len(content.splitlines()) for content in files.values()),
    )
    line_target = (
        (line_target + _NORMALIZED_LINE_COUNT - 1)
        // _NORMALIZED_LINE_COUNT
        * _NORMALIZED_LINE_COUNT
    )

    line_padded = {
        path: _pad_lines(content, line_target)
        for path, content in files.items()
    }
    byte_target = max(len(content.encode("utf-8")) for content in line_padded.values())
    return {
        path: _pad_bytes(content, byte_target)
        for path, content in line_padded.items()
    }


def _pad_lines(content: str, line_target: int) -> str:
    lines = content.splitlines()
    if content.endswith("\n"):
        content = content[:-1]
    pad_count = line_target - len(lines)
    if pad_count <= 0:
        return content + "\n"
    padding = "\n".join("# sealed metadata pad" for _ in range(pad_count))
    return f"{content}\n{padding}\n"


def _pad_bytes(content: str, byte_target: int) -> str:
    missing = byte_target - len(content.encode("utf-8"))
    if missing <= 0:
        return content
    lines = content.splitlines()
    if not lines:
        return content
    lines[-1] = lines[-1] + (" " * missing)
    return "\n".join(lines) + "\n"


__all__ = ["seal", "seal_files"]
