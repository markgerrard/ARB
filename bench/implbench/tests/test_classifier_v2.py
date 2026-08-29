from __future__ import annotations

import pytest

from implbench.harness.classifier import (
    ClassificationInput,
    classify,
)


def _input(**overrides: object) -> ClassificationInput:
    values: dict[str, object] = {
        "dispatch_status": "ok",
        "receipts": (),
        "imported_oids": (),
        "g1": "PASS",
        "g3": "PASS",
        "g4": "PASS",
        "g5": "PASS",
        "g6": "PASS",
        "g7": "PASS",
    }
    values.update(overrides)
    return ClassificationInput(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("ordinary", {"G0": "PASS", "G2": "not-delivered", "G1": "NOT_SCORED"}),
        ("empty-dirty", {"G0": "FAIL", "G2": "not-delivered", "G3": "NOT_SCORED"}),
        ("budget", {"G0": "FAIL", "G2": "not-delivered", "G4": "NOT_SCORED"}),
        ("auth", {"G0": "UNKNOWN", "G1": "UNKNOWN", "G7": "UNKNOWN"}),
        ("importer", {"G0": "UNKNOWN", "G2": "UNKNOWN", "G6": "UNKNOWN"}),
        ("scorer", {"G0": "UNKNOWN", "G1": "UNKNOWN", "G5": "UNKNOWN"}),
    ],
)
def test_frozen_g0_g7_precedence_table(case: str, expected: dict[str, str]) -> None:
    kwargs: dict[str, object] = {}
    if case == "empty-dirty":
        kwargs.update(dispatch_status="failed", dirty=True)
    elif case == "budget":
        kwargs.update(budget_authenticated=True, budget_operation="tool", dispatch_status="failed")
    elif case == "auth":
        kwargs.update(infrastructure_failure="receipt-authentication")
    elif case == "importer":
        kwargs.update(receipts=("a" * 40,), imported_oids=(), infrastructure_failure="importer")
    elif case == "scorer":
        kwargs.update(receipts=("a" * 40,), imported_oids=("a" * 40,), scorer_failure="supervisor")
    result = classify(_input(**kwargs))
    assert {gate: result[gate] for gate in expected} == expected


def test_nonempty_imported_attempt_is_delivered_and_g4_requires_red_then_green_receipts() -> None:
    result = classify(
        _input(
            receipts=("a" * 40,),
            imported_oids=("a" * 40,),
            g4_receipts=("FAIL", "PASS"),
        )
    )
    assert result["G2"] == "agent-delivered"
    assert result["G4"] == "PASS"


def test_proven_submitted_execution_limit_is_g1_fail_not_infrastructure_unknown() -> None:
    result = classify(
        _input(
            receipts=("a" * 40,),
            imported_oids=("a" * 40,),
            scorer_failure="execution-timeout",
        )
    )
    assert result["G0"] == "PASS"
    assert result["G2"] == "agent-delivered"
    assert result["G1"] == "FAIL"


def test_g4_without_qualifying_receipts_is_not_demonstrated_not_fail() -> None:
    result = classify(
        _input(
            receipts=("a" * 40,),
            imported_oids=("a" * 40,),
            g4_receipts=("PASS",),
        )
    )
    assert result["G4"] == "NOT_DEMONSTRATED"


@pytest.mark.parametrize(
    ("scorer_g4", "receipts", "expected"),
    [
        ("FAIL", ("FAIL", "PASS"), "FAIL"),
        ("UNKNOWN", ("FAIL", "PASS"), "UNKNOWN"),
        ("PASS", ("PASS",), "NOT_DEMONSTRATED"),
        ("FAIL", ("PASS",), "NOT_DEMONSTRATED"),
    ],
)
def test_g4_projected_scorer_verdict_and_tdd_proof_have_explicit_precedence(
    scorer_g4: str, receipts: tuple[str, ...], expected: str
) -> None:
    result = classify(
        _input(
            receipts=("a" * 40,),
            imported_oids=("a" * 40,),
            g4=scorer_g4,
            g4_receipts=receipts,
        )
    )
    assert result["G4"] == expected


def test_legacy_public_g2_labels_are_rejected() -> None:
    with pytest.raises(ValueError):
        classify(_input(public_g2="DELIVERED"))


def test_classifier_rejects_unclosed_downstream_verdicts() -> None:
    with pytest.raises(ValueError):
        classify(_input(g1="invented-verdict"))
