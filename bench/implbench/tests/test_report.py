from __future__ import annotations

import json
from pathlib import Path

import pytest

from implbench.harness.evidence import EvidencePackage, EvidencePackageError
from implbench.harness.provenance import Provenance
from implbench.harness.report import DISCLAIMER, WallBreach, assert_no_rank_fields, render, summary_body
from implbench.harness.scoring import build_grid


def test_reporter_refuses_rank_field() -> None:
    """RED: rank/score/trust/quorum/composite/leaderboard fields must breach the wall."""
    for key in ("rank", "score", "trust", "quorum", "composite", "leaderboard"):
        with pytest.raises(WallBreach):
            assert_no_rank_fields({"ok": [{"nested": {key: 1}}]})


def test_disclaimer_emitted_verbatim(tmp_path: Path, monkeypatch) -> None:
    """RED: render and summary_body must carry the P2 disclaimer byte-for-byte."""
    package = EvidencePackage.create(tmp_path / "evidence", {"schema_version": "manifest-v2", "run_id": "oi-pi-bakeoff-test"})
    package.seal([])
    assert DISCLAIMER in render(package.root)
    grid = build_grid([])
    prov = Provenance("seat", "engine", "model", "config+cli-version+billing-delta", "v", "h", "c")
    assert DISCLAIMER in summary_body(grid, prov)


def test_sealed_package_rejects_mutation(tmp_path: Path) -> None:
    package = EvidencePackage.create(tmp_path / "evidence", {"schema_version": "manifest-v2", "run_id": "oi-pi-bakeoff-test"})
    package.seal([])
    with pytest.raises(EvidencePackageError):
        package.append_public({"event": "late"})
