from __future__ import annotations

import pytest

from implbench.harness.report import ReportError, build_pair_analysis, validate_report


def test_pair_analysis_is_closed_and_has_no_ranking_surface() -> None:
    report = build_pair_analysis([])
    assert report["schema"] == "pair-analysis-v1"
    assert report["pairs"]["GLM"]["g1_sign_test"]["p_value"] == 1.0
    assert report["pairs"]["GLM"]["g1_sign_test"]["interval_95"] == [0.0, 1.0]
    assert set(report["pairs"]) == {"GLM", "Kimi"}
    validate_report(report)
    for forbidden in ("rank", "score", "composite", "leaderboard"):
        with pytest.raises(ReportError):
            validate_report({**report, forbidden: 1})


def test_pair_analysis_rejects_missing_pair_or_evidence_shape() -> None:
    report = build_pair_analysis([])
    broken = {**report, "pairs": {"GLM": report["pairs"]["GLM"]}}
    with pytest.raises(ReportError):
        validate_report(broken)


def test_render_requires_absolute_sealed_evidence_root(tmp_path) -> None:
    from implbench.harness.report import render

    with pytest.raises(ReportError):
        render(tmp_path / "missing")
