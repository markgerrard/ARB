from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from skills._diagnose_common import RUN_RECORD_SCHEMA_PATH, assert_run_record_conformant, seal
from skills._diagnose_common.clock import (
    append_event,
    max_seq,
    read_dispatch_log,
    validate_dispatch_log,
)
from skills._diagnose_common.collation import capture_artifact, collate_artifacts
from skills._diagnose_common.neutral_validators import (
    NEUTRAL_BLOCK_REASONS,
    validate_run_record,
)
import skills._diagnose_common.neutral_validators as neutral_validators


STEER_BLOCK_REASONS = {
    "early-steer-reveal",
    "steer-via-observation",
    "unresolved-citation",
    "no-discriminating-experiment",
    "inverted-confidence",
    "pmax-as-input",
}


def minimal_run_record(**overrides):
    record = {
        "trigger": {"failing_test": "tests/test_x.py::test_x", "error_log": "Traceback"},
        "extraction_scope": {"paths": ["pkg/mod.py"], "window": {"start": 1, "end": 2}},
        "observables": [{"id": "pkg/mod.py:1", "path": "pkg/mod.py"}],
        "seats": [
            {"seat": "a", "model": "model-a", "role": "blind"},
            {"seat": "b", "model": "model-b", "role": "alternative"},
            {"seat": "scribe", "model": "model-c", "role": "scribe"},
        ],
        "predicates": [
            {
                "predicate_id": "p1",
                "author_seat": "a",
                "author_model": "model-a",
                "hypothesis_a": "A",
                "hypothesis_b": "B",
                "observable": "pkg/mod.py:1",
                "under_a": {"op": "eq", "value": "X"},
                "under_b": {"op": "eq", "not_value": "X"},
                "certifier": {
                    "seat": "b",
                    "model": "model-b",
                    "evidence_ref": "pkg/mod.py:1",
                    "derivation_ref": "derivation-1",
                    "evidence_category": "file_line",
                },
            }
        ],
        "dispatch_log_ref": "dispatch_log.jsonl",
        "confidence": {},
        "assignment": {"orchestrator_chosen": False},
        "scribe_context": {"orchestrator_context_visible": False},
        "channel_routes": [],
    }
    record.update(overrides)
    return record


def verified_panel_run_record(**overrides):
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    target_ids = {seat["role"]: seat["target_id"] for seat in constants["roster"]}
    observations = [{"id": "pkg/mod.py:1", "path": "pkg/mod.py"}]
    sealed_briefs = [
        {
            "role": "scribe",
            "model": constants["scribe"]["model"],
            "brief": {
                "role": "scribe",
                "observables": observations,
                "task": "describe-observables-only",
                "system_prompt": constants["scribe"]["system_prompt"],
                "window": {"start": 1, "end": 2},
            },
        }
    ]
    for seat in constants["roster"]:
        sealed_briefs.append(
            {
                "role": seat["role"],
                "model": seat["model"],
                "brief": {
                    "role": seat["role"],
                    "observables": observations,
                    "task": "describe neutral observables",
                    "system_prompt": constants["scribe"]["system_prompt"],
                    "window": {"start": 1, "end": 2},
                },
            }
        )
    for brief in sealed_briefs:
        brief["seal"] = seal(brief["brief"])
    submissions = [
        {
            "role": brief["role"],
            "seat": target_ids.get(brief["role"], brief["role"]),
            "model": brief["model"],
            "seal": brief["seal"],
            "bus_reply_ref": f"bus://{brief['role']}",
            "bus_reply_sha256": "a" * 64,
        }
        for brief in sealed_briefs
    ]
    sealed_submissions = sorted(
        [submission for submission in submissions if submission["role"] != "scribe"],
        key=lambda item: (item["seat"], item["role"]),
    )
    post_bodies = [
        {"role": "certifier", "task": constants["certifier"]["rule"], "sealed_submissions": sealed_submissions},
        {"role": "collation", "task": constants["collation_order"], "sealed_submissions": sealed_submissions},
    ]
    post_briefs = [
        {"role": body["role"], "seal": seal(body), "brief": body}
        for body in post_bodies
    ]
    record = minimal_run_record(
        repo_sha="abc123",
        recorded_traceback={
            "reproduced": True,
            "window": {"start": 1, "end": 2},
            "traceback_sha256": "f" * 64,
        },
        sealed_briefs=sealed_briefs,
        submissions=submissions,
        post_briefs=post_briefs,
        panel_executed=True,
        verified=True,
    )
    record.update(overrides)
    return record


