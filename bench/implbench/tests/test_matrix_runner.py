from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.phases import AttemptOutcome, PilotPhase
from implbench.harness.runner import MatrixRunner, RunnerError


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": str(tmp_path / "evidence")},
        "tasks": [{"task_id": f"task-{i}", "fixture_sha": "a" * 40} for i in range(8)],
    }


def _pilot(tmp_path: Path):
    return PilotPhase(
        _manifest(tmp_path),
        execute=lambda cell, attempt_id: AttemptOutcome(cell.cell_id, attempt_id, "PASS", infrastructure=False),
        append_attempt=lambda outcome: None,
        manifest_bytes=b"m",
        config_bytes=b"c",
        refs=(),
        journal_tail=b"j",
    ).run()


def test_full_matrix_requires_unchanged_pilot_seal_and_runs_one_cell_at_a_time(tmp_path: Path) -> None:
    pilot = _pilot(tmp_path)
    running = 0
    max_running = 0
    indices: list[int] = []

    def execute(cell, attempt_id):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        indices.append(cell.schedule_index)
        running -= 1
        return AttemptOutcome(cell.cell_id, attempt_id, "PASS", infrastructure=False)

    result = MatrixRunner(_manifest(tmp_path), pilot_seal=pilot.seal, execute=execute).run()
    assert max_running == 1
    assert indices == list(range(32, 128))
    assert len(result.outcomes) == 128
    assert len({outcome.cell_id for outcome in result.outcomes}) == 128


def test_full_matrix_rejects_changed_pilot_seal_before_dispatch(tmp_path: Path) -> None:
    pilot = _pilot(tmp_path)
    called = False

    def execute(cell, attempt_id):
        nonlocal called
        called = True
        return AttemptOutcome(cell.cell_id, attempt_id, "PASS", infrastructure=False)

    changed = pilot.seal.__class__(pilot.seal.digest[:-1] + ("0" if pilot.seal.digest[-1] != "0" else "1"), pilot.seal.final_index_present)
    with pytest.raises(RunnerError, match="pilot seal"):
        MatrixRunner(_manifest(tmp_path), pilot_seal=changed, execute=execute).run()
    assert called is False


def test_pair_stop_prevents_quiet_retry_and_later_pair_dispatch(tmp_path: Path) -> None:
    pilot = _pilot(tmp_path)
    seen_pairs: list[str] = []

    def execute(cell, attempt_id):
        seen_pairs.append(cell.pair)
        return AttemptOutcome(cell.cell_id, attempt_id, "UNKNOWN", infrastructure=True, cause="bridge")

    result = MatrixRunner(_manifest(tmp_path), pilot_seal=pilot.seal, execute=execute, max_same_cause_failures=1).run()
    assert len(result.outcomes) == 34
    assert seen_pairs == ["Kimi", "GLM"]
    assert result.stopped_pairs == frozenset({"GLM", "Kimi"})
