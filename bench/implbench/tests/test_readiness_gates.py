from __future__ import annotations

from datetime import datetime, timezone

import pytest

from implbench.harness.readiness import (
    GATE_IDS,
    CapabilityContract,
    ControlAcknowledgement,
    GateRecord,
    ReadinessController,
    aggregate_readiness,
    production_gate_checks,
    run_gate14,
)


def _record(gate_id: str, status: str = "PASS") -> GateRecord:
    return GateRecord(
        gate_id=gate_id,
        status=status,
        evidence_digest="a" * 64,
        started_at="2026-07-14T10:00:00+00:00",
        ended_at="2026-07-14T10:00:01+00:00",
    )


def test_gate_record_schema_and_aggregate_require_exactly_fourteen_passes() -> None:
    assert GATE_IDS == tuple(f"G{i}" for i in range(1, 15))
    records = [_record(gate_id) for gate_id in GATE_IDS]

    aggregate = aggregate_readiness(records, clean_controller=True)

    assert aggregate.status == "PASS"
    assert [record.to_dict() for record in aggregate.gates] == [record.to_dict() for record in records]
    assert set(aggregate.to_dict()) == {"status", "gates", "clean_controller"}


def test_gate_record_rejects_noncanonical_status_digest_and_time_order() -> None:
    with pytest.raises(ValueError):
        _record("G1", "SKIPPED")
    with pytest.raises(ValueError):
        GateRecord("G1", "PASS", "not-a-digest", "2026-07-14T10:00:00+00:00", "2026-07-14T10:00:01+00:00")
    with pytest.raises(ValueError):
        GateRecord("G1", "PASS", "a" * 64, "2026-07-14T10:00:02+00:00", "2026-07-14T10:00:01+00:00")


def test_capability_contract_maps_surfaces_and_requires_positive_and_negative_probes() -> None:
    pi = CapabilityContract.from_tool_surface(
        {"read_file": "read", "grep": "search", "shell": "shell", "edit_file": "edit", "write_file": "write"}
    )
    oi = CapabilityContract.from_tool_surface(
        {"cat": "read", "ripgrep": "search", "terminal": "shell", "patch": "edit", "save": "write"}
    )

    matched = pi.match(oi)

    assert matched.classes == frozenset({"read", "search", "shell", "edit", "write"})
    assert matched.positive_probes == frozenset({"read", "search", "shell", "edit", "write"})
    assert matched.negative_probes == frozenset({"network", "browser", "memory"})
    assert pi.surface_digest != oi.surface_digest


def test_control_acknowledgement_uses_independent_runtime_evidence() -> None:
    ack = ControlAcknowledgement("reasoning", "medium", "medium", "provider-runtime-ack")

    assert ack.validate() is None
    with pytest.raises(ValueError):
        ControlAcknowledgement("reasoning", "medium", "medium", "request-echo").validate()
    with pytest.raises(ValueError):
        ControlAcknowledgement("reasoning", "medium", "UNKNOWN", "provider-runtime-ack").validate()


def test_gate14_validates_before_known_good_calibration() -> None:
    events: list[str] = []

    def validate(manifest: object) -> None:
        events.append("validate")

    def calibration(manifest: object, cell_factory: object) -> None:
        events.append("calibrate")

    record = run_gate14({}, lambda: object(), validate=validate, known_good_calibration=calibration)

    assert record.status == "PASS"
    assert events == ["validate", "calibrate"]


def test_controller_executes_each_of_the_fourteen_gate_slots_once() -> None:
    calls: list[str] = []
    checks = {
        gate_id: (lambda manifest, gate_id=gate_id: (calls.append(gate_id), _record(gate_id))[1])
        for gate_id in GATE_IDS[:-1]
    }
    aggregate = ReadinessController(
        {},
        checks,
        cell_factory=lambda: object(),
        validate=lambda manifest: calls.append("validate"),
        known_good_calibration=lambda manifest, cell_factory: calls.append("calibrate"),
    ).run(clean_controller=True)

    assert aggregate.status == "PASS"
    assert calls == list(GATE_IDS[:-1]) + ["validate", "calibrate"]


def test_production_gate_registration_is_complete_and_not_static_green() -> None:
    checks = production_gate_checks({})

    assert set(checks) == set(GATE_IDS)
    assert all(callable(check) for check in checks.values())
    result = ReadinessController({}, checks).run(clean_controller=True)
    assert result.status != "PASS"
