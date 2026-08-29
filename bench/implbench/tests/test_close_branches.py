from __future__ import annotations

from implbench.harness.controller import classify_close


def test_red_budget_failure_has_g0_fail_but_empty_submission_is_not_scored() -> None:
    result = classify_close(
        dispatch_status="ok",
        receipts=(),
        dirty=False,
        seal_complete=False,
        budget_authenticated=True,
        budget_operation="commit",
    )
    assert result["G0"] == "FAIL"
    assert result["G2"] == "not-delivered"
    assert result["G4"] == "NOT_SCORED"


def test_red_budget_failure_with_nonempty_incomplete_seal_is_not_scored() -> None:
    result = classify_close(
        dispatch_status="ok",
        receipts=("a" * 40,),
        imported_oids=("a" * 40,),
        dirty=False,
        seal_complete=False,
        budget_authenticated=True,
        budget_fsynced=True,
        budget_operation="commit",
    )
    assert result["G0"] == "FAIL"
    assert result["G2"] == "not-delivered"
    assert all(result[gate] == "NOT_SCORED" for gate in ("G1", "G3", "G4", "G5", "G6", "G7"))


def test_red_infrastructure_failure_wins_over_budget_and_import() -> None:
    result = classify_close(
        dispatch_status="ok",
        receipts=("a" * 40,),
        imported_oids=("a" * 40,),
        dirty=False,
        seal_complete=True,
        budget_authenticated=True,
        budget_operation="commit",
        infrastructure_failure="post-g4-attestation",
    )
    assert set(result.values()) == {"UNKNOWN"}


def test_red_dispatch_timeout_precedence_reaches_final_classification() -> None:
    result = classify_close(
        dispatch_status="ok",
        dispatch_timed_out=True,
        receipts=("a" * 40,),
        imported_oids=("a" * 40,),
        dirty=False,
        seal_complete=True,
        g4_receipts=(
            {"outcome_enum": "FAIL"},
            {"outcome_enum": "PASS"},
        ),
    )
    assert all(value == "UNKNOWN" for value in result.values())
