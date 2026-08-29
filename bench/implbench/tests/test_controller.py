from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.controller import (
    CLOSE_PHASES,
    CloseState,
    Controller,
    census_legacy_adapters,
    classify_close,
)


def test_red_close_runs_one_ordered_dispatcher_and_never_backtracks(tmp_path: Path) -> None:
    events: list[str] = []
    controller = Controller(tmp_path / "close.ndjson", actions={name: lambda name=name: events.append(name) for name in CLOSE_PHASES})

    result = controller.close(terminal="dispatch-failed")

    assert result.state is CloseState.DESTROYED
    expected = [phase for phase in CLOSE_PHASES if phase != "IMPORT_SCORE"]
    assert events == expected
    assert [row["status"] for row in controller.journal.read()] == ["prepared", "committed"] * len(expected)


def test_red_close_does_not_import_or_score_empty_or_dirty_seals(tmp_path: Path) -> None:
    calls: list[str] = []
    controller = Controller(
        tmp_path / "close.ndjson",
        actions={"IMPORT_SCORE": lambda: calls.append("score")},
        close_context={
            "receipts": (), "imported_oids": (), "dirty": True, "seal_complete": True,
            "receipts_authenticated": True, "imported_graph_attested": True,
            "infrastructure_failure": None, "dispatch_status": "ok",
        },
    )

    result = controller.close(terminal="completed")

    assert result.state is CloseState.DESTROYED
    assert calls == []
    assert result.classification["G2"] == "not-delivered"


@pytest.mark.parametrize(
    "context",
    [
        {"receipts": ("a" * 40,), "imported_oids": ("a" * 40,), "dirty": True, "seal_complete": True},
        {"receipts": ("a" * 40,), "imported_oids": ("a" * 40,), "dirty": False, "seal_complete": False},
        {"receipts": ("a" * 40,), "imported_oids": ("a" * 40,), "dirty": False, "seal_complete": True, "infrastructure_failure": "auth"},
    ],
)
def test_red_classify_precedes_import_for_nonempty_non_deliverable_seals(tmp_path: Path, context: dict[str, object]) -> None:
    calls: list[str] = []
    controller = Controller(
        tmp_path / "close.ndjson",
        actions={"IMPORT_SCORE": lambda: calls.append("score")},
        close_context=context,
    )

    result = controller.close()

    assert result.classification["G2"] != "agent-delivered"
    assert calls == []
    assert "CLASSIFY" in result.phases
    assert "IMPORT_SCORE" not in result.phases


def test_red_close_recovery_resumes_first_uncommitted_action_only(tmp_path: Path) -> None:
    events: list[str] = []
    fired = {"value": False}

    def crash() -> None:
        events.append("FINAL_STATUS")
        if not fired["value"]:
            fired["value"] = True
            raise RuntimeError("crash after status side effect")

    controller = Controller(
        tmp_path / "close.ndjson",
        actions={"FINAL_STATUS": crash, "KILL_GIT": lambda: events.append("KILL_GIT")},
        recovery_actions={"FINAL_STATUS": lambda: events.append("FINAL_STATUS_PROBE")},
    )

    with pytest.raises(RuntimeError, match="crash after status side effect"):
        controller.close(terminal="completed")
    controller.recover()

    assert events == ["FINAL_STATUS", "FINAL_STATUS_PROBE", "KILL_GIT"]
    assert controller.state is CloseState.DESTROYED


def test_census_is_zero_after_all_legacy_adapter_call_sites_are_migrated() -> None:
    hits = census_legacy_adapters(Path(__file__).parents[1] / "harness")
    assert hits == ()


def test_full_tree_census_covers_production_and_test_python() -> None:
    hits = census_legacy_adapters(Path(__file__).parents[1])
    assert hits == ()


def test_red_close_branch_precedence_matches_frozen_scored_completion() -> None:
    empty = classify_close(dispatch_status="ok", receipts=(), dirty=False, seal_complete=True)
    dirty = classify_close(dispatch_status="ok", receipts=("a" * 40,), dirty=True, seal_complete=True)
    delivered = classify_close(dispatch_status="ok", receipts=("a" * 40,), imported_oids=("a" * 40,), dirty=False, seal_complete=True)
    budget_infra = classify_close(
        dispatch_status="ok",
        receipts=(),
        dirty=False,
        seal_complete=False,
        budget_authenticated=True,
        budget_operation="commit",
        infrastructure_failure="auth",
    )

    assert empty["G2"] == "not-delivered" and empty["G1"] == "NOT_SCORED"
    assert dirty["G2"] == "not-delivered" and dirty["G1"] == "NOT_SCORED"
    assert delivered["G2"] == "agent-delivered"
    assert all(value == "UNKNOWN" for value in budget_infra.values())
