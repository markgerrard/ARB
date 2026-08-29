"""Deterministic H1 discovery eval harness.

The harness runs hunts only on sealed, in-memory scenarios. H2 is intentionally
outside this discovery runner; it is a schema-enforcement gate, not a detector.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skills.defect_hunts.scenarios import _discover_anchors, seal_files
from skills.defect_hunts.types import Finding, Hunt, Scenario, Verdict

_HUNT_TIMEOUT_SECONDS = 10


class SandboxViolation(RuntimeError):
    """Raised when a hunt tries to inspect data outside the sealed scenario."""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected_decision: str
    actual_decision: str
    passed: bool
    could_not_analyze: bool = False
    sandbox_violation: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    recall: float
    precision: float
    per_case: tuple[CaseResult, ...]
    validation_scope: str = "class-seed validation, not broad certification"

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.per_case)


def run_eval(hunt: Hunt, positives: Mapping[str, Any], negatives: Mapping[str, Any]) -> EvalResult:
    """Run ``hunt`` on sealed H1 discovery cases and return deterministic metrics."""

    cases = list(_iter_h1_cases(positives, negatives))
    keyed_results: dict[str, CaseResult] = {}
    for order in _case_permutations(cases):
        for case in order:
            scenario = _sealed_scenario(case)
            result = _run_case(hunt, scenario, case)
            previous = keyed_results.get(result.case_id)
            if previous is not None and (
                previous.actual_decision,
                previous.could_not_analyze,
                previous.evidence,
            ) != (
                result.actual_decision,
                result.could_not_analyze,
                result.evidence,
            ):
                result = CaseResult(
                    case_id=result.case_id,
                    expected_decision=result.expected_decision,
                    actual_decision=result.actual_decision,
                    passed=False,
                    could_not_analyze=result.could_not_analyze,
                    sandbox_violation=result.sandbox_violation,
                    evidence=(
                        *result.evidence,
                        "non-deterministic decision across eval permutations",
                    ),
                )
            keyed_results[result.case_id] = result

    case_results = list(keyed_results.values())

    positive_cases = [
        result for result in case_results if result.expected_decision == "FLAG"
    ]
    negative_cases = [
        result for result in case_results if result.expected_decision == "CLEAR"
    ]
    true_flags = sum(
        1
        for result in positive_cases
        if result.actual_decision == "FLAG" and not result.could_not_analyze
    )
    false_flags = sum(
        1
        for result in negative_cases
        if result.actual_decision == "FLAG" and not result.could_not_analyze
    )

    recall = true_flags / len(positive_cases) if positive_cases else 1.0
    precision = (
        true_flags / (true_flags + false_flags)
        if true_flags + false_flags
        else 1.0
    )
    return EvalResult(
        recall=recall,
        precision=precision,
        per_case=tuple(sorted(case_results, key=lambda result: result.case_id)),
    )


def _iter_h1_cases(
    positives: Mapping[str, Any],
    negatives: Mapping[str, Any],
) -> Iterator[Mapping[str, Any]]:
    for manifest in (positives, negatives):
        for case in manifest.get("cases", []):
            if case.get("kind") == "H1" and "expected_decision" in case:
                yield case


def _case_permutations(cases: list[Mapping[str, Any]]) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    return (tuple(cases), tuple(reversed(cases)))


def _sealed_scenario(case: Mapping[str, Any]) -> Scenario:
    files = case.get("files")
    if not isinstance(files, Mapping):
        files = _files_for_known_positive(case)
    diff = case.get("diff")
    if not isinstance(diff, str):
        diff = _diff_for_known_positive(case)

    anchors = dict(case.get("anchors", {})) if isinstance(case.get("anchors"), Mapping) else {}
    discovered = _discover_anchors(dict(files), set(files))
    for key, values in discovered.items():
        anchors[key] = sorted({*anchors.get(key, []), *values})
    anchors["scenario_ids"] = [str(case["id"])]
    anchors["symbols"] = sorted({*anchors.get("symbols", []), *_top_level_symbols(files)})
    if case.get("pre_fix_sha"):
        anchors["scenario_ids"].append(str(case["pre_fix_sha"]))
        anchors["commit_shas"] = [str(case["pre_fix_sha"])]
    return seal_files(dict(files), diff, anchors=anchors)


def _files_for_known_positive(case: Mapping[str, Any]) -> dict[str, str]:
    if case.get("id") != "audit-prefix":
        raise ValueError(f"positive case {case.get('id')} must include files")
    pre_fix_sha = str(case["pre_fix_sha"])
    return {
        "src/arb_memory/audit.py": _git_show(f"{pre_fix_sha}:src/arb_memory/audit.py"),
        "src/arb_memory/bus.py": _git_show(f"{pre_fix_sha}:src/arb_memory/bus.py"),
    }


def _diff_for_known_positive(case: Mapping[str, Any]) -> str:
    if case.get("id") != "audit-prefix":
        raise ValueError(f"positive case {case.get('id')} must include diff")
    return """\
