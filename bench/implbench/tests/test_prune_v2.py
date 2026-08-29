from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from implbench.harness.evidence import final_ref_index
from implbench.harness import ref_protection
from implbench.harness.ref_protection import RefProtectionError, prune_protected_refs


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def test_prune_requires_versioned_root_and_preserves_all_four_canaries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--ref-format=files")
    _git(repo, "commit", "--allow-empty", "-qm", "root")
    oid = _git(repo, "rev-parse", "HEAD")
    refs = [
        "refs/heads/index-only-20260701T000000Z",
        "refs/heads/ordinary-20260701T000000Z",
        "refs/heads/active-20260701T000000Z",
        "refs/implbench/runs/oi-pi-bakeoff-prefix-20260701T000000Z/cell-a/attempt-b",
    ]
    for ref in refs:
        _git(repo, "update-ref", ref, oid)
    root = tmp_path / "evidence"
    root.mkdir()
    manifest = json.dumps({"schema_version": "manifest-v2", "run_id": "oi-pi-bakeoff-active-20260701T000000Z", "cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 32, "ref": refs[2]}, sort_keys=True, separators=(",", ":")) + "\n"
    (root / "manifest.json").write_text(manifest)
    (root / "git-refs.txt").write_text(json.dumps(final_ref_index(hashlib.sha256(manifest.encode()).hexdigest(), "b" * 64, [(refs[0], oid)]), sort_keys=True, separators=(",", ":")) + "\n")
    (root / "attempt.ndjson").write_text(json.dumps({"run_id": "oi-pi-bakeoff-active-20260701T000000Z", "cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 32, "ref": refs[2]}) + "\n")
    assert prune_protected_refs(repo, "2026-07-09", evidence_root=root) == [refs[1]]
    for ref in (refs[0], refs[2], refs[3]):
        assert _git(repo, "show-ref", "--verify", ref)


def test_prune_rejects_missing_root_before_repo_mutation(tmp_path: Path) -> None:
    with pytest.raises(RefProtectionError):
        prune_protected_refs(tmp_path, "2026-07-09")


def test_prune_rejects_evidence_root_changed_during_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "manifest.json").write_text('{"schema_version":"manifest-v2"}')
    snapshots = [(("manifest.json", 1, 1, b"a"),), (("manifest.json", 1, 1, b"b"),)]
    monkeypatch.setattr(ref_protection, "_evidence_snapshot", lambda _root: snapshots.pop(0))
    with pytest.raises(RefProtectionError, match="changed"):
        ref_protection.protected_refs(root)
