from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from skills._diagnose_common import (
    append_event,
    assert_run_record_conformant,
    read_dispatch_log,
    resolve_evidence_category,
    validate_dispatch_log,
)


ROOT = Path(__file__).resolve().parents[1]
STEER_DIR = ROOT / "skills" / "diagnose-steer"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, STEER_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


diagnose_steer = load_module("diagnose_steer")
dispatch = load_module("dispatch")
steer_validators = load_module("steer_validators")


def make_repo(tmp_path: Path) -> tuple[Path, dict, dict]:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "util.py").write_text("def helper(value):\n    return value + 1\n", encoding="utf-8")
    (repo / "app" / "core.py").write_text(
        "from app.util import helper\n\n"
        "def compute(value):\n"
        "    return helper(value)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_core.py").write_text(
        "from app.core import compute\n\n"
        "def test_compute():\n"
        "    assert compute(1) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    trigger = {
        "failing_test": "tests/test_core.py::test_compute",
        "error_log": 'Traceback\n  File "tests/test_core.py", line 4, in test_compute\n',
    }
    steer = {
        "hypothesis": "look at app/core.py",
        "reason": {"type": "unreconstructable_context", "cited_fact": "incident-7"},
        "resolved_citations": ["incident-7"],
    }
    return repo, trigger, steer


def run_case(tmp_path: Path, *, partial_panel: bool = False, steer_override: dict | None = None):
    repo, trigger, steer = make_repo(tmp_path)
    if steer_override:
        steer.update(steer_override)

    def fake_dispatch(sealed_brief):
        if partial_panel:
            return None
        return {"model": sealed_brief["model"], "reply": f"reply for {sealed_brief['role']}"}

    return diagnose_steer.run_diagnose_steer(
        repo,
        trigger,
        steer,
        tmp_path / "work",
        dispatch=fake_dispatch,
    )


def assert_real_integrated_record(record: dict) -> None:
    assert_run_record_conformant(record)
    log_path = Path(record["dispatch_log_ref"])
    assert log_path.exists()
    events = read_dispatch_log(log_path)
    assert validate_dispatch_log(events) == []
    assert any(event["event_type"] == "trigger_received" for event in events)
    assert any(event["event_type"] == "artifact_visible" for event in events)
    assert any(event["event_type"] == "steer_sent" for event in events)


def validate_corrupted(record: dict) -> list[str]:
    assert_real_integrated_record(record)
    return steer_validators.validate_steer_run_record(record)


def replace_dispatch_log(record: dict, tmp_path: Path, events: list[tuple[str, str, str, str]]) -> None:
    log_path = tmp_path / "corrupted_dispatch_log.jsonl"
    for event_type, seat, channel, artifact in events:
        append_event(log_path, event_type, seat, channel, artifact)
    record["dispatch_log_ref"] = str(log_path)


def replace_predicates(
    record: dict,
    alternatives: list[str],
    *,
    evidence_ref: str = "app/core.py:1",
) -> None:
    observations = record["observables"]
    predicates = []
    for index, alternative in enumerate(alternatives):
        observable = observations[index % len(observations)]["id"]
        predicates.append(
            {
                "predicate_id": f"p{index + 1}",
                "author_seat": "alpha",
                "author_model": "model-alpha",
                "hypothesis_a": "steered fault",
                "hypothesis_b": alternative,
                "observable": observable,
                "under_a": {"op": "eq", "value": "fails-here"},
                "under_b": {"op": "eq", "not_value": "fails-here"},
                "certifier": {
                    "seat": "beta",
                    "model": "model-beta",
                    "evidence_ref": evidence_ref,
                    "derivation_ref": f"derived-from:{observable}",
                    "evidence_category": resolve_evidence_category(evidence_ref),
                },
            }
        )
    record["predicates"] = predicates
    record["confidence"] = steer_validators.compute_confidence(predicates)


def test_s1_steer_in_observation_channel_blocks_and_clean_twin_passes(tmp_path):
    contaminated, _clean_reasons = run_case(tmp_path)
    contaminated["observables"][0]["channel"] = "steer"
    contaminated["observables"][0]["steer"] = contaminated["steer"]["declared_steer"]["hypothesis"]
    reasons = validate_corrupted(contaminated)
    assert "steer-via-observation" in reasons

    clean, clean_reasons = run_case(tmp_path / "clean")
    assert_real_integrated_record(clean)
    assert clean_reasons == []


def test_s2_early_steer_reveal_blocks_and_clean_twin_passes(tmp_path):
    early, _clean_reasons = run_case(tmp_path)
    replace_dispatch_log(
        early,
        tmp_path,
        [
            ("trigger_received", "orchestrator", "trigger", "trigger"),
            ("scope_derived", "diagnose-steer", "extraction", "scope"),
            ("artifact_visible", "scribe", "observation", "observations"),
            ("steer_sent", "orchestrator", "steer", "steer"),
            ("blind_submission_received", "alpha", "blind", "blind-alpha"),
            ("blind_submission_received", "beta", "blind", "blind-beta"),
        ],
    )
    reasons = validate_corrupted(early)
    assert "early-steer-reveal" in reasons

    clean, clean_reasons = run_case(tmp_path / "clean")
    assert_real_integrated_record(clean)
    assert clean_reasons == []


