from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.phases import (
    CalibrationError,
    CalibrationPhase,
    known_good_calibration,
)


def _manifest(tmp_path: Path) -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z",
        "seed": "00" * 32,
        "evidence": {"root": str(tmp_path / "evidence")},
        "tasks": [
            {"task_id": f"task-{index}", "cluster": f"C{(index % 7) + 1}", "fixture_sha": "a" * 40}
            for index in range(8)
        ],
    }


class FakeCalibration:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.scored_refs: list[str] = []

    def hermetic_suite(self, manifest):
        self.events.append("hermetic")
        return {"passed": True}

    def adversarial_validation(self, manifest):
        self.events.append("adversarial")
        return {"passed": True}

    def known_good(self, manifest, cluster, cell_factory):
        self.events.append(f"known-good:{cluster}")
        return {"passed": True, "cluster": cluster}

    def unscored(self, manifest, task, arm, cell_factory):
        self.events.append(f"unscored:{task}:{arm}")
        return {"scored": False, "refs": [], "results": []}


def test_calibration_clears_every_cluster_then_uses_all_four_unscored_paths(tmp_path: Path) -> None:
    fake = FakeCalibration()
    result = CalibrationPhase(
        _manifest(tmp_path),
        lambda: object(),
        hermetic_suite=fake.hermetic_suite,
        adversarial_validation=fake.adversarial_validation,
        known_good=fake.known_good,
        unscored=fake.unscored,
    ).run()

    assert result.accepted is True
    assert result.cleared_clusters == frozenset({f"C{i}" for i in range(1, 8)})
    assert result.scored_refs == ()
    assert result.scored_results == ()
    assert fake.events[:2] == ["hermetic", "adversarial"]
    assert {event.rsplit(":", 1)[-1] for event in fake.events if event.startswith("unscored:")} == {
        "glm-pi",
        "glm-zcode",
        "kimi-pi",
        "kimi-cli",
    }


def test_calibration_refuses_an_uncleared_cluster(tmp_path: Path) -> None:
    fake = FakeCalibration()

    def missing(cluster, manifest, cell_factory):
        return {"passed": cluster != "C7"}

    with pytest.raises(CalibrationError, match="C7"):
        CalibrationPhase(
            _manifest(tmp_path),
            lambda: object(),
            hermetic_suite=fake.hermetic_suite,
            adversarial_validation=fake.adversarial_validation,
            known_good=lambda manifest, cluster, cell_factory: missing(cluster, manifest, cell_factory),
            unscored=fake.unscored,
        ).run()


def test_known_good_callback_is_real_phase_not_a_silent_noop(tmp_path: Path) -> None:
    calls: list[str] = []

    def factory():
        calls.append("cell")
        return object()

    with pytest.raises(CalibrationError, match="calibration callbacks"):
        known_good_calibration(_manifest(tmp_path), factory)
