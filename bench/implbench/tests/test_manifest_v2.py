from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from implbench.harness.manifest import (
    DESIGN_BLOB,
    DESIGN_COMMIT,
    PLAN_BLOB,
    PLAN_COMMIT,
    SPEC_BLOB,
    SPEC_COMMIT,
    ManifestError,
    canonical_json_bytes,
    create_manifest,
    load_manifest,
    write_manifest,
)
from implbench.harness.fixtures import materialize
from implbench.harness.tasks import load_task


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "bench" / "implbench" / "fixtures"


def _pin(name: str, version: str) -> dict[str, str]:
    return {"version": version, "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest()}


PINS = {
    "binary": {"openinterpreter": _pin("openinterpreter", "0.0.21")},
    "profile": _pin("profile", "v2"),
    "importer": _pin("importer", "v2"),
    "scorer": _pin("scorer", "v2"),
    "battery": _pin("battery", "v1"),
    "public_suite": _pin("public-suite", "v1"),
}


def _manifest(tmp_path: Path):
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(ROOT), str(source)], check=True)
    return create_manifest(source, "oi-pi-bakeoff-test-20260714T000000Z", "00" * 32, pins=PINS)


def test_manifest_pins_and_exact_arms_and_corpus(tmp_path: Path) -> None:
    """RED: Task 2 requires one authoritative frozen manifest."""
    manifest = _manifest(tmp_path)
    assert manifest["schema_version"] == "manifest-v2"
    assert (manifest["design"]["commit"], manifest["design"]["blob"]) == (DESIGN_COMMIT, DESIGN_BLOB)
    assert (manifest["spec"]["commit"], manifest["spec"]["blob"]) == (SPEC_COMMIT, SPEC_BLOB)
    assert (manifest["plan"]["commit"], manifest["plan"]["blob"]) == (PLAN_COMMIT, PLAN_BLOB)
    assert [arm["arm"] for arm in manifest["arms"]] == ["glm-pi", "glm-zcode", "kimi-pi", "kimi-cli"]
    assert len(manifest["tasks"]) == 8
    assert manifest.to_dict()["extensions"] == {
        "role_profiles": [],
        "project_instruction_files": [],
        "optional_skill_packs": [],
        "memory_mcps": [],
        "unrelated_extensions": [],
    }
    assert manifest["controls"]["reasoning"]["requested"] == "medium"
    assert manifest["controls"]["reasoning"]["effective"] == "medium"
    assert manifest["controls"]["reasoning"]["verified_via"] not in {"request", "config", "echo"}
    assert manifest["git_rpc"] == {
        "max_frame_bytes": 1048576,
        "max_path_bytes": 4096,
        "max_components_per_path": 256,
        "max_component_bytes": 255,
        "max_paths_per_request": 1024,
        "max_in_flight": 8,
        "status_rate_per_second": 4,
        "status_burst": 8,
    }


def test_fixture_pin_is_materialized_commit_sha(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(ROOT), str(source)], check=True)
    manifest = create_manifest(source, "oi-pi-bakeoff-test-20260714T000000Z", "00" * 32, pins=PINS)
    task = load_task(source / "bench" / "implbench" / "fixtures" / "c1-permissive-boundary" / "task.yaml")
    repo = tmp_path / "fixture-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert manifest["tasks"][0]["fixture_sha"] == materialize(task, repo)


def test_manifest_roundtrip_is_canonical_and_mode_0600(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert raw == canonical_json_bytes(json.loads(raw)) + b"\n"
    assert load_manifest(path) == manifest


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"manifest-v2","schema_version":"manifest-v2"}',
        '{"schema_version":"manifest-v999"}',
        '{"schema_version":"manifest-v2","unknown":1}',
        '{"schema_version":"manifest-v2","bad":1.0}',
    ],
)
def test_manifest_parser_rejects_noncanonical_or_unknown_input(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(raw)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_manifest_rejects_dirty_or_symlinked_source(tmp_path: Path) -> None:
    dirty = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(ROOT), str(dirty)], check=True)
    (dirty / "dirty.txt").write_text("dirty\n")
    with pytest.raises(ManifestError):
        create_manifest(dirty, "oi-pi-bakeoff-test-20260714T000000Z", "00" * 32)

    link = tmp_path / "link"
    link.symlink_to(ROOT, target_is_directory=True)
    with pytest.raises(ManifestError):
        create_manifest(link, "oi-pi-bakeoff-test-20260714T000000Z", "00" * 32)


def test_manifest_is_immutable_after_write(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(path, _manifest(tmp_path))
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"medium"', b'"low"', 1))
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_manifest_requires_authoritative_artifact_pins(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(ROOT), str(source)], check=True)
    with pytest.raises(ManifestError):
        create_manifest(source, "oi-pi-bakeoff-test-20260714T000000Z", "00" * 32)


def test_manifest_cannot_be_replaced_in_place(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest(tmp_path)
    write_manifest(path, manifest)
    changed = manifest.to_dict()
    changed["run_id"] = "oi-pi-bakeoff-other-20260714T000000Z"
    with pytest.raises(ManifestError):
        write_manifest(path, changed)
