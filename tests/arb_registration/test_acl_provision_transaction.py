"""Provisioning must be transactional: prove live BEFORE persisting, restore on failure.

`provision()` used to replace credentials.json, replace users.acl, ACL LOAD, and only THEN
run the live proof — with no except/finally path. A proof failure therefore left both files
and the live ACL changed, which is exactly what codex-arbmem-prod hit on 2026-08-10 when a
malformed proof key denied the own-secret check while minting claude-orch-mini-dev. It
refused to run the provisioner rather than discover that state.

The provisioner had NO unit tests before this file, which is how the ordering survived.

Shape (proposal acl-provision-preflight-rollback-v1, plus lead review):
  preflight  create each candidate LIVE with the EXACT username and rule args, verify,
             remove it again — before either file is touched
  marker     written before the FIRST mutation, including the in-memory SETUSER, because
             a crash there leaves a live identity present in NEITHER file
  commit     replace both files, ACL LOAD, verify against the persisted identity
  rollback   restore both files exactly and reload; a failed rollback is never masked
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arb_registration import bus_acl
from arb_registration.bus_acl import (
    AclProvisionError, AclProvisioner, AclResidueError, role_username,
)

HOST = "mini-dev"
ADMIN = "arb-admin"


class FakeConnector:
    """Tracks live ACL users; can be told to fail a specific command."""

    def __init__(self, *, fail_on: str | None = None, fail_times: int = 1) -> None:
        self.live: set[str] = set()
        self.calls: list[tuple] = []
        self.fail_on = fail_on
        self.fail_times = fail_times
        self.loads = 0

    def command(self, username, password, db, *command):
        self.calls.append(command)
        verb = " ".join(str(c) for c in command[:2])
        if self.fail_on and verb.startswith(self.fail_on) and self.fail_times > 0:
            self.fail_times -= 1
            raise bus_acl.redis.RedisError(f"injected failure on {verb}")
        if command[:2] == ("ACL", "SETUSER"):
            self.live.add(command[2])
        elif command[:2] == ("ACL", "DELUSER"):
            self.live.discard(command[2])
        elif command[:2] == ("ACL", "LOAD"):
            self.loads += 1
        return True


@pytest.fixture()
def paths(tmp_path: Path):
    acl = tmp_path / "users.acl"
    creds = tmp_path / "credentials.json"
    acl.write_text("user arb-admin on #deadbeef ~* +@all\n")
    creds.write_text(json.dumps({ADMIN: "admin-pw"}) + "\n")
    return acl, creds


def _provisioner(paths, connector) -> AclProvisioner:
    acl, creds = paths
    return AclProvisioner(acl_path=acl, credentials_path=creds, connector=connector)


@pytest.fixture()
def verify_ok(monkeypatch):
    seen = []
    monkeypatch.setattr(
        bus_acl.AclVerifier, "verify",
        lambda self, host, credentials, roles: seen.append(tuple(roles)),
    )
    return seen


def _fail_verify(monkeypatch, times=99):
    state = {"n": times}

    def _verify(self, host, credentials, roles):
        if state["n"] > 0:
            state["n"] -= 1
            raise AclProvisionError("injected proof failure")

    monkeypatch.setattr(bus_acl.AclVerifier, "verify", _verify)


# 1
def test_proof_failure_before_commit_leaves_files_byte_identical_and_no_live_candidate(
    paths, monkeypatch
):
    acl, creds = paths
    before = (acl.read_bytes(), creds.read_bytes())
    connector = FakeConnector()
    _fail_verify(monkeypatch)

    with pytest.raises(AclProvisionError, match="injected proof failure"):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert (acl.read_bytes(), creds.read_bytes()) == before, "files were touched"
    assert connector.live == set(), "preflight left a live identity behind"
    assert connector.loads == 0, "nothing should have been reloaded"


# 2
def test_failure_after_first_replace_restores_both_files(paths, monkeypatch, verify_ok):
    acl, creds = paths
    before = (acl.read_bytes(), creds.read_bytes())
    connector = FakeConnector(fail_on="ACL LOAD")

    with pytest.raises(AclProvisionError):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert (acl.read_bytes(), creds.read_bytes()) == before, "rollback did not restore"


# 3
def test_acl_load_failure_restores_originals_and_reloads(paths, monkeypatch, verify_ok):
    acl, creds = paths
    connector = FakeConnector(fail_on="ACL LOAD", fail_times=1)

    with pytest.raises(AclProvisionError, match="ACL LOAD failed"):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert connector.loads >= 1, "rollback must reload the restored file"
    assert role_username("claude", HOST) not in acl.read_text()


# 4
def test_post_load_proof_failure_restores_originals(paths, monkeypatch):
    acl, creds = paths
    before = (acl.read_bytes(), creds.read_bytes())
    connector = FakeConnector()
    # preflight verify passes, the post-commit one fails
    state = {"n": 0}

    def _verify(self, host, credentials, roles):
        state["n"] += 1
        if state["n"] > 1:
            raise AclProvisionError("injected post-load proof failure")

    monkeypatch.setattr(bus_acl.AclVerifier, "verify", _verify)

    with pytest.raises(AclProvisionError, match="post-load proof failure"):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert (acl.read_bytes(), creds.read_bytes()) == before


# 5
def test_rollback_failure_is_not_masked(paths, monkeypatch, verify_ok):
    """A rollback that fails quietly turns a recoverable state into an unknown one."""
    connector = FakeConnector(fail_on="ACL LOAD", fail_times=99)

    with pytest.raises(AclProvisionError, match="ROLLBACK FAILED") as exc:
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert "do not retry" in str(exc.value)


# 6
def test_crash_marker_blocks_a_second_provision(paths, verify_ok):
    acl, _creds = paths
    connector = FakeConnector()
    provisioner = _provisioner(paths, connector)
    marker = acl.with_name(acl.name + ".provision-inflight")
    marker.write_text("{}")

    with pytest.raises(AclProvisionError, match="unresolved provision marker"):
        provisioner.provision(HOST, ["claude"])


# P1 (codex-arbmem-prod review of cb6d109f): the first version cleared the marker in an
# `except BaseException` around preflight and an unconditional `finally` around commit —
# disarming the guard in precisely the two states it exists for. A marker that only
# survives the happy path is not a marker.
def test_stranded_live_candidate_keeps_the_marker_and_blocks_the_next_provision(
    paths, verify_ok
):
    """DELUSER fails: a fully-permissioned identity is live and in NEITHER file."""
    acl, _creds = paths
    marker = acl.with_name(acl.name + ".provision-inflight")
    connector = FakeConnector(fail_on="ACL DELUSER", fail_times=99)

    with pytest.raises(AclResidueError, match="NEITHER file"):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert connector.live, "the candidate really is still live"
    assert marker.exists(), "marker was cleared over a stranded live identity"

    with pytest.raises(AclProvisionError, match="unresolved provision marker"):
        _provisioner(paths, FakeConnector()).provision(HOST, ["claude"])


def test_failed_rollback_keeps_the_marker_and_blocks_the_next_provision(paths, verify_ok):
    """Commit fails AND rollback fails: the state is explicitly unknown."""
    acl, _creds = paths
    marker = acl.with_name(acl.name + ".provision-inflight")
    connector = FakeConnector(fail_on="ACL LOAD", fail_times=99)

    with pytest.raises(AclResidueError, match="ROLLBACK FAILED"):
        _provisioner(paths, connector).provision(HOST, ["claude"])

    assert marker.exists(), "marker was cleared over a known-inconsistent bus"

    with pytest.raises(AclProvisionError, match="unresolved provision marker"):
        _provisioner(paths, FakeConnector()).provision(HOST, ["claude"])


def test_clean_failures_still_clear_the_marker(paths, monkeypatch):
    """The guard must not be so sticky it fires on states that ARE resolvable."""
    acl, _creds = paths
    marker = acl.with_name(acl.name + ".provision-inflight")
    _fail_verify(monkeypatch)

    with pytest.raises(AclProvisionError, match="injected proof failure"):
        _provisioner(paths, FakeConnector()).provision(HOST, ["claude"])

    assert not marker.exists(), "a clean preflight failure must not strand the marker"


def test_marker_is_cleared_on_success_and_carries_no_plaintext(paths, verify_ok):
    acl, creds = paths
    connector = FakeConnector()
    marker = acl.with_name(acl.name + ".provision-inflight")
    captured = {}
    original = AclProvisioner._write_marker

    def _spy(self, host, acl_before, credentials_before, candidates):
        original(self, host, acl_before, credentials_before, candidates)
        captured["text"] = marker.read_text()

    AclProvisioner._write_marker = _spy
    try:
        result = _provisioner(paths, connector).provision(HOST, ["claude"])
    finally:
        AclProvisioner._write_marker = original

    assert not marker.exists(), "marker must not survive a successful provision"
    assert "acl_sha256_before" in captured["text"]
    minted = result[role_username("claude", HOST)]["password"]
    assert minted not in captured["text"], "marker leaked a plaintext credential"


def test_preflight_proves_the_exact_username_and_rules_that_will_be_persisted(
    paths, monkeypatch, verify_ok
):
    """Proving an adjacent identity is the defect this area keeps producing."""
    acl, _creds = paths
    connector = FakeConnector()
    _provisioner(paths, connector).provision(HOST, ["claude"])

    username = role_username("claude", HOST)
    setusers = [c for c in connector.calls if c[:2] == ("ACL", "SETUSER")]
    assert [c[2] for c in setusers] == [username], "preflight used a different username"

    persisted = next(
        line for line in acl.read_text().splitlines() if line.split()[1] == username
    )
    # args are (ACL, SETUSER, username, "on", ">password", *rules) — skip the password,
    # which is deliberately persisted hashed rather than in the clear.
    live_rules = setusers[0][5:]
    assert live_rules, "preflight sent no rule arguments"
    for arg in live_rules:
        assert arg in persisted, f"preflight rule {arg!r} is not what was persisted"
