from __future__ import annotations

import json
from pathlib import Path

import pytest

from implbench.harness.records import (
    RecordError,
    canonical_json_bytes,
    make_identity,
    public_projection,
    validate_record,
)


def _identity(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z",
        "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 32,
        "pair": "GLM",
        "arm": "glm-pi",
        "task": "c1-parser",
        "repetition": 0,
        "schedule_index": 0,
        "fixture_sha": "f" * 64,
        "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack",
        "engine_version": "pi-sdk-v1",
        "harness_version": "harness-v1",
        "corpus_version": "implbench-corpus-v1",
        "config_digest": "1" * 64,
        "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium",
        "reasoning_effective": "medium",
        "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z",
        "ended_at": "2026-07-14T00:00:01Z",
        "wall_time_s": 1,
        "terminal_status": "completed",
        "retry_count": 0,
        "tool_call_count": 0,
        "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {
            name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
            for name in (
                "temperature", "top_p", "top_k", "seed", "penalties", "maximum_output",
                "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts",
            )
        },
    }
    value.update(overrides)
    return value


def test_canonical_json_rejects_duplicates_numbers_and_trailing_bytes() -> None:
    assert canonical_json_bytes({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'
    for raw in (b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":1} trailing'):
        with pytest.raises(RecordError):
            from implbench.harness.records import parse_canonical_json

            parse_canonical_json(raw)


def test_identity_envelope_is_mandatory_and_closed() -> None:
    record = make_identity(_identity(), record_type="telemetry", payload={"event": "turn-start", "value": 1})
    assert validate_record(record)["schema_version"] == "record-v2"
    for field in ("cell_id", "attempt_id", "prior_record_digest", "controls"):
        broken = dict(record)
        broken.pop(field)
        with pytest.raises(RecordError):
            validate_record(broken)
    broken = dict(record)
    broken["unexpected"] = True
    with pytest.raises(RecordError):
        validate_record(broken)


def test_closed_record_schemas_cover_receipts_budget_g4_completion_attestation_census_gate_telemetry_provenance() -> None:
    base = _identity()
    receipt = {
        "cell_id": base["cell_id"], "attempt_id": base["attempt_id"], "fixture_root_oid": "a" * 40,
        "ordered_parent_oids": ["b" * 40], "commit_oid": "c" * 40, "tree_oid": "d" * 40,
        "changed_paths": ["src/app.py"], "tree_digest": "e" * 64, "tree_digest_version": "final-tree-v1",
        "head_oid": "c" * 40, "dirty": False, "controller_sequence": 1, "nonce": "n" * 64,
    }
    records = {
        "git-receipt": {**receipt, "nonce": "e" * 64},
        "budget": {"operation": "status", "reason": "MODEL_BUDGET_EXCEEDED", "budget_dimension": "wall_time_s", "limit": 1, "observed": 2},
        "g4-receipt": {"cell_id": base["cell_id"], "attempt_id": base["attempt_id"], "commit_oid": "c" * 40, "public_suite_oid": "d" * 40, "public_suite_digest": "e" * 64, "public_suite_digest_version": "public-suite-v1", "outcome_enum": "PASS", "controller_sequence": 1, "nonce": "e" * 64},
        "completion": {"cell_id": base["cell_id"], "attempt_id": base["attempt_id"], "fixture_root": "a" * 40, "receipts": [{**receipt, "nonce": "e" * 64}], "head": "c" * 40, "dirty": False, "final_tree_digest": "e" * 64, "final_tree_digest_version": "final-tree-v1"},
        "post-g4-attestation": {"pre_scorer_attestation_digest": "3" * 64, "g4_receipts_digest": "4" * 64},
        "census-private": {"phase": "cell", "gate_id": "G9", "expected_ref_digest": "1" * 64, "observed_ref_digest": "2" * 64, "expected_object_digest": "3" * 64, "observed_object_digest": "4" * 64, "expected_ref_count": 2, "observed_ref_count": 2, "expected_object_count": 3, "observed_object_count": 3, "violation": "EXTRA_REF"},
        "gate": {"gate_id": "G0", "status": "PASS", "evidence_digest": "1" * 64, "started_at": base["started_at"], "ended_at": base["ended_at"]},
        "telemetry": {"event": "turn-start", "value": 1},
        "provenance": {"model_declared": "glm-5.2", "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1", "corpus_version": "implbench-corpus-v1"},
        "unavailable": {"status": "UNAVAILABLE", "reason": "provider-timeout", "diagnostic_digest": "5" * 64},
    }
    for kind, payload in records.items():
        record = make_identity(base, record_type=kind, payload=payload)
        assert validate_record(record)["record_type"] == kind
    with pytest.raises(RecordError):
        validate_record(make_identity(base, record_type="budget", payload={**records["budget"], "operation": "shell"}))
    with pytest.raises(RecordError):
        validate_record(make_identity(base, record_type="unavailable", payload={**records["unavailable"], "reason": "raw provider text"}))


def test_public_projection_is_value_free_and_bounded() -> None:
    record = make_identity(_identity(), record_type="telemetry", payload={"event": "turn-start", "value": 1})
    projected = public_projection(record)
    assert "model_declared" not in projected
    assert projected["record_type"] == "telemetry"
    assert projected["value"] == 1
    json.dumps(projected)
