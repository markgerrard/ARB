"""Exact-code AST guard: engine-layer subprocess spawn CALLs must scrub env=.

Slice 1d-i rem2 P1-A: routing ONE spawn per family through the choke is not enough —
a second ``subprocess.run`` / ``Popen`` / ``check_output`` CALL in the same file can
still inherit the daemon env (openinterpreter ``--version`` preflight). A text/window
grep cannot distinguish a type annotation or a default-arg binding of
``subprocess.Popen`` from a real spawn CALL; this guard walks Call nodes only.

Vacuity: a synthetic un-scrubbed offender must be detected (see
``test_guard_detects_synthetic_unscrubbed_spawn``). Real-tree enumeration drives
fixes — no hand-maintained "safe list" of sites, only an explicit allowlist for
spawns that intentionally do not inherit the daemon env.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Production surfaces that may spawn engine / tool-plane children.
SCAN_ROOTS = (
    REPO / "src" / "agent_redis_bridge" / "engines",
    REPO / "src" / "agent_redis_bridge" / "scored_plane.py",
)

SPAWN_ATTRS = frozenset({"run", "Popen", "check_output"})
CHOKE_FUNCS = frozenset(
    {
        "scrubbed_child_env",
        "resolve_child_env",
        "scrub_env_dict",
    }
)

# Explicit allowlist: (repo-relative path, enclosing function name) → reason.
# Only for spawns that must NOT inherit the daemon environment and already use
# a non-daemon env mapping (probe input, fixed minimal tool-plane env, …).
# Adding a site here is a deliberate security decision, not a skip.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "src/agent_redis_bridge/engines/_stdio.py",
        "_probe_child_credential_presence",
    ): (
        "prove/probe child: env is the caller-supplied mapping under test "
        "(already scrubbed or intentionally polluted), not daemon inheritance"
    ),
    (
        "src/agent_redis_bridge/scored_plane.py",
        "execute_tool",
    ): (
        "tool-plane bash: fixed HOME/PATH/PYTHONDONTWRITEBYTECODE only; "
        "does not inherit the daemon environment"
    ),
}


def _repo_rel(path: Path) -> str:
    return path.resolve().relative_to(REPO.resolve()).as_posix()


def _is_subprocess_spawn_call(func: ast.AST) -> str | None:
    """Return 'subprocess.X' for a Call.func that is subprocess.run/Popen/check_output."""
    if (
        isinstance(func, ast.Attribute)
        and func.attr in SPAWN_ATTRS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    ):
        return f"subprocess.{func.attr}"
    return None


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _expr_routes_through_choke(node: ast.AST) -> bool:
    """True when env= expression is (or is a ternary of) a scrub choke call."""
    if isinstance(node, ast.Call) and _callee_name(node.func) in CHOKE_FUNCS:
        return True
    if isinstance(node, ast.IfExp):
        return _expr_routes_through_choke(node.body) and _expr_routes_through_choke(
            node.orelse
        )
    return False


def _method_return_dict_has_scrubbed_env(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if a method returns a dict literal with env=<choke call>."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, val in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "env":
                if val is not None and _expr_routes_through_choke(val):
                    return True
    return False


def _starstar_supplies_scrubbed_env(
    star_value: ast.AST,
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    """``**self.popen_kwargs()`` OK when that method returns env via the choke."""
    if not isinstance(star_value, ast.Call):
        return False
    if not isinstance(star_value.func, ast.Attribute):
        return False
    method = methods.get(star_value.func.attr)
    if method is None:
        return False
    return _method_return_dict_has_scrubbed_env(method)


def _enclosing_function(
    tree: ast.AST, lineno: int
) -> str:
    """Innermost function/async function name covering *lineno*, or '<module>'."""
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= lineno <= end:
            span = end - node.lineno
            if best is None or span < best[0]:
                best = (span, node.name)
    return best[1] if best else "<module>"


def _class_methods(tree: ast.AST) -> dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """class_name → {method_name → FunctionDef} for **kwargs method lookup."""
    out: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.ClassDef):
            continue
        methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods[item.name] = item
        out[node.name] = methods
    return out


def _methods_visible_at(tree: ast.AST, lineno: int) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Methods of the class (if any) that contains *lineno*."""
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.ClassDef):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= lineno <= end:
            methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = item
            return methods
    # Module-level helpers (e.g. verify_interpreter_binary near popen_kwargs class)
    # also allow looking up any class method named in **self.X() by scanning all.
    merged: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for methods in _class_methods(tree).values():
        merged.update(methods)
    # Module-level functions too.
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            merged[node.name] = node
    return merged


