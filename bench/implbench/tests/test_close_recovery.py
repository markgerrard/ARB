from __future__ import annotations

from pathlib import Path
import hashlib
import hmac

import pytest

from implbench.harness.controller import CLOSE_PHASES, CloseCrash, CloseJournal, Controller, CloseState
from implbench.harness.completion import materialization_digest
from implbench.harness.controller import ScoredCloseRuntime
from implbench.harness.receipts import ReceiptChain, make_git_receipt
from implbench.harness.records import canonical_json_bytes
from implbench.harness.scorer_sandbox import G4ReceiptBinding


def test_red_every_close_phase_is_write_ahead_and_restart_safe(tmp_path: Path) -> None:
    for index, phase in enumerate(CLOSE_PHASES):
        calls: list[str] = []

        def action(name: str = phase) -> None:
            calls.append(name)

        controller = Controller(
            tmp_path / f"{index}.ndjson",
            actions={phase: action},
            crash_before={phase},
            close_context={
                "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": ("a" * 40,),
                "dirty": False, "seal_complete": True, "receipts_authenticated": True,
                "imported_graph_attested": True, "infrastructure_failure": None,
            },
        )
        with pytest.raises(RuntimeError, match="close interrupted"):
            controller.close(terminal="completed")
        controller.recover()
        assert calls.count(phase) == 1
        assert controller.state is CloseState.DESTROYED


def test_red_recovery_resumes_between_phases_with_a_complete_scored_runtime(tmp_path: Path) -> None:
    journal = tmp_path / "between-phases.ndjson"
    CloseJournal(journal).append("STOP_TOOLS", "committed")
    events: list[str] = []
    context = {
        "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": ("a" * 40,),
        "dirty": False, "seal_complete": True, "receipts_authenticated": True,
        "imported_graph_attested": True, "infrastructure_failure": None,
    }

    class Runtime:
        result_context = {
            "imported_oids": ("a" * 40,), "imported_graph_attested": True, "scorer_failure": None,
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": ("FAIL", "PASS"),
        }

        def __getattr__(self, name: str):
            if name == "import_and_score":
                return lambda: events.append("IMPORT_SCORE")
            return lambda name=name: events.append(name)

    result = Controller(journal, runtime=Runtime(), close_context=context).recover()

    assert events[0] == "drain_rpc"
    assert "IMPORT_SCORE" in events
    assert "destroy" in events
    assert result.state is CloseState.DESTROYED
    assert result.phases == CLOSE_PHASES


def test_red_between_phase_recovery_uses_real_scorer_and_restores_g4_tdd_context(tmp_path: Path) -> None:
    journal = tmp_path / "between-phases-real.ndjson"
    CloseJournal(journal).append("STOP_TOOLS", "committed")
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n")
    events: list[str] = []
    receipt_oid = "a" * 40
    def g4_receipt(commit_oid: str, outcome: str, sequence: int) -> dict[str, object]:
        return {
            "cell_id": "cell-" + "b" * 64, "attempt_id": "attempt-" + "c" * 32,
            "commit_oid": commit_oid, "public_suite_oid": "d" * 40,
            "public_suite_digest": "e" * 64, "public_suite_digest_version": "public-suite-v1",
            "outcome_enum": outcome, "controller_sequence": sequence, "nonce": f"{sequence:x}" * 64,
        }

    class Completion:
        decision = "agent-delivered"
        payload = {"completion": "descriptor"}

    class Verifier:
        def verify(self, receipts, status, worktree):
            events.append("verify")
            return Completion()

    runtime = ScoredCloseRuntime(
        completion_verifier=Verifier(),
        descriptor_importer=lambda payload: events.append("import") or {"payload": payload},
        attestation_verifier=lambda imported, completion: {
            "attested": True,
            "object_ids": (receipt_oid,),
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=lambda post_import, attestation: events.append("score") or {
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": (
                g4_receipt("f" * 40, "FAIL", 1),
                g4_receipt("0" * 40, "PASS", 2),
            ),
        },
        receipts=[{"receipt": receipt_oid}],
        status={},
        worktree=materialization,
    )
    context = {
        "dispatch_status": "ok", "receipts": (receipt_oid,), "imported_oids": (),
        "dirty": False, "seal_complete": True, "receipts_authenticated": True,
        "imported_graph_attested": False, "infrastructure_failure": None,
    }
    actions = {phase: (lambda phase=phase: None) for phase in CLOSE_PHASES if phase != "IMPORT_SCORE"}
    result = Controller(journal, runtime=runtime, actions=actions, close_context=context).recover()

    assert events == ["verify", "import", "score"]
    assert result.classification["G4"] == "PASS"
    assert result.classification["G2"] == "agent-delivered"
    assert result.state is CloseState.DESTROYED


