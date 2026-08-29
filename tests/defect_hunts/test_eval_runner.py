from __future__ import annotations

import json
from pathlib import Path

from skills.defect_hunts.eval import runner
from skills.defect_hunts.eval.runner import run_eval
from skills.defect_hunts.h1_config_drift import hunt as h1_hunt
from skills.defect_hunts.h2_assumptions import validate_h2_section
from skills.defect_hunts.types import Scenario


FIXTURE_DIR = Path(__file__).parent / "eval"
REPO_ROOT = Path(__file__).parents[2]


def test_real_h1_hunt_passes_discovery_eval():
    result = run_eval(h1_hunt, _load("positives.json"), _load("negatives.json"))

    assert result.passed
    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.validation_scope == "class-seed validation, not broad certification"
    assert {case.case_id for case in result.per_case} == {
        "audit-prefix",
        "arb-memory-prefix-post-fix",
        "bridge-notify-inbox",
        "bridge-pi-thinking-level",
        "h1-alias-trap",
        "h1-diverging-literal-clean",
        "h1-divergence-trap",
        "h1-defaultless-env-trap",
        "h1-env-literal-outside-diff",
        "arb-memory-dsn",
        "arb-memory-redis-url",
    }
    assert "seat-import" not in {case.case_id for case in result.per_case}
    assert "boot-race-redis-per-seat" not in {case.case_id for case in result.per_case}


def test_run_eval_output_is_deterministic():
    positives = _load("positives.json")
    negatives = _load("negatives.json")

    first = run_eval(h1_hunt, positives, negatives)
    second = run_eval(h1_hunt, positives, negatives)

    assert first == second


def test_hanging_detector_times_out_as_failed_case_not_crashed_run(monkeypatch):
    monkeypatch.setattr(runner, "_HUNT_TIMEOUT_SECONDS", 0.5)
    positives = {
        "cases": [
            {
                "id": "hanging-detector",
                "kind": "H1",
                "expected_decision": "FLAG",
                "files": {"pkg/config.py": "VALUE = \"x\"\n"},
                "diff": "diff --git a/pkg/config.py b/pkg/config.py\n+VALUE = \"x\"\n",
            }
        ]
    }
    negatives = {"cases": []}

    result = run_eval(_hang, positives, negatives)

    assert not result.passed
    assert len(result.per_case) == 1
    case = result.per_case[0]
    assert case.case_id == "hanging-detector"
    assert not case.passed
    assert case.actual_decision == "SANDBOX"
    assert "timed out" in (case.sandbox_violation or "")


def test_could_not_analyze_is_not_real_flag_or_false_positive():
    positives = {
        "cases": [
            {
                "id": "indirect-positive",
                "kind": "H1",
                "expected_decision": "FLAG",
                "files": {"pkg/reader.py": "VALUE = load_env()\n"},
                "diff": "diff --git a/pkg/reader.py b/pkg/reader.py\n+VALUE = load_env()\n",
            }
        ]
    }
    negatives = {
        "cases": [
            {
                "id": "indirect-negative",
                "kind": "H1",
                "expected_decision": "CLEAR",
                "files": {"pkg/reader.py": "VALUE = load_env()\n"},
                "diff": "diff --git a/pkg/reader.py b/pkg/reader.py\n+VALUE = load_env()\n",
            }
        ]
    }

    result = run_eval(h1_hunt, positives, negatives)

    by_id = {case.case_id: case for case in result.per_case}
    assert by_id["indirect-positive"].actual_decision == "CLEAR"
    assert by_id["indirect-positive"].could_not_analyze
    assert not by_id["indirect-positive"].passed
    assert by_id["indirect-negative"].actual_decision == "CLEAR"
    assert by_id["indirect-negative"].could_not_analyze
    assert not by_id["indirect-negative"].passed
    assert result.recall == 0.0
    assert result.precision == 1.0


def test_h2_schema_gate_is_enforced_separately_from_detector_discovery(tmp_path):
    anchor = tmp_path / "tests" / "test_import_contract.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_import_contract():\n    pass\n", encoding="utf-8")
    complete = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "violating_run": "PYTHONPATH=. python -m pytest tests/test_import_contract.py",
                "evidence": "tests/test_import_contract.py::test_import_contract",
            }
        ],
    }
    incomplete = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "evidence": "tests/test_import_contract.py::test_import_contract",
            }
        ],
    }

    assert validate_h2_section(complete, repo_root=tmp_path) == (True, "ok")
    ok, reason = validate_h2_section(incomplete, repo_root=tmp_path)
    assert ok is False
    assert "violating_run" in reason


def test_no_llm_or_external_eval_taxonomy_in_runner_source():
    source = (REPO_ROOT / "skills" / "defect_hunts" / "eval" / "runner.py").read_text(
        encoding="utf-8"
    )

    assert "openai" not in source.lower()
    assert "anthropic" not in source.lower()
    assert "arb_eval" not in source


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _hang(_scenario: Scenario):
    import time

    time.sleep(60)
