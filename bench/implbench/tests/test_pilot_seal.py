from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.phases import (
    AttemptOutcome,
    PilotError,
    PilotPhase,
    pilot_seal_digest,
)


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": str(tmp_path / "evidence")},
        "tasks": [{"task_id": f"task-{i}", "fixture_sha": "a" * 40} for i in range(8)],
    }


def test_pilot_runs_repetition_one_in_frozen_order_and_seals_without_final_index(tmp_path: Path) -> None:
    attempts: list[tuple[int, str]] = []
    appended: list[AttemptOutcome] = []

    def execute(cell, attempt_id):
        attempts.append((cell.schedule_index, attempt_id))
        return AttemptOutcome(cell.cell_id, attempt_id, "MODEL_FAIL", infrastructure=False)

    phase = PilotPhase(
        _manifest(tmp_path),
        execute=execute,
        append_attempt=appended.append,
        manifest_bytes=b"manifest",
        config_bytes=b"config",
        refs=[("refs/implbench/runs/x", "a" * 40)],
        journal_tail=b"journal",
    )
    result = phase.run()

    assert [index for index, _ in attempts] == list(range(32))
    assert len(appended) == 32
    assert result.seal.digest == pilot_seal_digest(b"manifest", b"config", [("refs/implbench/runs/x", "a" * 40)], b"journal")
    assert result.seal.final_index_present is False


def test_pilot_reruns_only_infrastructure_unknown_with_new_attempt_id(tmp_path: Path) -> None:
    seen: list[str] = []

    def execute(cell, attempt_id):
        seen.append(attempt_id)
        if len(seen) == 1:
            return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", infrastructure=True, cause="provider-timeout")
        return AttemptOutcome(cell.cell_id, attempt_id, "PASS", infrastructure=False)

    appended: list[AttemptOutcome] = []
    result = PilotPhase(
        _manifest(tmp_path),
        execute=execute,
        append_attempt=appended.append,
        manifest_bytes=b"m",
        config_bytes=b"c",
        refs=(),
        journal_tail=b"j",
    ).run()
    assert seen[0] != seen[1]
    assert len(appended) == 33
    assert result.outcomes[0].status == "UNKNOWN"
    assert result.outcomes[1].status == "PASS"


def test_pilot_does_not_replace_model_outcomes_or_create_final_refs(tmp_path: Path) -> None:
    def execute(cell, attempt_id):
        return AttemptOutcome(cell.cell_id, attempt_id, "FAIL", infrastructure=False)

    with pytest.raises(PilotError, match="final refs"):
        PilotPhase(
            _manifest(tmp_path),
            execute=execute,
            append_attempt=lambda outcome: None,
            manifest_bytes=b"m",
            config_bytes=b"c",
            refs=(("refs/implbench/results/x", "b" * 40),),
            journal_tail=b"j",
            final_index_present=True,
        ).run()