def test_s2_rejects_seat_supplied_prev_hash_and_detects_reorder_forgery(tmp_path):
    with pytest.raises(ValueError, match="prev_hash"):
        dispatch.append_steer_event(tmp_path / "dispatch.jsonl", "steer_sent", "seat", "steer", "x", prev_hash="fake")

    record, reasons = run_case(tmp_path / "run")
    assert_real_integrated_record(record)
    assert reasons == []
    log_path = Path(record["dispatch_log_ref"])
    rows = log_path.read_text(encoding="utf-8").splitlines()
    forged = [rows[0], rows[2], rows[1], *rows[3:]]
    log_path.write_text("\n".join(forged) + "\n", encoding="utf-8")
    assert "dispatch-log-invalid" in steer_validators.validate_steer_run_record(record)


def test_s3_unresolved_citation_and_phase_constants_block_end_to_end(tmp_path):
    bad_steer = {
        "reason": {"type": "unreconstructable_context", "cited_fact": "missing-incident"},
        "resolved_citations": [],
    }
    unresolved, reasons = run_case(tmp_path, steer_override=bad_steer)
    assert_real_integrated_record(unresolved)
    assert "unresolved-citation" in reasons

    phase, _clean_phase_reasons = run_case(tmp_path / "phase")
    phase["phase_input"] = {"P_max": 0.99}
    phase_reasons = validate_corrupted(phase)
    assert "pmax-as-input" in phase_reasons

    clean, clean_reasons = run_case(tmp_path / "clean")
    assert_real_integrated_record(clean)
    assert clean_reasons == []


def test_s3_confidence_both_properties(tmp_path):
    def confidence_of(case_name: str, alternatives: list[str], evidence_ref: str = "app/core.py:1") -> float:
        record, _reasons = run_case(tmp_path / case_name)
        replace_predicates(record, alternatives, evidence_ref=evidence_ref)
        record["converged"] = False
        assert_real_integrated_record(record)
        return record["confidence"]["Q"]

    q_many_weak_same_alt = confidence_of("many-weak-same-alt", ["A"] * 12)
    q_few_weak_same_alt = confidence_of("few-weak-same-alt", ["A"] * 2)
    assert q_many_weak_same_alt == pytest.approx(q_few_weak_same_alt)

    q_one_alt = confidence_of("one-alt", ["A"])
    q_two_alts = confidence_of("two-alts", ["A", "B"])
    assert q_two_alts > q_one_alt

    assert confidence_of("strong-one-alt", ["A"], "live:experiment-1") > q_many_weak_same_alt


def test_s3_inverted_confidence_formula_blocks_end_to_end(tmp_path):
    inverted, _clean_reasons = run_case(tmp_path)
    inverted["confidence"]["confidence"] = inverted["confidence"]["P_max"] - inverted["confidence"]["confidence"]
    reasons = validate_corrupted(inverted)
    assert "inverted-confidence" in reasons

    clean, clean_reasons = run_case(tmp_path / "clean")
    assert_real_integrated_record(clean)
    assert clean_reasons == []


def test_s4_no_discriminating_experiment_blocks_even_when_converged(tmp_path):
    record, _clean_reasons = run_case(tmp_path)
    for predicate in record["predicates"]:
        predicate["certifier"]["evidence_ref"] = "unresolved"
        predicate["certifier"]["evidence_category"] = resolve_evidence_category("unresolved")
    record["confidence"] = steer_validators.compute_confidence(record["predicates"])
    reasons = validate_corrupted(record)
    assert "no-discriminating-experiment" in reasons

    clean, clean_reasons = run_case(tmp_path / "clean")
    assert_real_integrated_record(clean)
    assert clean_reasons == []


def test_live_steer_panel_run_fails_loud_as_harness_only_unverified(tmp_path):
    record, reasons = run_case(tmp_path)
    assert_real_integrated_record(record)
    assert reasons == []
    assert record["panel_executed"] is True
    assert len(record["sealed_briefs"]) == len(record["submissions"])
    assert any(brief["role"] == "steer" for brief in record["sealed_briefs"])
    assert record["verified"] is False
    assert record["harness_only"] is True
    assert record["blocking_real_use"] == "steer-verification-pending"


def test_steer_brief_is_sealed_and_recomputed_from_declared_input(tmp_path):
    record, reasons = run_case(tmp_path)
    assert reasons == []
    assert diagnose_steer.steer_brief_authorship_blocks(record) == []

    steer_brief = next(brief for brief in record["sealed_briefs"] if brief["role"] == "steer")
    steer_brief["brief"]["declared_steer"]["hypothesis"] = "reshaped by orchestrator"
    steer_brief["seal"] = diagnose_steer.seal(steer_brief["brief"])
    assert diagnose_steer.steer_brief_authorship_blocks(record) == ["brief-not-skill-authored"]


def test_partial_steer_panel_never_verifies(tmp_path):
    record, reasons = run_case(tmp_path, partial_panel=True)
    assert record["panel_executed"] is False
    assert record["verified"] is False
    assert record["harness_only"] is True
    assert record["blocking_real_use"] == "incomplete-panel"
    assert reasons == ["incomplete-panel"]


def test_steer_validators_live_in_diagnose_steer_not_common():
    common_source = (ROOT / "skills" / "_diagnose_common" / "neutral_validators.py").read_text(encoding="utf-8")
    steer_source = (STEER_DIR / "steer_validators.py").read_text(encoding="utf-8")
    assert "early-steer-reveal" not in common_source
    assert "early-steer-reveal" in steer_source


def test_diagnose_steer_exposes_no_production_contamination_seam():
    source = (STEER_DIR / "diagnose_steer.py").read_text(encoding="utf-8")
    assert "contamination" not in source
