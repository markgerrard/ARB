"""Derive H2 environmental-assumption candidates from review diffs."""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateAssumption:
    id: str
    kind: str
    callee: str
    relpath: str
    occurrence: int
    site: str


def candidate_id(kind: str, relpath: str, callee: str, occurrence: int) -> str:
    return f"{kind}:{relpath}:{callee}#{occurrence}"


def derive(files: dict[str, str], diff: str) -> list[CandidateAssumption]:
    added_lines_by_path = _added_lines_by_path(diff)
    candidates: list[CandidateAssumption] = []

    for relpath in sorted(files):
        if not relpath.endswith(".py"):
            continue

        added_counts = Counter(added_lines_by_path.get(relpath, []))
        if not added_counts:
            continue

        source = files[relpath]
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=relpath)
        except SyntaxError:
            continue

        occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
        for call in _module_level_calls(tree):
            callee = _dotted_callee(call.func)
            kind = _kind_for_callee(callee)
            if kind is None or callee is None:
                continue

            occurrences[(kind, callee)] += 1
            occurrence = occurrences[(kind, callee)]
            line = _source_line(source_lines, call)
            if line is None or added_counts[line] <= 0:
                continue

            added_counts[line] -= 1
            candidates.append(
                CandidateAssumption(
                    id=candidate_id(kind, relpath, callee, occurrence),
                    kind=kind,
                    callee=callee,
                    relpath=relpath,
                    occurrence=occurrence,
                    site=f"{relpath}:{call.lineno}",
                )
            )

    return candidates


def _module_level_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for statement in tree.body:
        calls.extend(_calls_in_module_statement(statement))
    return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def _calls_in_module_statement(statement: ast.stmt) -> list[ast.Call]:
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return []
    if isinstance(statement, ast.If):
        calls: list[ast.Call] = []
        if not _is_excluded_if(statement.test):
            for child in statement.body:
                calls.extend(_calls_in_module_statement(child))
        for child in statement.orelse:
            calls.extend(_calls_in_module_statement(child))
        return calls
    if isinstance(statement, ast.Try):
        calls = []
        if not _is_noop_guarded_try(statement):
            for child in statement.body:
                calls.extend(_calls_in_module_statement(child))
        for child in [*statement.orelse, *statement.finalbody]:
            calls.extend(_calls_in_module_statement(child))
        return calls
    return [node for node in ast.walk(statement) if isinstance(node, ast.Call)]


def _is_excluded_if(test: ast.AST) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left = _constant_string(test.left)
    right = _constant_string(test.comparators[0])
    return {left, right} == {"__name__", "__main__"}


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_noop_guarded_try(statement: ast.Try) -> bool:
    return any(not _handler_body_contains_bare_raise(handler) for handler in statement.handlers)


def _handler_body_contains_bare_raise(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Raise) and node.exc is None
        for child in handler.body
        for node in ast.walk(child)
    )


def _kind_for_callee(callee: str | None) -> str | None:
    if callee in {"redis.from_url", "redis.Redis"}:
        return "redis"
    if callee == "psycopg.connect":
        return "postgres"
    if callee in {
        "subprocess.run",
        "subprocess.Popen",
        "socket.socket",
        "socket.create_connection",
        "urllib.request.urlopen",
    }:
        return "external"
    if callee in {
        "requests.delete",
        "requests.get",
        "requests.head",
        "requests.options",
        "requests.patch",
        "requests.post",
        "requests.put",
    }:
        return "external"
    return None


def _dotted_callee(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_callee(node.value)
        if prefix is None:
            return None
        return f"{prefix}.{node.attr}"
    return None


def _source_line(source_lines: list[str], node: ast.AST) -> str | None:
    lineno = getattr(node, "lineno", None)
    if not isinstance(lineno, int) or lineno < 1 or lineno > len(source_lines):
        return None
    return source_lines[lineno - 1].strip()


def _added_lines_by_path(diff: str) -> dict[str, list[str]]:
    added_by_path: dict[str, list[str]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = _path_from_diff_header(line)
            if current_path is not None:
                added_by_path.setdefault(current_path, [])
            continue
        if line.startswith("+++ b/"):
            current_path = line.removeprefix("+++ b/")
            added_by_path.setdefault(current_path, [])
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current_path is not None:
                added_by_path.setdefault(current_path, []).append(line[1:].strip())
    return added_by_path


def _path_from_diff_header(line: str) -> str | None:
    parts = line.split()
    if len(parts) < 4 or not parts[3].startswith("b/"):
        return None
    return parts[3].removeprefix("b/")


__all__ = ["CandidateAssumption", "candidate_id", "derive"]
