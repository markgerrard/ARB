from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.receipts import ReceiptChain, ReceiptError, make_git_receipt


def _identity() -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z", "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 32, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 1, "schedule_index": 0, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64, "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z", "ended_at": "2026-07-14T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1, "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"} for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")},
    }


def _receipt(fixture: str, parent: str, commit: str, sequence: int, *, paths: list[str] | None = None) -> dict[str, object]:
    return make_git_receipt(
        cell_id="cell-" + "a" * 64, attempt_id="attempt-" + "b" * 32, fixture_root_oid=fixture,
        ordered_parent_oids=[parent], commit_oid=commit, tree_oid="d" * 40,
        changed_paths=paths or ["src/main.py"], tree_digest="e" * 64,
        head_oid=commit, dirty=False, controller_sequence=sequence,
    )


def test_red_receipts_are_authenticated_fsynced_and_first_parent_chained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    import os
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))
    fixture, first, second = "a" * 40, "b" * 40, "c" * 40
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    first_row = chain.append(_receipt(fixture, fixture, first, 1))
    second_row = chain.append(_receipt(fixture, first, second, 2))
    assert first_row["payload"]["ordered_parent_oids"] == [fixture]
    assert second_row["payload"]["ordered_parent_oids"] == [first]
    assert first_row["mac"] and second_row["sequence"] == 2
    assert calls
    assert chain.verify() == 2


def test_red_receipts_reject_allowlist_and_prior_chain_breaks(tmp_path: Path) -> None:
    fixture, first = "a" * 40, "b" * 40
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    chain.append(_receipt(fixture, fixture, first, 1))
    with pytest.raises(ReceiptError):
        chain.append(_receipt(fixture, fixture, "c" * 40, 2, paths=["outside.txt"]))
    with pytest.raises(ReceiptError):
        chain.append(_receipt(fixture, "d" * 40, "c" * 40, 2))


def test_red_budget_candidate_is_service_only_and_controller_sealed(tmp_path: Path) -> None:
    fixture = "a" * 40
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    row = chain.append_budget_candidate({"operation": "stage", "reason": "MODEL_BUDGET_EXCEEDED", "budget_dimension": "stage_bytes", "limit": 10, "observed": 11})
    assert row["record_type"] == "budget"
    assert row["sequence"] == 1 and row["mac"]
    with pytest.raises(ReceiptError):
        chain.append_budget_candidate({"operation": "tool", "reason": "MODEL_BUDGET_EXCEEDED", "budget_dimension": "tool_command_bytes", "limit": 10, "observed": 11})


def test_red_update_ref_compensation_is_an_authenticated_durable_record(tmp_path: Path) -> None:
    fixture, commit = "a" * 40, "b" * 40
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    row = chain.append_infrastructure_failure(
        operation="update-ref", reason="UPDATE_REF_FAILED", parent_oid=fixture, commit_oid=commit
    )
    assert row["record_type"] == "infrastructure-failure"
    assert row["sequence"] == 1 and row["mac"]
    assert chain.verify() == 1


def _g4(commit: str, sequence: int, *, outcome: str = "PASS", nonce: str = "e" * 64) -> dict[str, object]:
    return {
        "cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 32,
        "commit_oid": commit, "public_suite_oid": "d" * 40,
        "public_suite_digest": "e" * 64, "public_suite_digest_version": "public-suite-v1",
        "outcome_enum": outcome, "controller_sequence": sequence, "nonce": nonce,
    }


def test_g4_durable_replay_is_idempotent_only_for_the_identical_authenticated_row(tmp_path: Path) -> None:
    fixture, commit = "a" * 40, "b" * 40
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    chain.append(_receipt(fixture, fixture, commit, 1))
    original = chain.append_g4_receipt(_g4(commit, 1))
    assert chain.append_g4_receipt(_g4(commit, 1)) == original
    assert chain.verify() == 2
    with pytest.raises(ReceiptError, match="replay mismatch"):
        chain.append_g4_receipt(_g4(commit, 1, outcome="FAIL"))
    with pytest.raises(ReceiptError, match="replay mismatch"):
        chain.append_g4_receipt(_g4(commit, 1, nonce="f" * 64))


def test_post_g4_attestation_replay_requires_identical_payload(tmp_path: Path) -> None:
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid="a" * 40, allowed_paths=("src/**",))
    payload = {
        "pre_scorer_attestation_digest": "3" * 64, "g4_receipts_digest": "4" * 64,
    }
    original = chain.append_post_g4_attestation(payload)
    assert chain.append_post_g4_attestation(dict(payload)) == original
    with pytest.raises(ReceiptError, match="replay mismatch"):
        chain.append_post_g4_attestation({**payload, "g4_receipts_digest": "5" * 64})
