from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from implbench.harness import dispatch
from implbench.harness.classifier import ClassificationInput, classify
from implbench.harness.controller import Controller
from implbench.harness.dispatch import DispatchResult, ScoredDispatchBinding
from implbench.harness.phases import AttemptOutcome, run_pilot
from implbench.harness.schedule import expand_schedule
from attempt_service_fixture import lifecycle as attempt_lifecycle


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-r11-test-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": str(tmp_path / "evidence")},
        "tasks": [{"task_id": f"task-{index}", "fixture_sha": "a" * 40} for index in range(8)],
    }


def _cell() -> object:
    return expand_schedule("00" * 32, [(f"task-{index}", "a" * 40) for index in range(8)])[0]


def _binding() -> ScoredDispatchBinding:
    return ScoredDispatchBinding(
        run_id="oi-pi-bakeoff-r11-test-20260714T000000Z",
        repo=Path("/tmp/implbench-r11-repo"),
        task_for_cell=lambda cell: SimpleNamespace(task_id=cell.task_id, expected_artifacts=("src/result.py",), allowed_paths=("src/*.py",), brief="brief", timeout_s=1),
        seat_for_cell=lambda cell: "seat-a",
        engine_for_cell=lambda cell: "pi-sdk",
        fixture_root_oid_for_cell=lambda cell: "b" * 40,
        tool_gid_for_cell=lambda cell: 49123,
        scored_runtime_factory=lambda **kwargs: None,
        lifecycle_for_cell=lambda cell, attempt_id=None: attempt_lifecycle(Path("/tmp")),
        dispatch_fn=lambda *args, **kwargs: DispatchResult("ok", completion={"classification": {"G0": "PASS", "G2": "not-delivered"}}),
    )


def test_pilot_uses_the_same_typed_scored_binding_as_full_matrix(tmp_path: Path) -> None:
    binding = _binding()
    result = run_pilot(
        _manifest(tmp_path),
        runtime=SimpleNamespace(
            scored_dispatch=binding,
            append_attempt=lambda outcome: None,
            manifest_bytes=b"manifest",
            config_bytes=b"config",
            refs=(),
            journal_tail=b"journal",
        ),
    )

    assert len(result.outcomes) == 32
    assert all(outcome.status == "FAIL" and not outcome.infrastructure for outcome in result.outcomes)


def test_authenticated_model_non_delivery_is_fail_not_infrastructure(tmp_path: Path) -> None:
    context = {
        "dispatch_status": "ok",
        "receipts": ("a" * 40,),
        "imported_oids": ("a" * 40,),
        "dirty": False,
        "seal_complete": True,
        "receipts_authenticated": True,
        "imported_graph_attested": True,
        "infrastructure_failure": None,
    }
    runtime = SimpleNamespace(
        verify_delivery=lambda: SimpleNamespace(decision="not-delivered"),
        import_and_score=lambda: (_ for _ in ()).throw(AssertionError("model non-delivery entered import")),
    )
    result = Controller(tmp_path / "close.ndjson", runtime=runtime, close_context=context).close()

    assert result.classification["G0"] == "PASS"
    assert result.classification["G2"] == "not-delivered"
    assert result.classification["G1"] == "NOT_SCORED"


def test_completion_verification_failure_is_infrastructure_unknown(tmp_path: Path) -> None:
    context = {
        "dispatch_status": "ok",
        "receipts": ("a" * 40,),
        "imported_oids": ("a" * 40,),
        "dirty": False,
        "seal_complete": True,
        "receipts_authenticated": True,
        "imported_graph_attested": True,
        "infrastructure_failure": None,
    }
    runtime = SimpleNamespace(
        verify_delivery=lambda: (_ for _ in ()).throw(RuntimeError("bad verifier")),
        import_and_score=lambda: (_ for _ in ()).throw(AssertionError("verification failure entered import")),
    )
    result = Controller(tmp_path / "close.ndjson", runtime=runtime, close_context=context).close()

    assert all(value == "UNKNOWN" for value in result.classification.values())


def test_scored_argv_uses_controller_cell_root_without_a_bridge_worktree() -> None:
    cells = expand_schedule("00" * 32, [(f"task-{index}", "a" * 40) for index in range(8)])
    first = dispatch._argv(SimpleNamespace(task_id=cells[0].task_id, expected_artifacts=(), allowed_paths=(), brief="", timeout_s=1), "seat-a", "pi-sdk", cells[0].fixture_sha, "oi-pi-bakeoff-r11-test-20260714T000000Z", cell_id=cells[0].cell_id, cell_root=Path("/Users/Shared/arb-implbench/cell-a"))
    second = dispatch._argv(SimpleNamespace(task_id=cells[1].task_id, expected_artifacts=(), allowed_paths=(), brief="", timeout_s=1), "seat-a", "pi-sdk", cells[1].fixture_sha, "oi-pi-bakeoff-r11-test-20260714T000000Z", cell_id=cells[1].cell_id, cell_root=Path("/Users/Shared/arb-implbench/cell-b"))

    assert "--worktree" not in first and "--worktree" not in second
    assert first[first.index("--cell-root") + 1] != second[second.index("--cell-root") + 1]


def test_classifier_accepts_explicit_model_non_delivery_without_infrastructure() -> None:
    result = classify(ClassificationInput(model_non_delivery=True, dispatch_status="ok"))

    assert result == {"G0": "PASS", "G2": "not-delivered", "G1": "NOT_SCORED", "G3": "NOT_SCORED", "G4": "NOT_SCORED", "G5": "NOT_SCORED", "G6": "NOT_SCORED", "G7": "NOT_SCORED"}
