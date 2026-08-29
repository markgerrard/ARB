from __future__ import annotations

from .report import DISCLAIMER, assert_no_rank_fields
from .classifier import validate_public_g2

_POOLED = {"G0", "G1", "G3", "G5", "G6", "G7"}


def pool_cluster(members: list[str]) -> str:
    if any(v == "FAIL" for v in members):
        return "FAIL"
    if any(v == "UNKNOWN" for v in members):
        return "UNKNOWN"
    return "PASS"


def build_grid(records) -> dict:
    grid = {
        "schema": "implbench/v2",
        "seat": _first(records, "seat", ""),
        "engine": _first(records, "engine", ""),
        "model_declared": _first(records, "model_declared", ""),
        "model_verified_via": _first(records, "model_verified_via", "config+cli-version+billing-delta"),
        "harness_version": _first(records, "harness_version", ""),
        "corpus_version": _first(records, "corpus_version", ""),
        "run_id": _first(records, "run_id", ""),
        "clusters": {},
        "gates": {},
        "delivery": {},
        "tdd": {},
        "flags": {},
        "disclaimer": DISCLAIMER,
    }
    pooled: dict[str, list[str]] = {}
    members: dict[str, set[str]] = {}
    for record in records:
        task = record["task_id"]
        cluster = record["cluster"]
        gate = record["gate"]
        verdict = record["verdict"]
        if gate == "G2":
            validate_public_g2(verdict)
        grid["gates"].setdefault(task, {})[gate] = verdict
        members.setdefault(cluster, set()).add(task)
        if gate == "G2":
            grid["delivery"][task] = verdict
        elif gate == "G4":
            grid["tdd"][task] = verdict
        elif gate in _POOLED:
            pooled.setdefault(cluster, []).append(verdict)
        flags = record.get("flags")
        if flags:
            grid["flags"][task] = flags
    for cluster in {f"C{i}" for i in range(1, 8)} | set(members):
        values = pooled.get(cluster, ["UNKNOWN"])
        grid["clusters"][cluster] = {"verdict": pool_cluster(values), "members": sorted(members.get(cluster, set()))}
    assert_no_rank_fields(grid)
    return grid


def _first(records, key: str, default: str) -> str:
    for record in records:
        if key in record:
            return record[key]
    return default
