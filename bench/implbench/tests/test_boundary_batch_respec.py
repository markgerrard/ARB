from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness.classifier import ClassificationInput, FailureCategory, classify
from implbench.harness.completion import materialization_digest
from implbench.harness.controller import ScoredCloseRuntime
from implbench.harness.receipts import ReceiptChain, ReceiptError


def _identity() -> dict[str, object]:
    controls = {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "runtime"} for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")}
    return {"run_id": "oi-pi-bakeoff-boundary", "cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 32, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser", "repetition": 1, "schedule_index": 0, "fixture_sha": "c" * 40, "model_declared": "m", "model_verified_via": "runtime", "engine_version": "v", "harness_version": "v", "corpus_version": "v", "config_digest": "d" * 64, "capability_manifest_digest": "e" * 64, "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "runtime", "started_at": "s", "ended_at": "e", "wall_time_s": 0, "terminal_status": "completed", "retry_count": 0, "tool_call_count": 0, "schema_version": "record-v2", "prior_record_digest": None, "controls": controls}


def test_failure_categories_are_closed_and_infrastructure_is_unknown() -> None:
    assert set(FailureCategory) == {FailureCategory.NONE, FailureCategory.MODEL_IMPLEMENTATION, FailureCategory.PROTOCOL_IMPORT_INFRASTRUCTURE, FailureCategory.OTHER_INFRASTRUCTURE}
    assert set(classify(ClassificationInput(failure_category=FailureCategory.PROTOCOL_IMPORT_INFRASTRUCTURE)).values()) == {"UNKNOWN"}
    with pytest.raises(ValueError, match="not closed"):
        classify(ClassificationInput(failure_category="provider-timeout"))


def test_pre_scorer_attestation_is_fsynced_and_replay_closed(tmp_path) -> None:
    chain = ReceiptChain(tmp_path / "records.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid="c" * 40, allowed_paths=("*.py",))
    payload = {"environment_manifest_digest": "1" * 64, "completion_digest": "2" * 64, "imported_graph_digest": "3" * 64}
    row = chain.append_pre_scorer_attestation(payload)
    assert row["record_type"] == "pre-scorer-attestation" and chain.verify() == 1
    assert chain.append_pre_scorer_attestation(payload) == row
    with pytest.raises(ReceiptError, match="replay mismatch"):
        chain.append_pre_scorer_attestation({**payload, "completion_digest": hashlib.sha256(b"changed").hexdigest()})


def test_scored_close_rejects_tampered_durable_pre_scorer_reread_before_launch(tmp_path) -> None:
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    completion = SimpleNamespace(decision="agent-delivered", payload={"completion": "sealed"})
    scorer_calls: list[object] = []

    class Lifecycle:
        @staticmethod
        def environment_manifest_digest() -> str:
            return "1" * 64

        @staticmethod
        def append_pre_scorer_attestation(payload):
            return {
                "record_type": "pre-scorer-attestation",
                "payload": {**payload, "completion_digest": "f" * 64},
            }

    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_args: completion),
        descriptor_importer=lambda payload: payload,
        attestation_verifier=lambda _imported, _completion: {
            "attested": True,
            "object_ids": ("a" * 40,),
            "imported_graph_digest": "2" * 64,
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=lambda *args: scorer_calls.append(args),
        receipts=[],
        status={},
        worktree=materialization,
        lifecycle=Lifecycle(),
    )

    with pytest.raises(RuntimeError, match="authenticated reread"):
        runtime.import_and_score()
    assert scorer_calls == []


def test_scored_close_releases_only_durable_pre_scorer_digest_projection(tmp_path) -> None:
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    completion = SimpleNamespace(decision="agent-delivered", payload={"completion": "sealed"})
    released: list[dict[str, object]] = []

    class Lifecycle:
        @staticmethod
        def environment_manifest_digest() -> str:
            return "1" * 64

        @staticmethod
        def append_pre_scorer_attestation(payload):
            return {
                "record_type": "pre-scorer-attestation",
                "payload": dict(payload),
                "auth_tag": "controller-only-record-metadata",
            }

        @staticmethod
        def append_post_g4_attestation(payload):
            return {"record_type": "post-g4-attestation", "payload": dict(payload)}

    def scorer(_post_import, attestation):
        released.append(attestation["pre_scorer_attestation"])
        return {
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS",
            "g6": "PASS", "g7": "PASS", "g4_receipts": (),
        }

    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_args: completion),
        descriptor_importer=lambda payload: payload,
        attestation_verifier=lambda _imported, _completion: {
            "attested": True,
            "object_ids": ("a" * 40,),
            "imported_graph_digest": "2" * 64,
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=scorer,
        receipts=[],
        status={},
        worktree=materialization,
        lifecycle=Lifecycle(),
    )

    runtime.import_and_score()
    assert released == [{
        "environment_manifest_digest": "1" * 64,
        "completion_digest": hashlib.sha256(b'{"completion":"sealed"}').hexdigest(),
        "imported_graph_digest": "2" * 64,
    }]


def test_scored_close_rejects_tampered_post_g4_reread_before_result_release(tmp_path) -> None:
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n", encoding="utf-8")
    completion = SimpleNamespace(decision="agent-delivered", payload={"completion": "sealed"})

    class Lifecycle:
        @staticmethod
        def environment_manifest_digest() -> str:
            return "1" * 64

        @staticmethod
        def append_pre_scorer_attestation(payload):
            return {"record_type": "pre-scorer-attestation", "payload": dict(payload)}

        @staticmethod
        def append_post_g4_attestation(payload):
            return {
                "record_type": "post-g4-attestation",
                "payload": {**payload, "g4_receipts_digest": "f" * 64},
            }

    runtime = ScoredCloseRuntime(
        completion_verifier=SimpleNamespace(verify=lambda *_args: completion),
        descriptor_importer=lambda payload: payload,
        attestation_verifier=lambda _imported, _completion: {
            "attested": True,
            "object_ids": ("a" * 40,),
            "imported_graph_digest": "2" * 64,
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=lambda *_args: {
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS",
            "g6": "PASS", "g7": "PASS", "g4_receipts": (),
        },
        receipts=[],
        status={},
        worktree=materialization,
        lifecycle=Lifecycle(),
    )

    with pytest.raises(RuntimeError, match="post-G4 authenticated reread"):
        runtime.import_and_score()
    assert runtime.result_context == {}


def test_repository_owned_plane_helper_accepts_only_closed_production_protocol(tmp_path) -> None:
    helper = Path(__file__).parents[1] / "plane_helper.py"
    request = {"version": "implbench-plane-v1", "action": "reserve", "run_id": "oi-pi-bakeoff-boundary", "nonce": "a" * 64, "cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 32, "root": str(tmp_path)}
    result = subprocess.run([str(helper)], input=json.dumps(request), capture_output=True, text=True, check=False)
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert set(response) == {"version", "ok", "action", "run_id", "nonce", "cell_id", "attempt_id", "root", "control_uid", "tool_uid", "git_uid", "tool_gid", "processes"}
    assert response["processes"] == []
    request["unexpected"] = True
    assert subprocess.run([str(helper)], input=json.dumps(request), capture_output=True, text=True, check=False).returncode != 0
