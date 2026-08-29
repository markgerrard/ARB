from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness.controller import CLOSE_PHASES, CloseState, Controller
from implbench.harness.evidence import EvidencePackage, final_ref_index
from implbench.harness.phases import AttemptOutcome, PilotPhase, evaluate_stop_rules
from implbench.harness.report import render
from implbench.harness.runner import MatrixRunner


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-integration-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": "/private/tmp/oi-pi-bakeoff-integration-evidence"},
        "tasks": [
            {"task_id": f"task-{index}", "cluster": f"C{(index % 7) + 1}", "fixture_sha": "a" * 64}
            for index in range(8)
        ],
    }


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _complete_context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "dispatch_status": "ok",
        "receipts": (),
        "imported_oids": (),
        "dirty": False,
        "seal_complete": True,
        "receipts_authenticated": True,
        "imported_graph_attested": True,
        "infrastructure_failure": None,
    }
    context.update(overrides)
    return context


def _pilot(
    manifest: dict[str, object],
    *,
    append_attempt,
) -> object:
    return PilotPhase(
        manifest,
        execute=lambda cell, attempt_id: {"status": "PASS", "infrastructure": False},
        append_attempt=append_attempt,
        manifest_bytes=_manifest_bytes(manifest),
        config_bytes=b"fixed-config",
        refs=(),
        journal_tail=b"fixed-journal",
    ).run()


def _close_known_good(root: Path, outcome: AttemptOutcome) -> None:
    events: list[str] = []
    controller = Controller(
        root / f"{outcome.cell_id}.ndjson",
        actions={phase: lambda phase=phase: events.append(phase) for phase in CLOSE_PHASES},
        close_context=_complete_context(receipts=("a" * 40,), imported_oids=("a" * 40,)),
    )
    result = controller.close(terminal="completed")
    assert result.state is CloseState.DESTROYED
    assert result.classification["G0"] == "PASS"
    assert events == list(CLOSE_PHASES)


