from __future__ import annotations

import pytest

from implbench.harness.readiness import (
    GATE_IDS,
    CapabilityContract,
    GateRecord,
    ReadinessController,
    aggregate_readiness,
)


def _record(gate_id: str, status: str = "PASS") -> GateRecord:
    return GateRecord(gate_id, status, "b" * 64, "2026-07-14T10:00:00+00:00", "2026-07-14T10:00:01+00:00")


def test_unknown_tool_class_refuses_capability_contract() -> None:
    with pytest.raises(ValueError, match="unknown capability class"):
        CapabilityContract.from_tool_surface({"mystery": "controller-secret"})


def test_capability_mismatch_refuses_pair_instead_of_widening_one_arm() -> None:
    left = CapabilityContract.from_tool_surface({"read": "read", "shell": "shell"})
    right = CapabilityContract.from_tool_surface({"read": "read", "shell": "shell", "browser": "browser"})

    with pytest.raises(ValueError, match="capability classes do not match"):
        left.match(right)


def test_negative_probe_failure_is_not_a_pass() -> None:
    contract = CapabilityContract.from_tool_surface({"read": "read", "shell": "shell"})

    with pytest.raises(ValueError, match="negative capability probe failed"):
        contract.verify_probes(positive={"read": True, "search": True, "shell": True, "edit": True, "write": True}, negative={"network": True, "browser": False, "memory": False})


def test_aggregate_rejects_duplicate_missing_or_unknown_gate_records() -> None:
    records = [_record(gate_id) for gate_id in GATE_IDS]
    with pytest.raises(ValueError, match="duplicate gate"):
        aggregate_readiness(records + [_record("G1")], clean_controller=True)
    with pytest.raises(ValueError, match="exactly fourteen"):
        aggregate_readiness(records[:-1], clean_controller=True)
    with pytest.raises(ValueError, match="unknown gate"):
        aggregate_readiness(records[:-1] + [_record("G15")], clean_controller=True)


def test_clean_controller_is_an_independent_aggregate_gate() -> None:
    with pytest.raises(ValueError, match="clean controller"):
        aggregate_readiness([_record(gate_id) for gate_id in GATE_IDS], clean_controller=False)


def test_seat_report_cannot_substitute_for_missing_controller_gate_checks() -> None:
    controller = ReadinessController({}, {}, cell_factory=lambda: object(), validate=lambda manifest: None, known_good_calibration=lambda manifest, cell_factory: None)

    result = controller.run(clean_controller=True)

    assert result.status == "UNKNOWN"
    assert all(gate.status == "UNKNOWN" for gate in result.gates[:-1])
