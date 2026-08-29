"""Boundary oracle for matching findings to enclosing symbols."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolResult:
    symbol: str | None
    tier: str


def tree_sitter_available(language: str = "python") -> bool:
    if language != "python":
        return False
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401
        return True
    except Exception:
        return False


def ctags_available() -> bool:
    if shutil.which("ctags") is None:
        return False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.py"
        p.write_text("def f():\n    return 1\n")
        return _ctags_symbol(Path(td), "x.py", 2) == "f"


def best_oracle_for_language(language: str) -> str:
    if tree_sitter_available(language):
        return "tree-sitter"
    if ctags_available():
        return "ctags"
    return "heuristic"


def enclosing_symbol(repo: Path | None, file: str | None, line: int | None) -> SymbolResult:
    symbol = _tree_sitter_symbol(repo, file, line)
    if symbol:
        return SymbolResult(symbol, "tree-sitter")
    symbol = _ctags_symbol(repo, file, line)
    if symbol:
        return SymbolResult(symbol, "ctags")
    return SymbolResult(_heuristic_symbol(repo, file, line), "heuristic")


def _tree_sitter_symbol(repo: Path | None, file: str | None, line: int | None) -> str | None:
    if repo is None or not file or line is None or not file.endswith(".py"):
        return None
    path = repo / file
    if not path.exists():
        return None
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_python

        language_obj = tree_sitter_python.language()
        try:
            language = Language(language_obj)
        except TypeError:
            language = language_obj
        parser = Parser()
        try:
            parser.language = language
        except AttributeError:
            parser.set_language(language)
        tree = parser.parse(path.read_bytes())
    except Exception:
        return None

    target = line - 1
    best: str | None = None

    def walk(node) -> None:
        nonlocal best
        if node.type in {"function_definition", "async_function_definition"}:
            start, end = node.start_point[0], node.end_point[0]
            if start <= target <= end:
                name = node.child_by_field_name("name")
                if name is not None:
                    best = path.read_bytes()[name.start_byte:name.end_byte].decode(errors="ignore")
        for child in getattr(node, "children", []):
            if child.start_point[0] <= target <= child.end_point[0]:
                walk(child)

    walk(tree.root_node)
    return best


def _ctags_symbol(repo: Path | None, file: str | None, line: int | None) -> str | None:
    if repo is None or not file or line is None or shutil.which("ctags") is None:
        return None
    path = repo / file
    if not path.exists():
        return None
    try:
        cmd = ["ctags", "-x", "--python-kinds=f", str(path)]
        proc = subprocess.run(
            cmd,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["ctags", "-x", str(path)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    best: tuple[int, str] | None = None
    for raw in proc.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        try:
            def_line = int(parts[2])
        except ValueError:
            continue
        if def_line <= line and (best is None or def_line > best[0]):
            best = (def_line, name)
    return best[1] if best else None


def _heuristic_symbol(repo: Path | None, file: str | None, line: int | None) -> str | None:
    """Small stdlib function-boundary heuristic for Python-like fixtures."""
    if repo is None or not file or line is None:
        return None
    path = repo / file
    if not path.exists():
        return None
    current_name: str | None = None
    current_indent: int | None = None
    saw_body = False
    for idx, raw in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        m = re.match(r"(\s*)(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", raw)
        if m:
            current_name = m.group(2)
            current_indent = len(m.group(1))
            saw_body = False
        elif stripped and current_indent is not None:
            if indent > current_indent:
                saw_body = True
            elif saw_body and indent <= current_indent:
                current_name = None
                current_indent = None
                saw_body = False
        if idx >= line:
            return current_name if current_indent is not None and indent > current_indent else None
    return None