def test_known_good_full_matrix_closes_each_cell_and_validates_golden_package(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    package = EvidencePackage.create(tmp_path / "evidence", manifest)
    closed = tmp_path / "closes"
    closed.mkdir()

    def append_attempt(outcome: AttemptOutcome) -> None:
        package.append_public(
            {
                "attempt_id": outcome.attempt_id,
                "cell_id": outcome.cell_id,
                "classification": {"G2": "agent-delivered"},
                "schedule_index": outcome.schedule_index,
                "status": outcome.status,
            }
        )

    def append_pilot_attempt(outcome: AttemptOutcome) -> None:
        append_attempt(outcome)
        _close_known_good(closed, outcome)

    pilot = _pilot(manifest, append_attempt=append_pilot_attempt)
    matrix = MatrixRunner(
        manifest,
        pilot_seal=pilot.seal,
        execute=lambda cell, attempt_id: AttemptOutcome(
            cell.cell_id,
            attempt_id,
            "PASS",
            infrastructure=False,
        ),
        append_attempt=append_attempt,
        close_cell=lambda outcome: _close_known_good(closed, outcome),
    ).run()

    assert matrix.complete is True
    assert matrix.stopped_pairs == frozenset()
    assert len(matrix.outcomes) == 128
    assert {outcome.schedule_index for outcome in matrix.outcomes} == set(range(128))
    assert len(package.root.joinpath("cells.ndjson").read_bytes().splitlines()) == 128

    package.seal([("refs/implbench/runs/integration", "b" * 40)])
    package.validate(require_sealed=True)
    expected_index = final_ref_index(package.manifest_digest, package.journal_tail_digest, [("refs/implbench/runs/integration", "b" * 40)])
    assert json.loads((package.root / "git-refs.txt").read_text(encoding="utf-8")) == expected_index
    assert json.loads(render(package.root))["schema"] == "pair-analysis-v1"


@pytest.mark.parametrize(
    ("name", "terminal", "context", "expected_g0", "expected_g2"),
    [
        ("empty", "completed", _complete_context(), "PASS", "not-delivered"),
        ("dirty-empty", "completed", _complete_context(dirty=True), "PASS", "not-delivered"),
        ("dispatch-failed", "dispatch-failed", _complete_context(), "FAIL", "not-delivered"),
        ("timeout", "timeout", _complete_context(), "UNKNOWN", "UNKNOWN"),
        (
            "budget-infrastructure",
            "completed",
            _complete_context(
                seal_complete=False,
                budget_authenticated=True,
                budget_operation="commit",
                infrastructure_failure="auth",
            ),
            "UNKNOWN",
            "UNKNOWN",
        ),
    ],
)
def test_failure_paths_close_once_without_importing_empty_submission(
    tmp_path: Path,
    name: str,
    terminal: str,
    context: dict[str, object],
    expected_g0: str,
    expected_g2: str,
) -> None:
    events: list[str] = []
    controller = Controller(
        tmp_path / f"{name}.ndjson",
        actions={"IMPORT_SCORE": lambda: events.append("IMPORT_SCORE")},
        close_context=context,
    )
    result = controller.close(terminal=terminal)

    assert result.state is CloseState.DESTROYED
    assert result.classification["G0"] == expected_g0
    assert result.classification["G2"] == expected_g2
    assert events == []
    assert controller.close(terminal=terminal) == result


def test_delivered_path_imports_once_and_then_closes(tmp_path: Path) -> None:
    events: list[str] = []
    controller = Controller(
        tmp_path / "delivered.ndjson",
        actions={"IMPORT_SCORE": lambda: events.append("IMPORT_SCORE")},
        close_context={
            "receipts": ("a" * 40,),
            "imported_oids": ("a" * 40,),
            "dirty": False,
            "seal_complete": True,
            "receipts_authenticated": True,
            "imported_graph_attested": True,
            "infrastructure_failure": None,
            "dispatch_status": "ok",
        },
    )
    result = controller.close()

    assert result.state is CloseState.DESTROYED
    assert result.classification["G2"] == "agent-delivered"
    assert events == ["IMPORT_SCORE"]
    assert controller.close() == result


@pytest.mark.parametrize("phase", CLOSE_PHASES)
def test_restart_after_every_close_phase_runs_only_the_uncommitted_action(tmp_path: Path, phase: str) -> None:
    calls: list[str] = []
    crashed = {"value": False}

    def action() -> None:
        calls.append(phase)
        if not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("side effect interrupted")

    controller = Controller(
        tmp_path / f"{phase}.ndjson",
        actions={phase: action},
        recovery_actions={phase: lambda: calls.append(f"probe:{phase}")},
        close_context=_complete_context(receipts=("a" * 40,), imported_oids=("a" * 40,)),
    )
    with pytest.raises(RuntimeError, match="side effect interrupted"):
        controller.close()

    result = controller.recover()

    assert result.state is CloseState.DESTROYED
    assert calls.count(phase) == 1
    assert calls.count(f"probe:{phase}") == 1
    assert result.phases == CLOSE_PHASES


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wrong_pin", True),
        ("context_reuse", True),
        ("hidden_key_exposure", True),
        ("fixture_sha_mismatch", True),
        ("write_outside_worktree", True),
        ("source_drift", True),
        ("malformed_ndjson", True),
        ("discarded_provider_error", True),
        ("unknown_reasoning", True),
        ("infrastructure_failures", ["bridge", "bridge", "bridge"]),
    ],
)
def test_every_stop_rule_is_evaluated_before_dispatch(field: str, value: object) -> None:
    assert evaluate_stop_rules({field: value})


def test_matrix_stop_rule_halts_the_pair_without_quiet_retry(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    pilot = _pilot(manifest, append_attempt=lambda outcome: None)
    dispatched: list[int] = []
    result = MatrixRunner(
        manifest,
        pilot_seal=pilot.seal,
        execute=lambda cell, attempt_id: dispatched.append(cell.schedule_index) or {"status": "PASS"},
        stop_observation=lambda cell: {"wrong_pin": True, "pair": cell.pair},
        max_same_cause_failures=1,
    ).run()

    assert dispatched == []
    assert result.stopped_pairs == frozenset({"GLM", "Kimi"})
    assert len(result.outcomes) == 32
