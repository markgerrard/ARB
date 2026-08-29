from __future__ import annotations

import pytest

from implbench.harness import validate
from implbench.harness.classifier import Classification


def test_validate_adversaries_trip_named_scored_gates() -> None:
    report = validate.run_validate()
    assert report.ok
    assert report.tripped == {
        "null-implementor": "G1",
        "fabricator": "G7",
        "scope-escaper": "G3",
        "test-weakener": "G6",
        "rail-breaker": "G5",
        "discipline-skipper": "G4",
    }


def test_validate_deny_proof_stub_green_goes_red(monkeypatch) -> None:
    monkeypatch.setattr(validate, "classify", lambda value: Classification({f"G{i}": "PASS" for i in range(8)}))
    report = validate.run_validate()
    assert not report.ok
    assert report.stubbed_green


@pytest.mark.parametrize("gate", ["G1", "G3", "G4", "G5", "G6", "G7"])
def test_validate_per_gate_deny_proof(monkeypatch, gate: str) -> None:
    real_classify = validate.classify

    def stub(value):
        result = real_classify(value)
        result[gate] = "PASS"
        return result

    monkeypatch.setattr(validate, "classify", stub)
    report = validate.run_validate(gates_subset=[gate])
    assert not report.ok


def test_scored_validator_rejects_legacy_delivery_labels() -> None:
    outcome = {f"G{i}": "NOT_SCORED" for i in range(8)}
    outcome["G0"] = "PASS"
    outcome["G2"] = "RESCUED"
    with pytest.raises(ValueError):
        validate.validate_scored_outcome(outcome)
