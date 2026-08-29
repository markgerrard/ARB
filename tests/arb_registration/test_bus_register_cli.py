from __future__ import annotations

import json

import pytest

from arb_registration.bus_register_cli import (
    _validate_bundle_role_union, _write_private_json,
)


def test_private_bundle_atomically_replaces_existing_mode_0600_file(tmp_path):
    output = tmp_path / "bus-credentials.json"
    output.write_text(json.dumps({"roles": {"codex-orch-host-b": {}}}))
    output.chmod(0o600)

    union = {
        "roles": {
            "codex-orch-host-b": {"password": "preserved"},
            "arb-worker-host-b": {"password": "added"},
        }
    }
    _write_private_json(output, union)

    assert json.loads(output.read_text()) == union
    assert output.stat().st_mode & 0o777 == 0o600


def test_private_bundle_refuses_to_replace_insecure_existing_file(tmp_path):
    output = tmp_path / "bus-credentials.json"
    output.write_text("{}")
    output.chmod(0o644)

    with pytest.raises(ValueError, match="mode 0600"):
        _write_private_json(output, {"roles": {}})


def test_bundle_role_union_requires_declared_roles_and_only_host_roles():
    assert _validate_bundle_role_union(
        "host-b", ("codex",), {"codex-orch-host-b": {}}
    )
    assert _validate_bundle_role_union(
        "host-b", ("codex",),
        {"codex-orch-host-b": {}, "arb-worker-host-b": {}},
    )
    assert not _validate_bundle_role_union(
        "host-b", ("codex",), {"arb-worker-host-b": {}}
    )
    assert not _validate_bundle_role_union(
        "host-b", ("codex",),
        {"codex-orch-host-b": {}, "codex-orch-other": {}},
    )
