from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness import cli
from implbench.harness.cell_runtime import attempt_id_for
from implbench.harness.dispatch import DispatchResult, ScoredDispatchBinding, run_task
from implbench.harness.phases import AttemptOutcome, PilotPhase
from implbench.harness.runner import RunnerError, run_full_matrix
from implbench.harness.runtime import ProductionRuntimeUnavailable, build_production_controller, build_production_runtime
from implbench.harness.schedule import expand_schedule


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-r10-fix-20260714T000000Z",
        "source": {"realpath": str(tmp_path)},
        "seed": "00" * 32,
        "tasks": [{"task_id": f"task-{index}", "fixture_sha": "a" * 40} for index in range(8)],
    }


def _controller() -> SimpleNamespace:
    callback = lambda *args, **kwargs: None
    return SimpleNamespace(
        gate_checks={f"G{index}": callback for index in range(1, 15)},
        cell_factory=callback,
        validate=callback,
        known_good_calibration=callback,
        hermetic_suite=callback,
        adversarial_validation=callback,
        known_good=callback,
        unscored=callback,
        execute=callback,
        append_attempt=callback,
        pilot_seal=object(),
        close_cell=callback,
        freeze_final=callback,
        manifest_bytes=b"manifest",
        config_bytes=b"config",
        refs=(),
        journal_tail=b"journal",
        task_for_cell=lambda cell: SimpleNamespace(task_id=cell.task_id, expected_artifacts=("src/result.py",), allowed_paths=("src/*.py",), brief="brief", timeout_s=1),
        seat_for_cell=lambda cell: "seat-a",
        engine_for_cell=lambda cell: "pi-sdk",
        fixture_root_oid_for_cell=lambda cell: "b" * 40,
        tool_gid_for_cell=lambda cell: 49123,
        scored_runtime_factory=lambda **kwargs: SimpleNamespace(import_and_score=callback),
        stop_observation=lambda cell: {},
        repo=Path("/tmp/implbench-r10-repo"),
    )