def assert_schema_conformant(record):
    assert_run_record_conformant(record)


def test_run_record_schema_has_c0_contract_fields():
    schema = json.loads(RUN_RECORD_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["required"] == [
        "trigger",
        "extraction_scope",
        "observables",
        "seats",
        "predicates",
        "dispatch_log_ref",
        "confidence",
    ]
    assert "assignment" in schema["properties"]
    assert "scribe_context" in schema["properties"]
    assert "channel_routes" in schema["properties"]
    assert "steer" in schema["properties"]
    predicate = schema["definitions"]["predicate"]
    certifier = predicate["properties"]["certifier"]
    assert certifier["properties"]["evidence_category"]["enum"] == [
        "live_data",
        "file_line",
        "none",
    ]
    assert_schema_conformant(minimal_run_record())


def test_top_level_public_api_exports_frozen_surface():
    common = importlib.import_module("skills._diagnose_common")
    expected = {
        "RUN_RECORD_SCHEMA_PATH",
        "append_event",
        "read_dispatch_log",
        "validate_dispatch_log",
        "max_seq",
        "capture_artifact",
        "collate_artifacts",
        "validate_run_record",
        "NEUTRAL_BLOCK_REASONS",
        "resolve_evidence_category",
        "assert_run_record_conformant",
        "canonical_bytes",
        "seal",
    }
    assert set(common.__all__) == expected
    for name in expected:
        assert getattr(common, name)


def test_canonical_bytes_env_invariant():
    from skills._diagnose_common import canonical_bytes, seal

    a = {"path": "pkg/m.py", "obs": ["pkg/m.py:3"], "role": "blind"}
    b = {"role": "blind", "obs": ["pkg/m.py:3"], "path": "pkg/m.py"}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert seal(a) == seal(b)


def test_canonical_rejects_absolute_path():
    from skills._diagnose_common import canonical_bytes

    with pytest.raises(ValueError):
        canonical_bytes({"path": "/Users/<user>/x.py"})


def test_dispatch_log_assigns_common_owned_sequence_and_detects_tampering(tmp_path):
    log_path = tmp_path / "dispatch_log.jsonl"
    first = append_event(log_path, "blind_prompt_sent", "seat-a", "blind", b"prompt")
    second = append_event(log_path, "blind_submission_received", "seat-a", "blind", b"answer")
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert second["dispatch_log_ref"] == str(log_path)
    assert validate_dispatch_log(read_dispatch_log(log_path)) == []

    rows = log_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(rows[1])
    tampered["seq"] = 1
    (tmp_path / "tampered.jsonl").write_text(rows[0] + "\n" + json.dumps(tampered) + "\n", encoding="utf-8")
    assert "duplicate-seq" in validate_dispatch_log(read_dispatch_log(tmp_path / "tampered.jsonl"))


def test_dispatch_log_reorder_forgery_is_detected(tmp_path):
    log_path = tmp_path / "dispatch_log.jsonl"
    append_event(log_path, "blind_prompt_sent", "seat-a", "blind", b"prompt")
    append_event(log_path, "blind_submission_received", "seat-a", "blind", b"answer")
    append_event(log_path, "artifact_visible", "seat-a", "blind", b"artifact")
    rows = log_path.read_text(encoding="utf-8").splitlines()
    forged = [rows[0], rows[2], rows[1]]
    (tmp_path / "reordered.jsonl").write_text("\n".join(forged) + "\n", encoding="utf-8")
    assert "hash-chain" in validate_dispatch_log(read_dispatch_log(tmp_path / "reordered.jsonl"))


def test_append_event_rejects_seat_supplied_sequence(tmp_path):
    with pytest.raises(ValueError, match="seq"):
        append_event(tmp_path / "dispatch_log.jsonl", "steer_sent", "seat-a", "steer", b"x", seq=99)


def test_append_event_preserves_non_sequence_metadata_and_max_seq(tmp_path):
    log_path = tmp_path / "dispatch_log.jsonl"
    append_event(log_path, "blind_prompt_sent", "seat-a", "blind", b"prompt", role="blind", model="m1")
    append_event(log_path, "blind_submission_received", "seat-a", "blind", b"answer", role="blind")
    events = read_dispatch_log(log_path)
    assert events[0]["role"] == "blind"
    assert events[0]["model"] == "m1"
    assert max_seq(events, "blind_submission_received") == 2
    assert max_seq(events, "missing") is None


def test_neutral_validator_reasons_and_basic_blocks():
    assert NEUTRAL_BLOCK_REASONS == {
        "scope-override",
        "blind-assignment-violation",
        "non-exclusive-predicate",
        "reciprocal-certification",
        "scribe-contamination",
        "cross-channel-routing",
        "evidence-category-mismatch",
        "unverified-without-panel",
        "certifier-starved",
    }
    clean = minimal_run_record()
    assert_schema_conformant(clean)
    assert validate_run_record(clean) == []

    non_exclusive = minimal_run_record()
    del non_exclusive["predicates"][0]["under_b"]["not_value"]
    assert validate_run_record(non_exclusive) == ["non-exclusive-predicate"]

    contaminated_scribe = minimal_run_record(scribe_context={"orchestrator_context_visible": True})
    assert_schema_conformant(contaminated_scribe)
    assert validate_run_record(contaminated_scribe) == ["scribe-contamination"]

    cross_channel = minimal_run_record(channel_routes=[{"from": "a", "to": "b", "phase": "pre-submit"}])
    assert_schema_conformant(cross_channel)
    assert validate_run_record(cross_channel) == ["cross-channel-routing"]


def test_schema_conformant_record_can_trigger_each_neutral_block():
    cases = {
        "scope-override": minimal_run_record(observables=[{"id": "other.py:1", "path": "other.py"}]),
        "blind-assignment-violation": minimal_run_record(
            assignment={"orchestrator_chosen": True}
        ),
        "scribe-contamination": minimal_run_record(
            scribe_context={"orchestrator_context_visible": True}
        ),
        "cross-channel-routing": minimal_run_record(
            channel_routes=[{"from": "a", "to": "b", "phase": "pre-submit"}]
        ),
    }
    for expected, record in cases.items():
        assert_schema_conformant(record)
        assert expected in validate_run_record(record)

    reciprocal = minimal_run_record()
    reciprocal["predicates"].extend([
        {
            "predicate_id": "p2",
            "author_seat": "b",
            "author_model": "model-b",
            "hypothesis_a": "B",
            "hypothesis_b": "C",
            "observable": "pkg/mod.py:1",
            "under_a": {"op": "eq", "value": "Y"},
            "under_b": {"op": "eq", "not_value": "Y"},
            "certifier": {
                "seat": "c",
                "model": "model-c",
                "evidence_ref": "pkg/mod.py:1",
                "derivation_ref": "derivation-2",
                "evidence_category": "file_line",
            },
        },
        {
            "predicate_id": "p3",
            "author_seat": "c",
            "author_model": "model-c",
            "hypothesis_a": "C",
            "hypothesis_b": "A",
            "observable": "pkg/mod.py:1",
            "under_a": {"op": "eq", "value": "Z"},
            "under_b": {"op": "eq", "not_value": "Z"},
            "certifier": {
                "seat": "a",
                "model": "model-a",
                "evidence_ref": "pkg/mod.py:1",
                "derivation_ref": "derivation-3",
                "evidence_category": "file_line",
            },
        },
    ])
    assert_schema_conformant(reciprocal)
    assert "reciprocal-certification" in validate_run_record(reciprocal)


def test_reciprocal_certification_blocks():
    record = minimal_run_record()
    record["predicates"].append(
        {
            "predicate_id": "p2",
            "author_seat": "b",
            "author_model": "model-b",
            "hypothesis_a": "B",
            "hypothesis_b": "A",
            "observable": "pkg/mod.py:1",
            "under_a": {"op": "eq", "value": "Y"},
            "under_b": {"op": "eq", "not_value": "Y"},
            "certifier": {
                "seat": "a",
                "model": "model-a",
                "evidence_ref": "pkg/mod.py:1",
                "derivation_ref": "derivation-2",
                "evidence_category": "file_line",
            },
        }
    )
    assert validate_run_record(record) == ["reciprocal-certification"]


def test_empty_scope_blocks_and_certifier_independence_is_labeled_reciprocal():
    empty_scope = minimal_run_record(extraction_scope={"paths": [], "window": {"start": 1, "end": 2}})
    assert validate_run_record(empty_scope) == ["scope-override"]

    self_certifier = minimal_run_record()
    self_certifier["predicates"][0]["certifier"]["seat"] = "a"
    assert validate_run_record(self_certifier) == ["reciprocal-certification"]

    same_model = minimal_run_record()
    same_model["predicates"][0]["certifier"]["model"] = "model-a"
    assert validate_run_record(same_model) == ["reciprocal-certification"]


def test_resolve_evidence_category_branches():
    common = importlib.import_module("skills._diagnose_common")
    assert common.resolve_evidence_category("live:experiment-1") == "live_data"
    assert common.resolve_evidence_category("pkg/mod.py:12") == "file_line"
    assert common.resolve_evidence_category("unresolved") == "none"


def test_run_record_schema_validator_checks_nested_required_fields():
    assert_run_record_conformant(minimal_run_record())

    invalid = minimal_run_record()
    del invalid["predicates"][0]["under_b"]["not_value"]
    with pytest.raises(ValidationError):
        assert_run_record_conformant(invalid)


def test_run_record_schema_carries_recompute_basis_and_requires_it_when_verified():
    record = minimal_run_record(
        repo_sha="abc123",
        recorded_traceback={
            "reproduced": True,
            "window": {"start": 3, "end": 8},
            "traceback_sha256": "f" * 64,
        },
        sealed_briefs=[
            {"role": "blind", "model": "model-a", "seal": "a" * 64, "brief": {"path": "pkg/mod.py"}}
        ],
        submissions=[
            {
                "role": "blind",
                "seat": "seat-a",
                "seal": "a" * 64,
                "bus_reply_ref": "bus://reply/1",
                "bus_reply_sha256": "b" * 64,
            }
        ],
        post_briefs=[
            {"role": "scribe", "seal": "c" * 64, "brief": {"summary": "observed only"}}
        ],
    )
    assert_run_record_conformant(record)

    verified = dict(record)
    verified["verified"] = True
    assert_run_record_conformant(verified)

    missing = minimal_run_record(verified=True)
    with pytest.raises(ValidationError):
        assert_run_record_conformant(missing)


def test_neutral_validator_blocks_reordered_observables_in_common():
    record = minimal_run_record(
        extraction_scope={"paths": ["pkg/a.py", "pkg/b.py"], "window": {"start": 1, "end": 2}},
        observables=[
            {"id": "pkg/b.py:1", "path": "pkg/b.py"},
            {"id": "pkg/a.py:1", "path": "pkg/a.py"},
        ],
    )
    assert_schema_conformant(record)
    assert validate_run_record(record) == ["scope-override"]


def test_neutral_validator_blocks_evidence_category_mismatch():
    mismatch = minimal_run_record()
    mismatch["predicates"][0]["certifier"]["evidence_ref"] = "pkg/mod.py:12"
    mismatch["predicates"][0]["certifier"]["evidence_category"] = "live_data"
    assert_schema_conformant(mismatch)
    assert validate_run_record(mismatch) == ["evidence-category-mismatch"]

    matching = minimal_run_record()
    matching["predicates"][0]["certifier"]["evidence_ref"] = "pkg/mod.py:12"
    matching["predicates"][0]["certifier"]["evidence_category"] = "file_line"
    assert_schema_conformant(matching)
    assert validate_run_record(matching) == []


def test_verified_true_without_panel_evidence_fails_loud():
    record = minimal_run_record(
        repo_sha="abc123",
        recorded_traceback={
            "reproduced": True,
            "window": {"start": 1, "end": 2},
            "traceback_sha256": "f" * 64,
        },
        sealed_briefs=[],
        submissions=[],
        post_briefs=[],
        verified=True,
    )
    assert_schema_conformant(record)
    assert "unverified-without-panel" in validate_run_record(record)


def test_verified_true_with_complete_panel_evidence_passes():
    record = verified_panel_run_record()
    assert_schema_conformant(record)
    assert validate_run_record(record) == []


def test_verified_panel_rejects_submission_seat_that_differs_from_roster_target():
    record = verified_panel_run_record()
    submission = next(item for item in record["submissions"] if item["role"] == "blind")
    submission["seat"] = "not-codex-bridge-dev"
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    sealed_submissions = sorted(
        [dict(item) for item in record["submissions"] if item["role"] != "scribe"],
        key=lambda item: (item["seat"], item["role"]),
    )
    post_bodies = {
        "certifier": {
            "role": "certifier",
            "task": constants["certifier"]["rule"],
            "sealed_submissions": sealed_submissions,
        },
        "collation": {
            "role": "collation",
            "task": constants["collation_order"],
            "sealed_submissions": sealed_submissions,
        },
    }
    for post in record["post_briefs"]:
        post["brief"] = post_bodies[post["role"]]
        post["seal"] = seal(post["brief"])
    assert_schema_conformant(record)
    assert "unverified-without-panel" in validate_run_record(record)


def test_scribe_brief_must_match_committed_template():
    record = verified_panel_run_record()
    record["sealed_briefs"][0]["brief"]["system_prompt"] = "infer likely cause"
    record["sealed_briefs"][0]["seal"] = seal(record["sealed_briefs"][0]["brief"])
    assert_schema_conformant(record)
    assert "unverified-without-panel" in validate_run_record(record)


def test_collation_is_neutral_artifact_capture_only(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("observable\n", encoding="utf-8")
    captured = capture_artifact(artifact)
    assert captured["path"] == str(artifact)
    assert captured["sha256"]
    assert collate_artifacts([captured]) == {"artifacts": [captured]}


def test_c0_neutrality_guard_scans_all_common_modules():
    package_root = Path(neutral_validators.__file__).resolve().parent
    for source_path in package_root.glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert not common_source_has_forbidden_import(source), source_path.name
        assert not common_source_violates_neutrality(source), source_path.name


def common_source_has_forbidden_import(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(import_name_is_forbidden(alias.name) for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if import_name_is_forbidden(module):
                return True
            if any(import_name_is_forbidden(alias.name) for alias in node.names):
                return True
    return False


def import_name_is_forbidden(name: str) -> bool:
    parts = name.split(".")
    return (
        "dispatch" in parts
        or "diagnose-steer" in parts
        or "steer_validators" in parts
    )


def common_source_violates_neutrality(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in STEER_BLOCK_REASONS:
                return True
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "steer":
                return True
        if isinstance(node, ast.Attribute) and node.attr == "steer":
            return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "steer":
                    return True
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "steer":
                    return True
    return False


def test_neutrality_guard_negative_control_detects_get_steer():
    assert common_source_violates_neutrality("def f(run_record):\n    return run_record.get('steer')\n")


def test_neutrality_guard_negative_control_detects_dispatch_import():
    assert common_source_has_forbidden_import("import dispatch\n")
    assert common_source_has_forbidden_import("from x import dispatch\n")
