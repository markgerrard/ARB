from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from implbench.harness.cell_runtime import ACLIdentity
from implbench.harness.runtime import ProductionRuntimeUnavailable, _SystemPlaneProvisioner


def _helper(tmp_path: Path, response: dict) -> Path:
    path = tmp_path / "plane-helper"
    path.write_text("#!/usr/bin/env python3\nimport json,sys\nrequest=json.load(sys.stdin)\nresponse=" + repr(response) + "\nresponse.update({k:request[k] for k in ('action','run_id','cell_id','attempt_id','root','nonce')})\nprint(json.dumps(response))\n")
    path.chmod(0o750)
    return path


def test_helper_marker_success_is_not_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper(tmp_path, {"version": "implbench-plane-v1", "ok": True})
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    with pytest.raises(ProductionRuntimeUnavailable):
        _SystemPlaneProvisioner(helper=str(helper)).reserve_identities("cell-" + "a" * 64, attempt_id="attempt-1")


def test_helper_requires_operator_pins_and_full_identity_census(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper(tmp_path, {
        "version": "implbench-plane-v1", "ok": True,
        "control_uid": 101, "tool_uid": 102, "git_uid": 103, "tool_gid": 104,
        "processes": [],
    })
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("IMPLBENCH_PLANE_HELPER_MODE", "0750")
    provisioner = _SystemPlaneProvisioner(helper=str(helper), run_id="oi-pi-bakeoff-r17")
    assert provisioner.reserve_identities("cell-" + "a" * 64, attempt_id="attempt-1", root=tmp_path)


def test_acl_identity_never_reuses_public_identifier_as_password() -> None:
    first = ACLIdentity.create("cell-" + "b" * 64)
    second = ACLIdentity.create("cell-" + "b" * 64)
    assert first.password != second.password
    assert first.password not in first.user
