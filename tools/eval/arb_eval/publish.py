"""Publishable change-event artifact assembly."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import report


class PublishError(RuntimeError):
    pass


def _events_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seats(detail: dict[str, Any]) -> list[str]:
    return sorted((detail.get("oracle") or {}).keys())


def build_artifact(run_dir: str | Path, seat: str | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    detail = json.loads((run_dir / "detail.json").read_text())
    report.guard(detail)
    events_path = run_dir / "events.ndjson"
    seats = _seats(detail)
    if seat is None:
        if len(seats) != 1:
            raise PublishError(f"multi-seat run requires --seat; present seats: {', '.join(seats)}")
        seat = seats[0]
    elif seat not in seats:
        raise PublishError(f"seat {seat!r} not present; present seats: {', '.join(seats)}")

    prov = detail.get("provenance") or {}
    harness = (prov.get("harness_version") or {}).get("describe", "")
    if str(harness).endswith("-dirty"):
        raise PublishError("dirty harness refuses publish")
    model = (prov.get("model") or {}).get(seat, {})
    gold_version = (prov.get("gold_versions") or {}).get(seat, "GOLD_UNADJUDICATED")
    grid = {
        seat: {
            cls: row.get("verdict", "UNKNOWN")
            for cls, row in (detail.get("oracle") or {}).get(seat, {}).items()
            if row.get("claim_level") == "CLASS-LEVEL"
        }
    }
    lines = detail.get("report_lines") or {}
    artifact = {
        "seat": seat,
        "run_id": detail.get("run_id"),
        "model_version": model.get("model_requested") or model.get("model_reported"),
        "model_unverified": model.get("model_source") == "cli-version-only",
        "harness_version": harness,
        "corpus_version": prov.get("corpus_version"),
        "grid": grid,
        "claim_levels": detail.get("claim_levels", {}),
        "small_n_lines": lines.get("small_n", []),
        "orphan_lines": lines.get("orphan", []),
        "infra_incomplete_lines": lines.get("infra_incomplete", []),
        "events_sha256": _events_sha(events_path),
        "gold": {
            "gold_adjudicated": gold_version != "GOLD_UNADJUDICATED",
            "gold_version": gold_version,
            "suppressed_seats": list(detail.get("suppressed_seats", [])),
        },
        "disclaimer": report.DISCLAIMER,
    }
    report.assert_gold_field_shape(artifact["gold"])
    report.guard(artifact)
    report.assert_verdict_row(seat, artifact["grid"][seat])
    return artifact
