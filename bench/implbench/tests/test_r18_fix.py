from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from implbench.harness.runtime import ProductionRuntimeUnavailable, _SystemPlaneProvisioner
from implbench.harness.git_service import AttemptGitServiceServer, GitRPCError, GitService, RemoteGitService


def _helper(path: Path, *, live: bool = False) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "print(json.dumps({'version':'implbench-plane-v1','ok':True,"
        "'control_uid':101,'tool_uid':102,'git_uid':103,'tool_gid':104,"
        "'processes':[],'action':request['action'],'run_id':request['run_id'],"
        "'cell_id':request['cell_id'],'attempt_id':request['attempt_id'],"
        "'root':request['root'],'nonce':request['nonce']}))\n",
        encoding="utf-8",
    )
    path.chmod(0o750)


def _pins(monkeypatch: pytest.MonkeyPatch, helper: Path) -> None:
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_MODE", "0750")


def test_plane_helper_requires_nonempty_immutable_run_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "plane-helper"
    _helper(helper)
    _pins(monkeypatch, helper)
    with pytest.raises(ProductionRuntimeUnavailable, match="run ID"):
        _SystemPlaneProvisioner(helper=str(helper))


def test_plane_helper_executes_verified_descriptor_not_swapped_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "plane-helper"
    _helper(helper)
    _pins(monkeypatch, helper)
    provisioner = _SystemPlaneProvisioner(helper=str(helper), run_id="oi-pi-bakeoff-r18")
    replacement = tmp_path / "replacement"
    replacement.write_text("#!/bin/sh\necho swapped >&2\nexit 99\n", encoding="utf-8")
    replacement.chmod(0o750)
    os.replace(replacement, helper)
    # The opened image remains authoritative even after the pathname is replaced.
    assert provisioner.reserve_identities("cell-" + "a" * 64, attempt_id="attempt-1", root=tmp_path).tool_gid == 104


def test_helper_self_reported_empty_uid_is_independently_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "plane-helper"
    _helper(helper)
    _pins(monkeypatch, helper)
    provisioner = _SystemPlaneProvisioner(helper=str(helper), run_id="oi-pi-bakeoff-r18")
    monkeypatch.setattr(provisioner, "_census_uid", lambda uid: {777} if uid == 101 else set())
    with pytest.raises(ProductionRuntimeUnavailable, match="reserved identities are not empty"):
        provisioner.reserve_identities("cell-" + "a" * 64, attempt_id="attempt-1", root=tmp_path)


def test_attempt_git_rpc_is_cross_process_authenticated_and_terminally_removed(tmp_path: Path) -> None:
    """Executable seam: remote bridge client cannot forge/reuse controller Git authority."""
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "x.txt").write_text("x\n"); subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    fixture = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    service = GitService(repo, fixture_root_oid=fixture, allowed_paths=("*.txt",), tool_gid=104)
    server = AttemptGitServiceServer(service, root=tmp_path, attempt_id="attempt-" + "a" * 64, tool_gid=os.getgid(), peer_uids=(os.getuid(),))
    binding = server.start()
    client = RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=104)
    child = subprocess.run(
        [sys.executable, "-c", "import json,sys; from implbench.harness.git_service import RemoteGitService; b=json.loads(sys.argv[1]); print(RemoteGitService(endpoint=b['endpoint'], capability=b['capability'], tool_gid=104).handle({'op':'status'})['head'])", json.dumps(binding)],
        text=True, capture_output=True, check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2])},
    )
    assert child.stdout.strip() == fixture
    forged = RemoteGitService(endpoint=binding["endpoint"], capability="0" * 64, tool_gid=104)
    with pytest.raises(GitRPCError, match="authentication"):
        forged.handle({"op": "status"})
    endpoint = Path(binding["endpoint"]); server.close()
    assert not endpoint.exists()
    with pytest.raises(OSError):
        client.handle({"op": "status"})
