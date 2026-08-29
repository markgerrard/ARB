"""H1 config-drift AST kernel.

H1 covers direct literal env readers: module-level assignments using
``os.environ.get("NAME", DEFAULT)`` or ``os.environ["NAME"]``. It intentionally
does not claim full coverage for indirected readers such as wrapper functions,
config objects, re-exports, aliases, or runtime-built keys. When a changed
module exposes only such unanalyzable reader shapes, the hunt emits a
``could-not-analyze:`` finding instead of silently clearing the scenario.
The sibling scan is intentionally same-directory only; same-named literals in
other packages are outside this check's scope.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from skills.defect_hunts.types import Finding, Scenario, Verdict


@dataclass(frozen=True)
class _EnvAssignment:
    path: str
    name: str
    env_name: str | None
    default: Any
    has_default: bool


@dataclass(frozen=True)
class _LiteralAssignment:
    path: str
    name: str
    value: Any


@dataclass(frozen=True)
class _IndirectAssignment:
    path: str
    name: str


@dataclass(frozen=True)
class _ModuleAssignments:
    env: list[_EnvAssignment]
    literals: list[_LiteralAssignment]
    indirect: list[_IndirectAssignment]


def hunt(scenario: Scenario) -> Verdict:
    modules = {
        path: _parse_module_assignments(path, content)
        for path, content in scenario.files.items()
        if path.endswith(".py")
    }
    changed_env = _changed_env_assignments(scenario, modules)
    findings: list[Finding] = []

    for assignment in changed_env:
        findings.append(_finding_for_env_assignment(assignment, modules))

    if not findings:
        changed_indirect = _changed_indirect_assignments(scenario, modules)
        if changed_indirect:
            findings.append(_could_not_analyze_finding(changed_indirect[0]))

    if not findings:
        findings.append(
            Finding(
                subject=scenario.scenario_id,
                kind="H1",
                decision="CLEAR",
                evidence="no changed direct env-derived module global found",
            )
        )

    return Verdict(findings)


def _parse_module_assignments(path: str, content: str) -> _ModuleAssignments:
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return _ModuleAssignments(env=[], literals=[], indirect=[])

    env: list[_EnvAssignment] = []
    literals: list[_LiteralAssignment] = []
    indirect: list[_IndirectAssignment] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        if not names:
            continue

        env_reader = _direct_env_reader(statement.value)
        literal_marker = _literal_value(statement.value)
        for name in names:
            if env_reader is not None:
                env_name, has_default, default = env_reader
                env.append(
                    _EnvAssignment(
                        path=path,
                        name=name,
                        env_name=env_name,
                        default=default,
                        has_default=has_default,
                    )
                )
            elif literal_marker[0]:
                literals.append(_LiteralAssignment(path=path, name=name, value=literal_marker[1]))
            elif isinstance(statement.value, ast.Call):
                indirect.append(_IndirectAssignment(path=path, name=name))

    return _ModuleAssignments(env=env, literals=literals, indirect=indirect)


def _direct_env_reader(node: ast.AST) -> tuple[str | None, bool, Any] | None:
    if isinstance(node, ast.Call) and _is_os_environ_get(node.func):
        if not node.args:
            return (None, False, None)
        env_name = _string_literal(node.args[0])
        if len(node.args) >= 2:
            has_default, default = _literal_value(node.args[1])
            return (env_name, has_default, default)
        return (env_name, False, None)

    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        return (_string_literal(node.slice), False, None)

    return None


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


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_value(node: ast.AST) -> tuple[bool, Any]:
    if isinstance(node, ast.Constant) and isinstance(
        node.value,
        str | bytes | int | float | complex | bool | type(None),
    ):
        return (True, node.value)
    return (False, None)


def _changed_env_assignments(
    scenario: Scenario,
    modules: dict[str, _ModuleAssignments],
) -> list[_EnvAssignment]:
    added_lines_by_path = _added_diff_lines_by_path(scenario.diff)
    return [
        assignment
        for assignments in modules.values()
        for assignment in assignments.env
        if _assignment_appears_in_added_lines(
            assignment.name,
            added_lines_by_path.get(assignment.path, []),
        )
    ]


def _changed_indirect_assignments(
    scenario: Scenario,
    modules: dict[str, _ModuleAssignments],
) -> list[_IndirectAssignment]:
    added_lines_by_path = _added_diff_lines_by_path(scenario.diff)
    return [
        assignment
        for assignments in modules.values()
        for assignment in assignments.indirect
        if _assignment_appears_in_added_lines(
            assignment.name,
            added_lines_by_path.get(assignment.path, []),
        )
    ]


def _added_diff_lines_by_path(diff: str) -> dict[str, list[str]]:
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


def _assignment_appears_in_added_lines(name: str, added_lines: list[str]) -> bool:
    return any(line.startswith(f"{name} ") or line.startswith(f"{name}=") for line in added_lines)


def _finding_for_env_assignment(
    assignment: _EnvAssignment,
    modules: dict[str, _ModuleAssignments],
) -> Finding:
    siblings = _sibling_modules(assignment.path, modules)
    same_named_env = [
        env_assignment
        for sibling in siblings
        for env_assignment in sibling.env
        if env_assignment.name == assignment.name
    ]
    if any(env.env_name == assignment.env_name for env in same_named_env):
        return Finding(
            subject=f"{assignment.path}:{assignment.name}",
            kind="H1",
            decision="CLEAR",
            evidence=(
                f"{assignment.name} co-move: sibling also reads "
                f"{_format_env_name(assignment.env_name)}"
            ),
        )

    same_named_literals = [
        literal
        for sibling in siblings
        for literal in sibling.literals
        if literal.name == assignment.name
    ]
    matching_literals = [
        literal
        for literal in same_named_literals
        if assignment.has_default and literal.value == assignment.default
    ]
    if matching_literals:
        literal = matching_literals[0]
        return Finding(
            subject=f"{literal.path}:{literal.name}",
            kind="H1",
            decision="FLAG",
            evidence=(
                f"{literal.name} literal sibling equals env default "
                f"{_format_literal(assignment.default)} for "
                f"{_format_env_name(assignment.env_name)}"
            ),
        )

    if same_named_literals:
        literal = same_named_literals[0]
        return Finding(
            subject=f"{literal.path}:{literal.name}",
            kind="H1",
            decision="CLEAR",
            evidence=(
                f"{literal.name} literal sibling diverges from env default: "
                f"{_format_literal(literal.value)} != {_format_literal(assignment.default)}"
            ),
        )

    return Finding(
        subject=f"{assignment.path}:{assignment.name}",
        kind="H1",
        decision="CLEAR",
        evidence=f"{assignment.name} has no same-named literal sibling",
    )


def _sibling_modules(
    path: str,
    modules: dict[str, _ModuleAssignments],
) -> list[_ModuleAssignments]:
    parent = PurePosixPath(path).parent
    return [
        assignments
        for candidate_path, assignments in modules.items()
        if candidate_path != path and PurePosixPath(candidate_path).parent == parent
    ]


def _could_not_analyze_finding(assignment: _IndirectAssignment) -> Finding:
    return Finding(
        subject=f"{assignment.path}:{assignment.name}",
        kind="H1",
        decision="FLAG",
        evidence=(
            "could-not-analyze: H1 covers direct os.environ readers; "
            "this changed module global uses an indirect reader"
        ),
    )


def _format_env_name(env_name: str | None) -> str:
    if env_name is None:
        return "a non-literal env key"
    return f'"{env_name}"'


def _format_literal(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return repr(value)


__all__ = ["hunt"]
