"""Closed pair-analysis-v1 report projection."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping

DISCLAIMER = (
    "Claim bound: this bench qualifies a seat for rung 1 plus the stated-plan/mechanical "
    "subset of rung 2, and can disqualify at the floor; it cannot rank rung-3 fitness "
    "and must never claim to."
)
RESULTS_DIR = Path("bench/implbench/results")
_FORBIDDEN = {"rank", "score", "composite", "trust", "quorum", "leaderboard", "promotion"}
_PRIVATE = {"stdout", "stderr", "traceback", "assertion", "diagnostic", "secret", "credential", "battery"}
_SHAPES = {"openinterpreter-dominated", "operationally-equivalent", "openinterpreter-adds-capability", "mixed-decorrelated"}


class ReportError(ValueError):
    pass


class WallBreach(ReportError):
    pass


def assert_no_rank_fields(obj: Any) -> None:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if str(key).lower() in _FORBIDDEN:
                raise WallBreach(f"forbidden report field: {key}")
            assert_no_rank_fields(value)
    elif isinstance(obj, list):
        for value in obj:
            assert_no_rank_fields(value)


def _row(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return {**record, **payload}
    return record


def _field(record: Mapping[str, Any], name: str, default: Any = None) -> Any:
    row = _row(record)
    return row.get(name, default)


def _timing(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if _field(row, "terminal_status") in {"completed", "ok"}]
    values = sorted(value for value in (_field(row, "wall_time_s") for row in successful) if isinstance(value, int) and value >= 0)
    if not values:
        return {"successful_median_wall_time_s": None, "successful_p95_wall_time_s": None, "failure_count": len(rows)}
    p95_index = min(len(values) - 1, max(0, math.ceil(len(values) * 0.95) - 1))
    return {"successful_median_wall_time_s": median(values), "successful_p95_wall_time_s": values[p95_index], "failure_count": len(rows) - len(successful)}


def _beta_cdf_integer(x: float, a: int, b: int) -> float:
    """Regularized beta CDF for the integer parameters of Clopper-Pearson."""

    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    total = 0.0
    degree = a + b - 1
    for value in range(a, degree + 1):
        total += math.comb(degree, value) * (x ** value) * ((1 - x) ** (degree - value))
    return min(1.0, total)


def _beta_quantile(probability: float, a: int, b: int) -> float:
    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if _beta_cdf_integer(middle, a, b) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _clopper_pearson(wins: int, non_tied: int) -> list[float]:
    if non_tied == 0:
        return [0.0, 1.0]
    lower = 0.0 if wins == 0 else _beta_quantile(0.025, wins, non_tied - wins + 1)
    upper = 1.0 if wins == non_tied else _beta_quantile(0.975, wins + 1, non_tied - wins)
    return [lower, upper]


def _pair(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    grid: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (_field(item, "task", _field(item, "task_id", "")), _field(item, "repetition", 0), _field(item, "arm", ""))):
        grid.append({
            "task": _field(row, "task", _field(row, "task_id", "")),
            "repetition": _field(row, "repetition", 0),
            "arm": _field(row, "arm", ""),
            "gates": {f"G{i}": _field(row, f"G{i}", _field(row, "gate" if i == 0 else "", "UNKNOWN")) for i in range(8)},
            "delivery": _field(row, "delivery", "UNKNOWN"),
            "tdd": _field(row, "tdd", "UNKNOWN"),
        })
    wins = losses = ties = 0
    for row in rows:
        outcome = _field(row, "g1_pair", _field(row, "pair_outcome", "tie"))
        if outcome == "openinterpreter":
            wins += 1
        elif outcome == "pi":
            losses += 1
        else:
            ties += 1
    non_tied = wins + losses
    p_value = 1.0
    status = "underpowered" if non_tied < 8 else "computed"
    if non_tied:
        tail = sum(math.comb(non_tied, k) for k in range(min(wins, losses) + 1)) / (2 ** non_tied)
        p_value = min(1.0, 2.0 * tail)
    return {
        "grid": grid,
        "g1_sign_test": {"wins": wins, "losses": losses, "ties": ties, "non_tied": non_tied, "p_value": p_value, "interval_95": _clopper_pearson(wins, non_tied), "status": status},
        "regressions": [{"gate": f"G{i}", "task": _field(row, "task", _field(row, "task_id", "")), "repetition": _field(row, "repetition", 0)} for row in rows for i in (3, 5, 6, 7) if _field(row, f"G{i}") == "FAIL"],
        "delivery_shape": {"delivered": sum(_field(row, "delivery") == "delivered" for row in rows), "not_delivered": sum(_field(row, "delivery") == "not-delivered" for row in rows)},
        "tdd_shape": {"pass": sum(_field(row, "tdd") == "PASS" for row in rows), "fail": sum(_field(row, "tdd") == "FAIL" for row in rows)},
        "timing": _timing(rows),
        "task_family_asymmetries": [],
        "within_arm_variance": {"wall_time_s": None},
        "evidence_shape": "mixed-decorrelated",
    }


def build_pair_analysis(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    grouped = {pair: [row for row in records if _field(row, "pair") == pair] for pair in ("GLM", "Kimi")}
    report = {"schema": "pair-analysis-v1", "pairs": {pair: _pair(rows) for pair, rows in grouped.items()}, "no_rankings": True, "no_composite_scores": True, "disclaimer": DISCLAIMER}
    validate_report(report)
    return report


def validate_report(report: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(report, Mapping) or set(report) != {"schema", "pairs", "no_rankings", "no_composite_scores", "disclaimer"}:
        raise ReportError("report fields mismatch")
    if report["schema"] != "pair-analysis-v1" or report["no_rankings"] is not True or report["no_composite_scores"] is not True or report["disclaimer"] != DISCLAIMER:
        raise ReportError("report schema or analysis wall mismatch")
    pairs = report["pairs"]
    if not isinstance(pairs, Mapping) or set(pairs) != {"GLM", "Kimi"}:
        raise ReportError("report must contain separate GLM and Kimi analyses")
    for pair in pairs.values():
        if not isinstance(pair, Mapping) or set(pair) != {"grid", "g1_sign_test", "regressions", "delivery_shape", "tdd_shape", "timing", "task_family_asymmetries", "within_arm_variance", "evidence_shape"}:
            raise ReportError("pair analysis fields mismatch")
        if pair["evidence_shape"] not in _SHAPES or not isinstance(pair["grid"], list) or not isinstance(pair["regressions"], list):
            raise ReportError("pair analysis contains an invalid evidence shape")
        sign = pair["g1_sign_test"]
        if not isinstance(sign, Mapping) or set(sign) != {"wins", "losses", "ties", "non_tied", "p_value", "interval_95", "status"} or sign["status"] not in {"underpowered", "computed"}:
            raise ReportError("sign-test fields mismatch")
        if any(not isinstance(sign[name], int) or sign[name] < 0 for name in ("wins", "losses", "ties", "non_tied")) or sign["non_tied"] != sign["wins"] + sign["losses"]:
            raise ReportError("sign-test counts are inconsistent")
        if sign["status"] == "underpowered" and sign["non_tied"] >= 8:
            raise ReportError("underpowered sign-test status is inconsistent")
        if sign["status"] == "computed" and sign["non_tied"] < 8:
            raise ReportError("computed sign-test status is inconsistent")
        if not isinstance(sign["p_value"], (int, float)) or not 0 <= sign["p_value"] <= 1:
            raise ReportError("sign-test p-value is invalid")
        interval = sign["interval_95"]
        if (not isinstance(interval, list) or len(interval) != 2 or any(not isinstance(value, (int, float)) or not 0 <= value <= 1 for value in interval) or interval[0] > interval[1]):
            raise ReportError("Clopper-Pearson interval is invalid")
        for name, expected in (("delivery_shape", {"delivered", "not_delivered"}), ("tdd_shape", {"pass", "fail"}), ("timing", {"successful_median_wall_time_s", "successful_p95_wall_time_s", "failure_count"}), ("within_arm_variance", {"wall_time_s"})):
            if not isinstance(pair[name], Mapping) or set(pair[name]) != expected:
                raise ReportError(f"{name} fields mismatch")
        if not isinstance(pair["task_family_asymmetries"], list):
            raise ReportError("task-family asymmetries must be a list")
        for row in pair["grid"]:
            if not isinstance(row, Mapping) or set(row) != {"task", "repetition", "arm", "gates", "delivery", "tdd"}:
                raise ReportError("grid row fields mismatch")
            if not isinstance(row["gates"], Mapping) or set(row["gates"]) != {f"G{i}" for i in range(8)}:
                raise ReportError("grid gate fields mismatch")
    _reject_private_fields(report)
    assert_no_rank_fields(report)
    return dict(report)


def _reject_private_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _PRIVATE or str(key).lower().endswith(("_stdout", "_stderr", "_traceback")):
                raise ReportError(f"private report field: {key}")
            _reject_private_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_private_fields(child)


def _read_records(root: Path) -> list[dict[str, Any]]:
    results = root / "results"
    rows: list[dict[str, Any]] = []
    for path in sorted(results.glob("*.ndjson")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ReportError("result row must be an object")
                rows.append(value)
    return rows


def render(evidence: str | Path) -> str:
    path = Path(evidence)
    if not path.is_absolute() or not path.is_dir():
        raise ReportError("--evidence must name an absolute evidence root")
    from .evidence import validate_evidence_package

    package = validate_evidence_package(path, require_sealed=True)
    result = build_pair_analysis(_read_records(package.root))
    return json.dumps(result, indent=2, sort_keys=True)


def summary_body(grid: dict, prov) -> str:
    assert_no_rank_fields(grid)
    lines = [f"seat: {prov.seat}", f"engine: {prov.engine}", f"model_declared: {prov.model_declared}", "", "clusters:"]
    for cluster, row in sorted(grid.get("clusters", {}).items()):
        lines.append(f"{cluster}: {row.get('verdict')}")
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)