@pytest.mark.parametrize(
    ("crash_point", "crash_index", "durable_g4_prefix"),
    (("after_g4_receipt", 1, 1), ("after_g4_receipt", 2, 2),
     ("before_post_g4_attestation", None, 2), ("after_post_g4_attestation", None, 2)),
)
def test_scored_close_crash_recovery_reuses_controller_bound_g4_receipt(
    tmp_path: Path, crash_point: str, crash_index: int | None, durable_g4_prefix: int,
) -> None:
    """Every durable prefix is reused exactly and missing G4 suffixes append in import order."""
    identity = {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z", "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 32, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 1, "schedule_index": 0, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64, "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z", "ended_at": "2026-07-14T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1, "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
                     for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")},
    }
    fixture, commits = "c" * 40, ("d" * 40, "e" * 40)
    key = b"k" * 32
    chain = ReceiptChain(tmp_path / "receipts.ndjson", key, identity=identity, fixture_root_oid=fixture, allowed_paths=("src/**",))
    first_receipt = chain.append(make_git_receipt(
        cell_id=identity["cell_id"], attempt_id=identity["attempt_id"], fixture_root_oid=fixture,
        ordered_parent_oids=[fixture], commit_oid=commits[0], tree_oid="f" * 40, changed_paths=["src/a.py"],
        tree_digest="0" * 64, head_oid=commits[0], dirty=False, controller_sequence=1,
    ))
    second_receipt = chain.append(make_git_receipt(
        cell_id=identity["cell_id"], attempt_id=identity["attempt_id"], fixture_root_oid=fixture,
        ordered_parent_oids=[commits[0]], commit_oid=commits[1], tree_oid="1" * 40, changed_paths=["src/b.py"],
        tree_digest="2" * 64, head_oid=commits[1], dirty=False, controller_sequence=2,
    ))
    receipts = [first_receipt, second_receipt]
    materialization = tmp_path / "materialization"; materialization.mkdir()
    (materialization / "result").write_text("trusted\n")

    class Completion:
        decision = "agent-delivered"
        payload = {"cell_id": identity["cell_id"], "attempt_id": identity["attempt_id"], "receipts": [row["payload"] for row in receipts]}

    class Lifecycle:
        def g4_receipt_bindings(self, completion, attestation):
            bindings = []
            for row in completion["receipts"]:
                unsigned = {
                    "cell_id": identity["cell_id"], "attempt_id": identity["attempt_id"], "commit_oid": row["commit_oid"],
                    "public_suite_oid": "1" * 40, "public_suite_digest": "2" * 64,
                    "public_suite_digest_version": "public-suite-v1", "controller_sequence": row["controller_sequence"],
                }
                binding = G4ReceiptBinding(**unsigned, nonce=hmac.new(key, canonical_json_bytes(unsigned), hashlib.sha256).hexdigest())
                chain.validate_g4_binding(binding)
                bindings.append(binding)
            return tuple(bindings)
        def append_g4_receipt(self, value): chain.append_g4_receipt(value)
        def append_pre_scorer_attestation(self, value): return chain.append_pre_scorer_attestation(value)
        def append_post_g4_attestation(self, value): return chain.append_post_g4_attestation(value)
        def environment_manifest_digest(self): return "3" * 64

    fired: set[str] = set()
    def crash(point: str, index: int | None) -> None:
        if point == crash_point and index == crash_index and point not in fired:
            fired.add(point); raise CloseCrash(f"injected {point}")
    runtime = ScoredCloseRuntime(
        completion_verifier=type("Verifier", (), {"verify": lambda *_: Completion()})(),
        descriptor_importer=lambda _: {"imported": True},
        attestation_verifier=lambda *_: {
            "attested": True, "object_ids": commits, "materialization": materialization,
            "materialization_digest": materialization_digest(materialization), "imported_graph_digest": "4" * 64,
            "public_suite_oid": "1" * 40, "public_suite_digest": "2" * 64,
            "public_suite_digest_version": "public-suite-v1",
        },
        scorer=lambda _, evidence: {
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": tuple({**binding.__dict__, "outcome_enum": outcome}
                                 for binding, outcome in zip(evidence["g4_receipt_bindings"], ("PASS", "FAIL"))),
        }, receipts=receipts, status={}, worktree=materialization, lifecycle=Lifecycle(), crash_injector=crash,
    )
    context = {"dispatch_status": "ok", "receipts": commits, "imported_oids": (), "dirty": False, "seal_complete": True,
               "receipts_authenticated": True, "imported_graph_attested": False, "infrastructure_failure": None}
    actions = {phase: (lambda: None) for phase in CLOSE_PHASES if phase != "IMPORT_SCORE"}
    controller = Controller(tmp_path / "close.ndjson", runtime=runtime, actions=actions, close_context=context, strict_lifecycle=True)
    controller._verify_delivery(); controller._classify("completed", provisional=True)
    assert controller._import_allowed()
    with pytest.raises(RuntimeError, match="injected"):
        controller.close(terminal="completed")
    rows = chain._rows()
    durable = [row["payload"] for row in rows if row["record_type"] == "g4-receipt"]
    assert [row["commit_oid"] for row in durable] == list(commits[:durable_g4_prefix])
    result = controller.recover()
    assert result.state is CloseState.DESTROYED
    rows = chain._rows()
    assert [row["payload"]["commit_oid"] for row in rows if row["record_type"] == "g4-receipt"] == list(commits)
    assert chain.verify() == 6  # two imports, pre-scorer, exact G4 replay/append, post-G4