def test_cli_run_builds_a_typed_scored_binding_from_controller_factories(monkeypatch, tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    controller = _controller()
    seen: list[object] = []
    monkeypatch.setattr(cli, "build_production_controller", lambda value: controller)
    monkeypatch.setattr(cli, "run_full_matrix", lambda value, *, runtime: seen.append(runtime) or object())

    cli._run_handler(manifest)

    runtime = seen[0]
    assert isinstance(runtime.scored_dispatch, ScoredDispatchBinding)
    assert runtime.scored_dispatch.task_for_cell is controller.task_for_cell
    assert runtime.scored_dispatch.fixture_root_oid_for_cell is controller.fixture_root_oid_for_cell


def test_production_runtime_requires_all_controller_dispatch_factories(tmp_path: Path) -> None:
    controller = _controller()
    del controller.task_for_cell
    with pytest.raises(ProductionRuntimeUnavailable, match="task_for_cell"):
        build_production_runtime(_manifest(tmp_path), controller=controller)


def test_run_full_matrix_rejects_an_untyped_scored_executor() -> None:
    runtime = SimpleNamespace(pilot_seal=object(), scored_dispatch=lambda cell, attempt: None)
    with pytest.raises(RunnerError, match="ScoredDispatchBinding"):
        run_full_matrix({}, runtime=runtime)


def test_unpatched_controller_assembly_binds_real_run_task_and_isolates_cells(tmp_path: Path) -> None:
    repo = Path(__file__).parents[3]
    tasks = [
        {"task_id": task_id, "fixture_sha": "a" * 40}
        for task_id in (
            "c1-permissive-boundary", "c1-token-bucket", "c2-parser", "c3-refactor",
            "c4-rail", "c5-artifact", "c6-scope", "c7-provenance",
        )
    ]
    arms = [
        {"arm": "glm-pi", "engine": "pi-sdk", "agent_prefix": "pi-glm"},
        {"arm": "glm-zcode", "engine": "openinterpreter", "agent_prefix": "oi-glm"},
        {"arm": "kimi-pi", "engine": "pi-sdk", "agent_prefix": "pi-kimi"},
        {"arm": "kimi-cli", "engine": "openinterpreter", "agent_prefix": "oi-kimi"},
    ]
    manifest = {
        "run_id": "oi-pi-bakeoff-r11-assembly-20260714T000000Z",
        "source": {"realpath": str(repo)},
        "seed": "00" * 32,
        "tasks": tasks,
        "arms": arms,
        "evidence": {"root": str(tmp_path / "evidence")},
    }

    with pytest.raises(ProductionRuntimeUnavailable, match="ARB_MEMORY_REDIS_URL"):
        build_production_controller(manifest)


def _binding(*, completion: dict[str, object], status: str = "ok", timed_out: bool = False) -> ScoredDispatchBinding:
    cell = expand_schedule("00" * 32, [(f"task-{index}", "a" * 40) for index in range(8)])[0]
    return ScoredDispatchBinding(
        run_id="oi-pi-bakeoff-r10-fix-20260714T000000Z",
        repo=Path("/tmp/implbench-r10-repo"),
        task_for_cell=lambda value: SimpleNamespace(task_id=value.task_id, expected_artifacts=("src/result.py",), allowed_paths=("src/*.py",), brief="brief", timeout_s=1),
        seat_for_cell=lambda value: "seat-a",
        engine_for_cell=lambda value: "pi-sdk",
        fixture_root_oid_for_cell=lambda value: "b" * 40,
        tool_gid_for_cell=lambda value: 49123,
        scored_runtime_factory=lambda **kwargs: SimpleNamespace(import_and_score=lambda: None),
        dispatch_fn=lambda *args, **kwargs: DispatchResult(status, timed_out=timed_out, completion=completion),
    ), cell


@pytest.mark.parametrize(
    ("classification", "expected_status", "expected_infrastructure"),
    [
        ({"G0": "PASS", "G2": "not-delivered", "G1": "NOT_SCORED"}, "FAIL", False),
        ({"G0": "FAIL", "G2": "not-delivered", "G1": "NOT_SCORED"}, "FAIL", False),
        ({"G0": "UNKNOWN", "G1": "UNKNOWN", "G2": "UNKNOWN"}, "UNKNOWN", True),
    ],
)
def test_scored_binding_preserves_model_failures_and_marks_only_unknown_as_infrastructure(
    classification: dict[str, str], expected_status: str, expected_infrastructure: bool
) -> None:
    binding, cell = _binding(completion={"classification": classification, "infrastructure_failure": "receipt-authentication"})
    outcome = binding(cell, attempt_id_for(cell.cell_id, 1))
    assert outcome.status == expected_status
    assert outcome.infrastructure is expected_infrastructure
    if expected_infrastructure:
        assert outcome.cause == "receipt-authentication"


def test_scored_timeout_is_infrastructure_unknown_with_timeout_cause() -> None:
    binding, cell = _binding(completion={"classification": {"G0": "UNKNOWN"}}, status="timeout", timed_out=True)
    attempt_id = attempt_id_for(cell.cell_id, 1)
    outcome = binding(cell, attempt_id)
    assert outcome == AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", True, cause="timeout")


def test_pilot_does_not_retry_or_stop_on_model_failure() -> None:
    manifest = {
        "seed": "00" * 32,
        "tasks": [{"task_id": f"task-{index}", "fixture_sha": "a" * 40} for index in range(8)],
    }
    calls = 0

    def execute(cell, attempt_id):
        nonlocal calls
        calls += 1
        return AttemptOutcome(cell.cell_id, attempt_id, "FAIL", False)

    result = PilotPhase(
        manifest,
        execute=execute,
        append_attempt=lambda outcome: None,
        manifest_bytes=b"manifest",
        config_bytes=b"config",
        refs=(),
        journal_tail=b"journal",
        max_same_cause_failures=1,
    ).run()
    assert calls == 32
    assert len(result.outcomes) == 32
