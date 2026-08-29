from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.classifier import ClassificationInput, classify
from implbench.harness.controller import classify_close
from implbench.harness.gates import parse_junit


def test_scored_classifier_has_closed_empty_and_delivered_branches() -> None:
    empty = classify(ClassificationInput(receipts=(), imported_oids=()))
    delivered = classify(ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,)))
    assert empty["G2"] == "not-delivered"
    assert empty["G1"] == "NOT_SCORED"
    assert delivered["G2"] == "agent-delivered"


def test_scored_classifier_infrastructure_precedes_budget() -> None:
    result = classify_close(
        dispatch_status="ok",
        receipts=("a" * 40,),
        imported_oids=("a" * 40,),
        dirty=False,
        seal_complete=True,
        budget_authenticated=True,
        budget_operation="commit",
        infrastructure_failure="auth",
    )
    assert set(result.values()) == {"UNKNOWN"}


def test_scored_classifier_rejects_legacy_public_label() -> None:
    with pytest.raises(ValueError):
        classify(ClassificationInput(public_g2="RESCUED"))


def test_parse_junit_counts_passes_failures_and_skips(tmp_path: Path) -> None:
    path = tmp_path / "report.xml"
    path.write_text('<testsuite tests="4" failures="1" errors="1" skipped="1"/>')
    assert parse_junit(path) == {"tests": 4, "failed": 2, "errors": 1, "skipped": 1, "passed": 1}