diff --git a/src/arb_memory/bus.py b/src/arb_memory/bus.py
--- a/src/arb_memory/bus.py
+++ b/src/arb_memory/bus.py
@@
-PREFIX = ""
+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
"""


def _git_show(ref: str) -> str:
    repo_root = Path(__file__).parents[3]
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _top_level_symbols(files: Mapping[str, str]) -> set[str]:
    symbols: set[str] = set()
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        for statement in tree.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
    return symbols


def _run_case(hunt: Hunt, scenario: Scenario, case: Mapping[str, Any]) -> CaseResult:
    expected = str(case["expected_decision"])
    try:
        verdict = _run_hunt_out_of_process(hunt, scenario)
    except SandboxViolation as exc:
        return CaseResult(
            case_id=str(case["id"]),
            expected_decision=expected,
            actual_decision="SANDBOX",
            passed=False,
            sandbox_violation=str(exc),
        )

    findings = tuple(verdict.findings)
    could_not_analyze = _has_could_not_analyze(findings)
    real_flag = _has_real_flag(findings)
    actual = "FLAG" if real_flag else "CLEAR"
    passed = actual == expected and not could_not_analyze
    return CaseResult(
        case_id=str(case["id"]),
        expected_decision=expected,
        actual_decision=actual,
        passed=passed,
        could_not_analyze=could_not_analyze,
        evidence=tuple(finding.evidence for finding in findings),
    )


def _run_hunt_out_of_process(hunt: Hunt, scenario: Scenario) -> Verdict:
    payload = {
        "scenario": {
            "scenario_id": scenario.scenario_id,
            "files": scenario.files,
            "diff": scenario.diff,
        },
        "detector": _detector_spec(hunt),
    }
    with tempfile.TemporaryDirectory(prefix="defect-eval-sandbox-") as temp_dir:
        root = Path(temp_dir)
        _copy_defect_hunts_package(root)
        worker = root / "_worker.py"
        worker.write_text(_WORKER_SOURCE, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(worker)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                cwd=root,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
                timeout=_HUNT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxViolation(
                f"hunt subprocess timed out after {exc.timeout} seconds"
            ) from exc
    if result.returncode != 0:
        raise SandboxViolation(result.stderr.strip() or "hunt subprocess failed")
    data = json.loads(result.stdout)
    return Verdict(
        [
            Finding(
                str(item["subject"]),
                str(item["kind"]),
                str(item["decision"]),
                str(item["evidence"]),
            )
            for item in data["findings"]
        ]
    )


def _copy_defect_hunts_package(root: Path) -> None:
    package_root = root / "skills"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    source = Path(__file__).parents[1]
    shutil.copytree(
        source,
        package_root / "defect_hunts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _detector_spec(hunt: Hunt) -> dict[str, str]:
    module = getattr(hunt, "__module__", "")
    qualname = getattr(hunt, "__qualname__", "")
    if module.startswith("skills.defect_hunts.") and "<locals>" not in qualname:
        return {"mode": "import", "module": module, "qualname": qualname}
    try:
        source = textwrap.dedent(inspect.getsource(hunt))
    except OSError as exc:
        raise TypeError("non-package eval hunts must be source-inspectable") from exc
    return {"mode": "source", "source": source, "name": getattr(hunt, "__name__", qualname)}


_WORKER_SOURCE = r'''
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from skills.defect_hunts.types import Finding, Scenario, Verdict


def _load_detector(spec):
    if spec["mode"] == "import":
        target = importlib.import_module(spec["module"])
        obj = target
        for part in spec["qualname"].split("."):
            obj = getattr(obj, part)
        return obj
    namespace = {
        "Finding": Finding,
        "Scenario": Scenario,
        "Verdict": Verdict,
        "Path": Path,
    }
    exec(spec["source"], namespace)
    return namespace[spec["name"]]


payload = json.loads(sys.stdin.read())
scenario_payload = payload["scenario"]
scenario = Scenario(
    scenario_id=scenario_payload["scenario_id"],
    files=dict(scenario_payload["files"]),
    diff=scenario_payload["diff"],
)
detector = _load_detector(payload["detector"])
verdict = detector(scenario)
if not isinstance(verdict, Verdict):
    raise TypeError("hunt must return Verdict")
print(json.dumps({
    "findings": [
        {
            "subject": finding.subject,
            "kind": finding.kind,
            "decision": finding.decision,
            "evidence": finding.evidence,
        }
        for finding in verdict.findings
    ]
}))
'''


def _has_real_flag(findings: tuple[Finding, ...]) -> bool:
    return any(
        finding.kind == "H1"
        and finding.decision == "FLAG"
        and not finding.evidence.startswith("could-not-analyze:")
        for finding in findings
    )


def _has_could_not_analyze(findings: tuple[Finding, ...]) -> bool:
    return any(
        finding.kind == "H1"
        and finding.decision == "FLAG"
        and finding.evidence.startswith("could-not-analyze:")
        for finding in findings
    )


__all__ = ["CaseResult", "EvalResult", "SandboxViolation", "run_eval"]
