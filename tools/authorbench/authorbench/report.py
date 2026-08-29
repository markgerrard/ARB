from __future__ import annotations

import re
from typing import Any


FORBIDDEN_FIELDS = {"winner", "rank", "total", "composite", "score_sum"}


class WallBreach(RuntimeError):
    pass


def _canonical_key(key: str) -> str:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def assert_no_rank_fields(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_text = str(key)
            canonical = _canonical_key(key_text)
            if (
                canonical in FORBIDDEN_FIELDS
                or canonical.startswith("rank_")
                or canonical.endswith("_rank")
                or "cross_seat_sort" in canonical
            ):
                raise WallBreach(f"rank/composite field forbidden: {key_text}")
            assert_no_rank_fields(value)
    elif isinstance(obj, list | tuple):
        for item in obj:
            assert_no_rank_fields(item)


def guard(obj):
    assert_no_rank_fields(obj)
    return obj


def _headline_counts(detail: dict[str, Any]) -> str:
    counts = detail.get("mechanical_counts") or {}
    if counts:
        return f"{counts.get('verified', 0)} verified of {counts.get('checked', 0)}, {counts.get('fabricated', 0)} fabricated"
    return "mechanical counts unavailable"


def render_seat_report(seat, grid, detail) -> str:
    obj = {"seat": seat, "grid": grid, "detail": detail}
    guard(obj)
    lines = [
        f"# report-{seat}.md",
        "",
        f"Headline: {_headline_counts(detail)}",
        "",
        "## Dimension Grid",
    ]
    for brief, dims in grid.items():
        lines.append(f"- {brief}")
        for dim, verdict in dims.items():
            lines.append(f"  - {dim}: {verdict}")
    lines.extend(
        [
            "",
            "## Construct Validity",
            "Tree-only access narrows R2-R5 reads and may under-read history-archaeology diligence.",
            "Reader-convertibility residual: a dimension by brief grid remains convertible into a ranking by a motivated human.",
            "AB-D1 reconstruction caveat applies when AB-D1 appears in the grid.",
            "Same-family judge scores are non-certifying when an Anthropic judge scored an Anthropic author.",
            "",
            "## Claim Grammar",
            "Supported: seat S (model M, corpus v1.0, run R): on AB-D1 verified 7 of 9 key facts, avoided trap T1, carried trap T2 with evidence, NOT-MET on R3 with evidence.",
            "Not supported: S is a better author than T; S has improved; any sentence about S unqualified by brief and run.",
            "All reads are indicative, not capability measurements.",
        ]
    )
    return "\n".join(lines) + "\n"
