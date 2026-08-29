from __future__ import annotations

import subprocess
import hashlib
import json
from pathlib import Path

import pytest

from implbench.harness.ref_protection import (
    RefProtectionError,
    bakeoff_ref,
    parse_run_id,
    prune_protected_refs,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def test_bakeoff_ref_requires_cell_and_attempt() -> None:
    run_id = "oi-pi-bakeoff-test-20260714T000000Z"
    assert bakeoff_ref("runs", run_id, "cell-" + "a" * 64, "attempt-" + "b" * 32).endswith("/cell-" + "a" * 64 + "/attempt-" + "b" * 32)
    with pytest.raises(RefProtectionError):
        bakeoff_ref("runs", run_id, "task-only", "attempt-" + "b" * 32)


def test_run_id_parser_is_total_and_prefix_is_protected() -> None:
    assert parse_run_id("refs/implbench/runs/oi-pi-bakeoff-x/cell/attempt") == "oi-pi-bakeoff-x"
    assert parse_run_id("refs/heads/main") is None
    assert parse_run_id("refs/implbench/runs/not-bakeoff/task") is None


def test_prune_requires_evidence_root_and_deletes_only_eligible_ordinary_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--ref-format=files")
    _git(repo, "commit", "--allow-empty", "-qm", "root")
    oid = _git(repo, "rev-parse", "HEAD")
    old = "20260701T000000Z"
    ordinary = f"refs/heads/ordinary-{old}"
    protected = f"refs/heads/protected-{old}"
    _git(repo, "update-ref", ordinary, oid)
    _git(repo, "update-ref", protected, oid)
    root = tmp_path / "evidence"
    root.mkdir()
    manifest = b'{"schema_version":"manifest-v2"}\n'
    (root / "manifest.json").write_bytes(manifest)
    from implbench.harness.evidence import final_ref_index

    index = final_ref_index(hashlib.sha256(manifest).hexdigest(), "b" * 64, [(protected, oid)])
    (root / "git-refs.txt").write_text(json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n")
    assert prune_protected_refs(repo, "2026-07-09", evidence_root=root) == [ordinary]
    assert _git(repo, "show-ref", "--verify", protected)
    with pytest.raises(RefProtectionError):
        prune_protected_refs(repo, "2026-07-09")
