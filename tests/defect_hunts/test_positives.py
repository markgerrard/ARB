from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

from skills.defect_hunts.h1_config_drift import hunt as h1_hunt
from skills.defect_hunts.scenarios import seal_files
from skills.defect_hunts.types import Verdict


POSITIVES_PATH = Path(__file__).parent / "eval" / "positives.json"
REPO_ROOT = Path(__file__).parents[2]


def test_positives_manifest_has_h1_positive_and_h2_schema_references():
    positives = _load_positives()

    audit_prefix = _case_by_id(positives, "audit-prefix")
    assert audit_prefix["kind"] == "H1"
    assert audit_prefix["expected_decision"] == "FLAG"
    _assert_pinned_sha(audit_prefix, "113fc89~1")

    h2_cases = {
        case["id"]: case
        for case in positives["cases"]
        if case.get("kind") == "H2"
    }
    assert set(h2_cases) == {"seat-import", "boot-race-redis-per-seat"}
    for case in h2_cases.values():
        assert case["coverage"] == "schema-enforcement"
        assert "expected_decision" not in case


def test_audit_prefix_positive_seals_cleanly_and_h1_flags_real_defect():
    positives = _load_positives()
    audit_prefix = _case_by_id(positives, "audit-prefix")

    scenario = _audit_prefix_scenario(audit_prefix)
    joined = "\n".join([*scenario.files.keys(), *scenario.files.values(), scenario.diff])
    for anchor in ("audit", "PREFIX", "ARB_MEMORY_PREFIX", "audit.py"):
        assert anchor not in joined

    verdict = h1_hunt(scenario)
    assert _has_real_h1_flag(verdict)


def test_h2_references_are_schema_enforcement_only_not_h1_expected_flags():
    positives = _load_positives()

    h2_cases = [case for case in positives["cases"] if case.get("kind") == "H2"]
    assert len(h2_cases) == 2
    for case in h2_cases:
        assert case["coverage"] == "schema-enforcement"
        _assert_pinned_sha(case, case["pre_fix_ref"])
        assert case["assumption"]
        assert "expected_decision" not in case
        assert case.get("expected_hunt") != "H1"


def _load_positives() -> dict:
    return json.loads(POSITIVES_PATH.read_text(encoding="utf-8"))


def _case_by_id(positives: dict, case_id: str) -> dict:
    matches = [case for case in positives["cases"] if case["id"] == case_id]
    assert len(matches) == 1
    return matches[0]


def _assert_pinned_sha(case: dict, ref: str) -> None:
    """The pinned sha must match the ref when the ref resolves; when it does not
    (a checkout without that history), the case MUST carry its files inline so
    the corpus stays portable. Either branch can fail; neither is vacuous."""
    resolved = _rev_parse(ref)
    if resolved is None:
        assert isinstance(case.get("files"), dict) and case["files"], (
            f"{case['id']}: ref {ref} is unresolvable here and no inline files are pinned"
        )
        return
    assert case["pre_fix_sha"] == resolved


def _audit_prefix_scenario(case: dict):
    pre_fix_sha = case["pre_fix_sha"]
    if isinstance(case.get("files"), dict) and case["files"]:
        # Portable path: the corpus carries the pre-fix files inline.
        files = dict(case["files"])
    else:
        files = {
            "src/arb_memory/audit.py": _git_show(f"{pre_fix_sha}:src/arb_memory/audit.py"),
            "src/arb_memory/bus.py": _git_show(f"{pre_fix_sha}:src/arb_memory/bus.py"),
        }
    diff = """\
diff --git a/src/arb_memory/bus.py b/src/arb_memory/bus.py
@@
-PREFIX = ""
+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
"""
    return seal_files(
        files,
        diff,
        anchors={
            "filenames": ["audit.py", "bus.py"],
            "symbols": _top_level_symbols(files),
            "class_names": ["audit"],
            "scenario_ids": [case["id"], pre_fix_sha],
        },
    )


def _has_real_h1_flag(verdict: Verdict) -> bool:
    return any(
        finding.kind == "H1"
        and finding.decision == "FLAG"
        and "literal sibling equals env default" in finding.evidence
        and not finding.evidence.startswith("could-not-analyze:")
        for finding in verdict.findings
    )


def _top_level_symbols(files: dict[str, str]) -> list[str]:
    symbols: set[str] = set()
    for path, content in files.items():
        tree = ast.parse(content, filename=path)
        for statement in tree.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
    return sorted(symbols)


def _rev_parse(ref: str) -> str | None:
    try:
        return _git("rev-parse", ref)
    except subprocess.CalledProcessError:
        return None


def _git_show(ref: str) -> str:
    return _git("show", ref)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
