from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from implbench.harness.evidence import (
    EvidencePackageError,
    EvidencePackage,
    census_legacy_adapters,
    final_ref_index,
    manifest_digest,
)


def _manifest(run_id: str = "oi-pi-bakeoff-test-20260714T000000Z") -> dict[str, object]:
    return {
        "schema_version": "manifest-v2",
        "run_id": run_id,
        "source": {"realpath": "/tmp/source", "commit": "a" * 40, "tree": "b" * 40, "dirty": False},
    }


def test_full_tree_legacy_adapter_census_is_zero_after_migration() -> None:
    hits = census_legacy_adapters(Path(__file__).parents[1])
    assert hits == ()


def test_package_writes_open_layout_without_final_index(tmp_path: Path) -> None:
    package = EvidencePackage.create(tmp_path / "evidence", _manifest())
    assert (package.root / "manifest.json").is_file()
    assert not (package.root / "git-refs.txt").exists()
    assert (package.root / "results").is_dir()
    assert manifest_digest(package.root) == package.manifest_digest


def test_closed_package_is_immutable_and_index_is_canonical(tmp_path: Path) -> None:
    package = EvidencePackage.create(tmp_path / "evidence", _manifest())
    package.seal([("refs/heads/old", "a" * 40)])
    assert package.is_sealed
    assert json.loads((package.root / "git-refs.txt").read_text()) == final_ref_index(
        package.manifest_digest, package.journal_tail_digest, [("refs/heads/old", "a" * 40)]
    )
    with pytest.raises(EvidencePackageError):
        package.append_public({"anything": "after-close"})


def test_census_is_ast_based_and_does_not_count_strings(tmp_path: Path) -> None:
    path = tmp_path / "fixture.py"
    path.write_text("# Recorder and prune_refs are historical words\nvalue = 'Recorder'\n")
    assert not any(hit.path == str(path) for hit in census_legacy_adapters(tmp_path))
