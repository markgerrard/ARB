from __future__ import annotations

import ast
import subprocess
import textwrap

from skills.defect_hunts.h1_config_drift import hunt
from skills.defect_hunts.scenarios import seal, seal_files


def test_sealed_scenario_keeps_h1_shape_and_strips_textual_anchors():
    files = {
        "pkg/bus.py": """
import os

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
        "pkg/audit.py": """
PREFIX = ""
LABEL = "held-axis audit-prefix"
""",
    }
    diff = """
diff --git a/pkg/bus.py b/pkg/bus.py
@@
-PREFIX = ""
+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
"""

    scenario = seal_files(
        files,
        diff,
        anchors={
            "class_names": ["held-axis", "audit-prefix"],
            "filenames": ["audit.py"],
            "symbols": ["PREFIX"],
            "scenario_ids": ["audit-prefix"],
        },
    )

    joined = "\n".join([*scenario.files.keys(), *scenario.files.values(), scenario.diff])
    assert "held-axis" not in joined
    assert "audit-prefix" not in joined
    assert "audit.py" not in joined
    assert "PREFIX" not in joined

    assignments = _module_assignments(scenario.files)
    renamed_prefix = _single_name(
        name
        for values in assignments.values()
        for name, value in values.items()
        if _is_env_default(value, "")
    )
    assert any(
        isinstance(value, ast.Constant) and value.value == ""
        for values in assignments.values()
        for name, value in values.items()
        if name == renamed_prefix
    )


def test_import_targets_are_rewritten_out_of_import_statements():
    scenario = seal_files(
        {
            "pkg/bus.py": """
def ensure_group(value):
    return value
""",
            "pkg/audit.py": """
from .bus import ensure_group

PREFIX = ensure_group("")
""",
        },
        """
diff --git a/pkg/audit.py b/pkg/audit.py
@@
+from .bus import ensure_group
+PREFIX = ensure_group("")
""",
        anchors={
            "filenames": ["bus.py", "audit.py"],
            "symbols": ["ensure_group", "PREFIX"],
        },
    )

    for path, content in scenario.files.items():
        tree = ast.parse(content, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "bus"
                assert all(alias.name != "ensure_group" for alias in node.names)
            if isinstance(node, ast.Import):
                assert all("bus" not in alias.name.split(".") for alias in node.names)


def test_renamed_import_target_resolves_to_renamed_module_and_symbol():
    scenario = seal_files(
        {
            "pkg/bus.py": """
def ensure_group(value):
    return value
""",
            "pkg/audit.py": """
from .bus import ensure_group

VALUE = ensure_group("x")
""",
        },
        """
diff --git a/pkg/audit.py b/pkg/audit.py
@@
+from .bus import ensure_group
+VALUE = ensure_group("x")
""",
        anchors={
            "filenames": ["bus.py", "audit.py"],
            "symbols": ["ensure_group", "VALUE"],
        },
    )

    module_names = {path.rsplit("/", 1)[-1].removesuffix(".py") for path in scenario.files}
    imported_symbol = None
    imported_module = None
    call_name = None

    for path, content in scenario.files.items():
        ast.parse(content, filename=path)
        tree = ast.parse(content, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_module = node.module
                imported_symbol = node.names[0].name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                call_name = node.func.id

    assert imported_module in module_names
    assert imported_symbol == call_name
    defining_path = next(path for path in scenario.files if path.endswith(f"/{imported_module}.py"))
    defining_module = scenario.files[defining_path]
    assert f"def {imported_symbol}(" in defining_module


def test_metadata_is_normalized_across_files():
    scenario = seal_files(
        {
            "pkg/bus.py": 'import os\nPREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
            "pkg/audit.py": 'PREFIX = ""\n',
        },
        'diff --git a/pkg/bus.py b/pkg/bus.py\n+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
        anchors={"filenames": ["audit.py"], "symbols": ["PREFIX"]},
    )

    line_counts = {len(content.splitlines()) for content in scenario.files.values()}
    byte_sizes = {len(content.encode("utf-8")) for content in scenario.files.values()}

    assert line_counts == {16}
    assert len(byte_sizes) == 1


def test_seal_discovers_changed_file_anchors_and_scopes_archive(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "skills" / "defect_hunts").mkdir(parents=True)
    (repo / "skills" / "defect_hunts" / "scenarios.py").write_text(
        "UNRELATED = 'skills defect_hunts source'\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "unrelated base")

    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "bus.py").write_text(
        textwrap.dedent(
            '''
            import os

            class ArbMemoryBus:
                pass

            PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")

            def ensure_group(value):
                return value
            '''
        ),
        encoding="utf-8",
    )
    (repo / "pkg" / "audit.py").write_text(
        'from .bus import PREFIX, ensure_group\nVALUE = ensure_group(PREFIX)\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    sha = _git(repo, "rev-parse", "HEAD")

    scenario = seal(sha, repo_root=repo)

    joined = _joined(scenario)
    assert "bus.py" not in joined
    assert "audit.py" not in joined
    assert "ArbMemoryBus" not in joined
    assert "ensure_group" not in joined
    assert "PREFIX" not in joined
    assert "ARB_MEMORY_PREFIX" not in joined
    assert "skills/defect_hunts" not in joined
    assert all(path.startswith("neutral_pkg/") for path in scenario.files)


def test_env_name_literals_are_neutralized_and_h1_still_flags():
    scenario = seal_files(
        {
            "pkg/bus.py": 'import os\nPREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
            "pkg/audit.py": 'PREFIX = ""\n',
        },
        'diff --git a/pkg/bus.py b/pkg/bus.py\n+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
        anchors={"filenames": ["bus.py", "audit.py"], "symbols": ["PREFIX"]},
    )

    assert "ARB_MEMORY_PREFIX" not in _joined(scenario)
    assert "ENV_00" in _joined(scenario)
    verdict = hunt(scenario)
    assert any(finding.kind == "H1" and finding.decision == "FLAG" for finding in verdict.findings)


def test_plain_import_module_references_are_renamed():
    scenario = seal_files(
        {
            "pkg/bus.py": "def value():\n    return 1\n",
            "pkg/audit.py": "import bus\nRESULT = bus.value()\n",
        },
        "diff --git a/pkg/audit.py b/pkg/audit.py\n+import bus\n+RESULT = bus.value()\n",
        anchors={"filenames": ["bus.py", "audit.py"], "symbols": ["RESULT", "value"]},
    )

    imports, attributes = _module_import_refs(scenario.files)
    assert "bus" not in _joined(scenario)
    assert imports == attributes


def test_plain_import_asname_keeps_resolvable_reference():
    scenario = seal_files(
        {
            "pkg/bus.py": "def value():\n    return 1\n",
            "pkg/audit.py": "import bus as b\nRESULT = b.value()\n",
        },
        "diff --git a/pkg/audit.py b/pkg/audit.py\n+import bus as b\n+RESULT = b.value()\n",
        anchors={"filenames": ["bus.py", "audit.py"], "symbols": ["RESULT", "value"]},
    )

    imports, attributes = _module_import_refs(scenario.files)
    assert "bus" not in _joined(scenario)
    assert imports == attributes


def test_from_dot_import_module_is_classified_as_module_target():
    scenario = seal_files(
        {
            "pkg/bus.py": "def value():\n    return 1\n",
            "pkg/audit.py": "from . import bus\nRESULT = bus.value()\n",
        },
        "diff --git a/pkg/audit.py b/pkg/audit.py\n+from . import bus\n+RESULT = bus.value()\n",
        anchors={"filenames": ["bus.py", "audit.py"], "symbols": ["RESULT", "value"]},
    )

    imports, attributes = _module_import_refs(scenario.files)
    assert "bus" not in _joined(scenario)
    assert imports == attributes


def test_from_dot_import_asname_keeps_resolvable_reference():
    scenario = seal_files(
        {
            "pkg/bus.py": "def value():\n    return 1\n",
            "pkg/audit.py": "from . import bus as b\nRESULT = b.value()\n",
        },
        "diff --git a/pkg/audit.py b/pkg/audit.py\n+from . import bus as b\n+RESULT = b.value()\n",
        anchors={"filenames": ["bus.py", "audit.py"], "symbols": ["RESULT", "value"]},
    )

    imports, attributes = _module_import_refs(scenario.files)
    assert "bus" not in _joined(scenario)
    assert imports == attributes


def test_from_dot_import_multi_aliases_are_resolvable_and_anchor_free():
    scenario = seal_files(
        {
            "pkg/bus.py": "def value():\n    return 1\n",
            "pkg/queue.py": "def other():\n    return 2\n",
            "pkg/audit.py": "from . import bus, queue as q\nRESULT = bus.value() + q.other()\n",
        },
        "diff --git a/pkg/audit.py b/pkg/audit.py\n+from . import bus, queue as q\n+RESULT = bus.value() + q.other()\n",
        anchors={
            "filenames": ["bus.py", "queue.py", "audit.py"],
            "symbols": ["RESULT", "value", "other"],
        },
    )

    imports, attributes = _module_import_refs(scenario.files)
    assert "bus" not in _joined(scenario)
    assert "queue" not in _joined(scenario)
    assert imports == attributes


def test_docstrings_are_removed_before_unparse_can_preserve_anchor_text():
    scenario = seal_files(
        {
            "pkg/bus.py": '"""ARB Memory class token."""\n\nclass Worker:\n    pass\n',
        },
        "diff --git a/pkg/bus.py b/pkg/bus.py\n+class Worker:\n",
        anchors={"filenames": ["bus.py"], "class_names": ["ARB Memory"], "symbols": ["Worker"]},
    )

    assert "ARB Memory" not in _joined(scenario)


def test_package_directory_components_are_renamed_for_local_imports():
    scenario = seal_files(
        {
            "src/arb_memory/__init__.py": "",
            "src/arb_memory/bus.py": "VALUE = 1\n",
            "src/arb_memory/audit.py": "from . import bus\nRESULT = bus.VALUE\n",
        },
        "diff --git a/src/arb_memory/audit.py b/src/arb_memory/audit.py\n+from . import bus\n",
        anchors={
            "filenames": ["arb_memory", "bus.py", "audit.py"],
            "symbols": ["VALUE", "RESULT"],
        },
    )

    assert "arb_memory" not in _joined(scenario)
    assert all("arb_memory" not in path for path in scenario.files)


def _module_assignments(files: dict[str, str]) -> dict[str, dict[str, object]]:
    assignments: dict[str, dict[str, object]] = {}
    for path, content in files.items():
        tree = ast.parse(content, filename=path)
        values: dict[str, object] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = statement.value
        assignments[path] = values
    return assignments


def _is_env_default(node: object, default: object) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == default
    )


def _single_name(names) -> str:
    values = list(names)
    assert len(values) == 1
    return values[0]


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _joined(scenario) -> str:
    return "\n".join([*scenario.files.keys(), *scenario.files.values(), scenario.diff])


def _module_import_refs(files: dict[str, str]) -> tuple[set[str], set[str]]:
    imports: set[str] = set()
    attributes: set[str] = set()
    for path, content in files.items():
        tree = ast.parse(content, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.asname or alias.name)
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module is None:
                for alias in node.names:
                    imports.add(alias.asname or alias.name)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                attributes.add(node.value.id)
    return imports, attributes