def _call_routes_env_through_choke(
    call: ast.Call,
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    for kw in call.keywords:
        if kw.arg == "env" and _expr_routes_through_choke(kw.value):
            return True
    for kw in call.keywords:
        if kw.arg is None and _starstar_supplies_scrubbed_env(kw.value, methods):
            return True
    return False


def unscrubbed_spawn_sites(source: str, *, rel_path: str = "<memory>") -> list[dict]:
    """Return Call-site findings that spawn without a scrubbed env= (or allowlist).

    Each finding is a dict with keys: path, lineno, spawn, function, snippet.
    """
    tree = ast.parse(source, filename=rel_path)
    findings: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        spawn = _is_subprocess_spawn_call(node.func)
        if spawn is None:
            continue
        lineno = node.lineno
        func_name = _enclosing_function(tree, lineno)
        if (rel_path, func_name) in ALLOWLIST:
            continue
        methods = _methods_visible_at(tree, lineno)
        if _call_routes_env_through_choke(node, methods):
            continue
        findings.append(
            {
                "path": rel_path,
                "lineno": lineno,
                "spawn": spawn,
                "function": func_name,
                "snippet": ast.unparse(node)[:160],
            }
        )
    return findings


def scan_engine_layer() -> list[dict]:
    """Enumerate un-scrubbed subprocess spawn CALLs under engines/ + scored_plane."""
    findings: list[dict] = []
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    for path in files:
        rel = _repo_rel(path)
        source = path.read_text(encoding="utf-8")
        findings.extend(unscrubbed_spawn_sites(source, rel_path=rel))
    return findings


def test_engine_layer_subprocess_spawns_route_env_through_scrub_choke() -> None:
    findings = scan_engine_layer()
    assert not findings, (
        "subprocess spawn CALL(s) in engines/ + scored_plane omit the scrubbed "
        "env choke (env=scrubbed_child_env/resolve_child_env/scrub_env_dict). "
        "Route every spawn through the choke, or add an explicit ALLOWLIST entry "
        "with a reason for spawns that must not inherit the daemon env:\n"
        + "\n".join(
            f"  {f['path']}:{f['lineno']} {f['spawn']} in {f['function']}() — {f['snippet']}"
            for f in findings
        )
    )


def test_guard_detects_synthetic_unscrubbed_spawn() -> None:
    """Vacuity: a fake un-scrubbed spawn must RED the guard (not vacuously green)."""
    offender = (
        "import subprocess\n"
        "def start():\n"
        "    subprocess.run(['tool', '--version'], capture_output=True)\n"
    )
    found = unscrubbed_spawn_sites(offender, rel_path="synthetic/offender.py")
    assert len(found) == 1
    assert found[0]["spawn"] == "subprocess.run"
    assert found[0]["function"] == "start"
    assert found[0]["lineno"] == 3

    # Type annotation / default-arg binding of subprocess.Popen is NOT a Call.
    not_a_call = (
        "import subprocess\n"
        "from typing import Callable\n"
        "def __init__(self, popen_factory: Callable = subprocess.Popen):\n"
        "    self.popen_factory = popen_factory\n"
    )
    assert unscrubbed_spawn_sites(not_a_call, rel_path="synthetic/default.py") == []

    # Compliant: env through choke.
    compliant = (
        "import subprocess\n"
        "from agent_redis_bridge.engines._stdio import scrubbed_child_env\n"
        "def start():\n"
        "    subprocess.Popen(['x'], env=scrubbed_child_env())\n"
    )
    assert unscrubbed_spawn_sites(compliant, rel_path="synthetic/ok.py") == []

    # Compliant via resolve_child_env.
    compliant2 = (
        "import subprocess\n"
        "def start(process_env=None):\n"
        "    subprocess.run(['x'], env=resolve_child_env(process_env))\n"
    )
    assert unscrubbed_spawn_sites(compliant2, rel_path="synthetic/ok2.py") == []

    # **self.popen_kwargs() when method returns env via choke → OK.
    via_kwargs = (
        "import subprocess\n"
        "class E:\n"
        "    def popen_kwargs(self):\n"
        "        return {'env': resolve_child_env(None)}\n"
        "    def start(self):\n"
        "        subprocess.Popen(['x'], **self.popen_kwargs())\n"
    )
    assert unscrubbed_spawn_sites(via_kwargs, rel_path="synthetic/kwargs.py") == []
