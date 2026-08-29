from __future__ import annotations

from implbench.harness.gates import GateResult
from implbench.harness.scoring import build_grid, pool_cluster


def _rec(task: str, cluster: str, gate: str, verdict: str) -> dict:
    return {"run_id": "r", "task_id": task, "cluster": cluster, "gate": gate, "verdict": verdict, "evidence": {}}


def test_cluster_pooling_precedence() -> None:
    """RED: pooling precedence must be FAIL > UNKNOWN > PASS."""
    assert pool_cluster(["PASS", "PASS"]) == "PASS"
    assert pool_cluster(["PASS", "UNKNOWN"]) == "UNKNOWN"
    assert pool_cluster(["UNKNOWN", "FAIL", "PASS"]) == "FAIL"


def test_g4_g2_columns_excluded_from_pooling() -> None:
    """RED: G2/G4 column values must not affect cluster pool verdict."""
    grid = build_grid([_rec("t1", "C1", "G0", "PASS"), _rec("t1", "C1", "G2", "agent-delivered"), _rec("t1", "C1", "G4", "NOT_DEMONSTRATED")])
    assert grid["clusters"]["C1"]["verdict"] == "PASS"


def test_legacy_public_g2_values_are_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_grid([_rec("t1", "C1", "G2", "DELIVERED")])


def test_empty_clusters_are_unknown_not_pass() -> None:
    """RED: clusters with no task records must not pool to false-green PASS."""
    grid = build_grid([])
    assert grid["clusters"]["C1"]["verdict"] == "UNKNOWN"


def test_build_grid_shape() -> None:
    """RED: emitted grid must have the fixed schema key set only."""
    grid = build_grid([_rec("t1", "C1", "G0", "PASS")])
    assert set(grid) == {
        "schema",
        "seat",
        "engine",
        "model_declared",
        "model_verified_via",
        "harness_version",
        "corpus_version",
        "run_id",
        "clusters",
        "gates",
        "delivery",
        "tdd",
        "flags",
        "disclaimer",
    }
