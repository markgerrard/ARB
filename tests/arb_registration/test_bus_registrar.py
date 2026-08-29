from __future__ import annotations

import base64
from datetime import timedelta
import json
import logging
import sys

from arb_crypto import generate_keypair, fingerprint, unseal
from arb_registration.bus_approval_cli import main as approval_main
from arb_registration.bus_acl import role_rules
from arb_registration.bus_registrar import BusRegistrar, REGISTRAR_ID
from arb_registration.crypto import load_or_create_key, public_identity
from arb_registration.protocol import (
    BUS_DENY_EVENT, BUS_GRANT_EVENT, BUS_REQUEST_EVENT, notify_envelope, signed_data,
)
from arb_registration.store import RegistrationStore, iso, utcnow


SELF_REPORT = {
    "supervisors": [
        {
            "service": "agent-bridge@codex.service",
            "kind": "systemd",
            "env_file": "/srv/agent-redis-bridge/envs/instance-codex.env",
        }
    ],
    "producers": [
        {
            "service": "agent-bridge@codex.service",
            "env_var": "ARB_EVAL_REDIS_URL",
            "db": 6,
            "streams": ["eval:events"],
        }
    ],
}


class FakeRedis:
    def __init__(self):
        self.pushed = []

    def lpush(self, key, value):
        self.pushed.append((key, json.loads(value)))


class FakeProvisioner:
    def __init__(self):
        self.hosts = []
        self.calls = []
        self.current = {}
        self.secrets = {
            "claude-orch-host-b": {
                "username": "claude-orch-host-b", "password": "claude-secret", "db": 12,
            },
            "codex-orch-host-b": {
                "username": "codex-orch-host-b", "password": "codex-secret", "db": 12,
            },
            "pi-orch-host-b": {
                "username": "pi-orch-host-b", "password": "pi-secret", "db": 12,
            },
            "arb-worker-host-b": {
                "username": "arb-worker-host-b", "password": "worker-secret", "db": 12,
            },
        }

    def provision(self, host, roles):
        self.hosts.append(host)
        self.calls.append((host, tuple(roles)))
        for username in role_rules(host, roles):
            self.current[username] = self.secrets[username]
        return dict(self.current)


def request_envelope(
    tmp_path, token, *, report=SELF_REPORT, sealing=None, name="host-b", host="host-b",
    roles=("claude", "codex", "pi", "worker"),
):
    signing_secret = load_or_create_key(tmp_path / "signing.key")
    pubkey, signing_pubkey = public_identity(signing_secret)
    sealing_private, sealing_public = sealing or generate_keypair()
    fields = {
        "token": token,
        "name": name,
        "host": host,
        "pubkey": pubkey,
        "signing_pubkey": signing_pubkey,
        "sealing_pubkey": base64.b64encode(sealing_public).decode(),
        "sealing_fingerprint": fingerprint(sealing_public),
        "reply_agent_id": "bus-registration-host-b",
        "client_nonce": "nonce",
        "issued_at": "2026-08-08T00:00:00Z",
        "roles": list(roles),
        "self_report": report,
    }
    data = signed_data(BUS_REQUEST_EVENT, fields, signing_secret, signing_pubkey)
    return (
        notify_envelope("bus-registration-host-b", REGISTRAR_ID, BUS_REQUEST_EVENT, data),
        sealing_private,
    )


def make_registrar(
    tmp_path, *, ttl=timedelta(hours=1), approval_timeout=timedelta(hours=24)
):
    store = RegistrationStore(tmp_path / "registrar.sqlite3")
    token, _ = store.mint("host-b", "host-b", ttl)
    redis = FakeRedis()
    provisioner = FakeProvisioner()
    registrar = BusRegistrar(
        store=store, redis_client=redis, provisioner=provisioner,
        endpoint="rediss://arb-bus.example:6379/12",
        approval_timeout=approval_timeout,
    )
    return store, token, redis, provisioner, registrar


def test_request_captures_inventory_and_waits_for_explicit_approval(tmp_path):
    store, token, _, provisioner, registrar = make_registrar(tmp_path)
    envelope, _ = request_envelope(tmp_path, token)
    registrar.handle(envelope)

    request = store.bus_requests()[0]
    assert request["status"] == "pending"
    assert json.loads(request["declared_roles_json"]) == [
        "claude", "codex", "pi", "worker",
    ]
    assert json.loads(request["self_report_json"]) == SELF_REPORT
    registrar.poll()
    assert provisioner.hosts == []


def test_bad_expired_and_reused_tokens_never_provision(tmp_path):
    store, token, _, provisioner, registrar = make_registrar(tmp_path)
    bad, _ = request_envelope(tmp_path, "bad-token")
    registrar.handle(bad)
    assert store.bus_requests() == []

    first, _ = request_envelope(tmp_path, token)
    registrar.handle(first)
    reused, _ = request_envelope(tmp_path, token)
    registrar.handle(reused)
    assert len(store.bus_requests()) == 1
    assert provisioner.hosts == []

    expired_store, expired, _, expired_provisioner, expired_registrar = make_registrar(
        tmp_path / "expired", ttl=timedelta(microseconds=1)
    )
    expired_envelope, _ = request_envelope(tmp_path / "expired", expired)
    expired_registrar.handle(expired_envelope)
    assert expired_store.bus_requests() == []
    assert expired_provisioner.hosts == []


def test_approval_mints_then_delivers_only_a_sealed_bundle(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    sealing = generate_keypair()
    envelope, sealing_private = request_envelope(tmp_path, token, sealing=sealing)
    registrar.handle(envelope)
    request_id = store.bus_requests()[0]["id"]
    assert store.set_bus_decision(request_id, "approve", "operator-cli")

    registrar.poll()

    assert provisioner.hosts == ["host-b"]
    assert store.request(request_id)["status"] == "provisioned"
    _, reply = redis.pushed[-1]
    payload = reply["payload"]
    assert payload["event"] == BUS_GRANT_EVENT
    serialized = json.dumps(reply, sort_keys=True)
    for role in provisioner.secrets.values():
        assert role["password"] not in serialized
    ciphertext = base64.b64decode(payload["data"]["sealed_bundle_b64"])
    bundle = json.loads(unseal(ciphertext, sealing_private))
    assert bundle["host"] == "host-b"
    assert bundle["roles"] == provisioner.secrets
    assert bundle["inventory_sha256"] == payload["data"]["inventory_sha256"]


def test_operator_denial_never_calls_provisioner(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    envelope, _ = request_envelope(tmp_path, token)
    registrar.handle(envelope)
    request_id = store.bus_requests()[0]["id"]
    assert store.set_bus_decision(request_id, "deny", "operator-cli")

    registrar.poll()

    assert provisioner.hosts == []
    assert store.request(request_id)["status"] == "denied"
    assert redis.pushed[-1][1]["payload"]["event"] == BUS_DENY_EVENT


def test_fingerprint_mismatch_rejected_without_burning_token(tmp_path):
    store, token, _, provisioner, registrar = make_registrar(tmp_path)
    envelope, _ = request_envelope(tmp_path, token)
    envelope["payload"]["data"]["sealing_fingerprint"] = "00" * 32
    registrar.handle(envelope)
    assert store.bus_requests() == []
    assert store.list_tokens()[0].status == "active"
    assert provisioner.hosts == []


def test_unknown_declared_role_is_rejected_without_burning_token(tmp_path):
    store, token, _, provisioner, registrar = make_registrar(tmp_path)
    envelope, _ = request_envelope(tmp_path, token, roles=("root",))
    registrar.handle(envelope)

    assert store.bus_requests() == []
    assert store.list_tokens()[0].status == "active"
    assert provisioner.hosts == []


def test_declared_subset_is_operator_visible_and_only_subset_is_provisioned(
    tmp_path, monkeypatch, capsys
):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    sealing = generate_keypair()
    envelope, sealing_private = request_envelope(
        tmp_path, token, sealing=sealing, roles=("codex", "worker")
    )
    registrar.handle(envelope)
    request_id = store.bus_requests()[0]["id"]

    monkeypatch.setattr(
        sys, "argv",
        ["bus-registrar-approve", "--store", str(store.path), "--list"],
    )
    approval_main()
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["declared_roles"] == ["codex", "worker"]

    assert store.set_bus_decision(request_id, "approve", "operator-cli")
    registrar.poll()

    assert provisioner.calls == [("host-b", ("codex", "worker"))]
    assert set(provisioner.current) == {
        "codex-orch-host-b", "arb-worker-host-b",
    }
    payload = redis.pushed[-1][1]["payload"]
    ciphertext = base64.b64decode(payload["data"]["sealed_bundle_b64"])
    bundle = json.loads(unseal(ciphertext, sealing_private))
    assert bundle["declared_roles"] == ["codex", "worker"]
    assert set(bundle["roles"]) == {
        "codex-orch-host-b", "arb-worker-host-b",
    }


def test_same_key_reack_adds_role_and_returns_full_current_union(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    sealing = generate_keypair()
    first, sealing_private = request_envelope(
        tmp_path, token, sealing=sealing, roles=("codex",)
    )
    registrar.handle(first)
    first_id = store.bus_requests()[0]["id"]
    assert store.set_bus_decision(first_id, "approve", "operator-cli")
    registrar.poll()
    pinned = store.host_sealing_pin("host-b")
    codex_password = provisioner.current["codex-orch-host-b"]["password"]

    second_token, _ = store.mint("host-b", "host-b", timedelta(hours=1))
    second, _ = request_envelope(
        tmp_path, second_token, sealing=sealing, roles=("worker",)
    )
    registrar.handle(second)
    second_id = store.bus_requests()[-1]["id"]
    assert store.set_bus_decision(second_id, "approve", "operator-cli")
    registrar.poll()

    assert store.host_sealing_pin("host-b") == pinned
    assert provisioner.calls == [
        ("host-b", ("codex",)), ("host-b", ("worker",)),
    ]
    assert provisioner.current["codex-orch-host-b"]["password"] == codex_password
    assert set(provisioner.current) == {
        "codex-orch-host-b", "arb-worker-host-b",
    }
    payload = redis.pushed[-1][1]["payload"]
    ciphertext = base64.b64decode(payload["data"]["sealed_bundle_b64"])
    bundle = json.loads(unseal(ciphertext, sealing_private))
    assert bundle["declared_roles"] == ["worker"]
    assert set(bundle["roles"]) == {
        "codex-orch-host-b", "arb-worker-host-b",
    }


def test_reprovision_with_a_different_key_is_rejected_by_host_pin(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    first_sealing = generate_keypair()
    first, _ = request_envelope(tmp_path, token, sealing=first_sealing)
    registrar.handle(first)
    first_request_id = store.bus_requests()[0]["id"]
    assert store.set_bus_decision(first_request_id, "approve", "operator-cli")
    registrar.poll()

    pinned = fingerprint(first_sealing[1])
    assert store.host_sealing_pin("host-b") == pinned
    second_token, _ = store.mint("host-b", "host-b", timedelta(hours=1))
    second, _ = request_envelope(
        tmp_path, second_token, sealing=generate_keypair()
    )
    registrar.handle(second)

    assert len(store.bus_requests()) == 1
    assert store.host_sealing_pin("host-b") == pinned
    assert provisioner.hosts == ["host-b"]
    assert len(redis.pushed) == 1


def test_token_expiring_between_reserve_and_approval_is_refused(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    envelope, _ = request_envelope(tmp_path, token)
    registrar.handle(envelope)
    request_id = store.bus_requests()[0]["id"]
    with store.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE tokens SET expires_at=? WHERE request_id=?",
            (iso(utcnow() - timedelta(seconds=1)), request_id),
        )
    assert store.set_bus_decision(request_id, "approve", "operator-cli")

    registrar.poll()

    assert provisioner.hosts == []
    assert store.request(request_id)["status"] == "denied"
    assert redis.pushed[-1][1]["payload"]["event"] == BUS_DENY_EVENT


def test_pending_request_expires_at_approval_timeout(tmp_path):
    store, token, redis, provisioner, registrar = make_registrar(
        tmp_path, approval_timeout=timedelta(0)
    )
    envelope, _ = request_envelope(tmp_path, token)
    registrar.handle(envelope)
    request_id = store.bus_requests()[0]["id"]

    registrar.poll()

    assert provisioner.hosts == []
    assert store.request(request_id)["status"] == "denied"
    assert redis.pushed[-1][1]["payload"]["event"] == BUS_DENY_EVENT


def test_provision_time_pin_gate_denies_second_pre_pin_request(tmp_path, caplog):
    store, first_token, redis, provisioner, registrar = make_registrar(tmp_path)
    second_token, _ = store.mint("host-b", "host-b", timedelta(hours=1))
    first_sealing = generate_keypair()
    second_sealing = generate_keypair()
    first, _ = request_envelope(tmp_path, first_token, sealing=first_sealing)
    second, _ = request_envelope(tmp_path, second_token, sealing=second_sealing)

    registrar.handle(first)
    registrar.handle(second)
    requests = {
        request["sealing_fingerprint"]: request for request in store.bus_requests()
    }
    first_request = requests[fingerprint(first_sealing[1])]
    second_request = requests[fingerprint(second_sealing[1])]
    assert first_request["status"] == second_request["status"] == "pending"
    now = utcnow()
    with store.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE requests SET created_at=? WHERE id=?",
            (iso(now), first_request["id"]),
        )
        conn.execute(
            "UPDATE requests SET created_at=? WHERE id=?",
            (iso(now + timedelta(microseconds=1)), second_request["id"]),
        )
    assert store.set_bus_decision(first_request["id"], "approve", "operator-cli")
    assert store.set_bus_decision(second_request["id"], "approve", "operator-cli")

    with caplog.at_level(
        logging.INFO, logger="arb_registration.bus_registrar"
    ):
        registrar.poll()

    assert store.request(first_request["id"])["status"] == "provisioned"
    assert store.request(second_request["id"])["status"] == "denied"
    assert store.host_sealing_pin("host-b") == fingerprint(first_sealing[1])
    assert provisioner.hosts == ["host-b"]
    events = [reply["payload"]["event"] for _, reply in redis.pushed]
    assert events.count(BUS_GRANT_EVENT) == 1
    assert events.count(BUS_DENY_EVENT) == 1
    assert any("sealing_key_pin_mismatch" in record.message for record in caplog.records)


def test_operator_local_unpin_audits_and_allows_rekey(
    tmp_path, monkeypatch, capsys, caplog
):
    store, token, redis, provisioner, registrar = make_registrar(tmp_path)
    first_sealing = generate_keypair()
    first, _ = request_envelope(tmp_path, token, sealing=first_sealing)
    registrar.handle(first)
    first_request_id = store.bus_requests()[0]["id"]
    assert store.set_bus_decision(first_request_id, "approve", "operator-cli")
    registrar.poll()
    assert store.host_sealing_pin("host-b") == fingerprint(first_sealing[1])

    monkeypatch.setattr(
        sys, "argv",
        [
            "bus-registrar-approve", "--store", str(store.path),
            "--unpin", "host-b",
        ],
    )
    with caplog.at_level(
        logging.WARNING, logger="arb_registration.bus_approval_cli"
    ):
        approval_main()

    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "host_sealing_key_unpinned"
    assert output["host"] == "host-b"
    assert output["source"] == "operator-cli"
    assert store.host_sealing_pin("host-b") is None
    assert store.bus_operator_audit() == [output]
    assert any("host_sealing_key_unpinned" in record.message for record in caplog.records)

    second_token, _ = store.mint("host-b", "host-b", timedelta(hours=1))
    second_sealing = generate_keypair()
    second, _ = request_envelope(tmp_path, second_token, sealing=second_sealing)
    registrar.handle(second)
    second_request_id = store.bus_requests()[-1]["id"]
    assert store.set_bus_decision(second_request_id, "approve", "operator-cli")
    registrar.poll()

    assert store.host_sealing_pin("host-b") == fingerprint(second_sealing[1])
    assert provisioner.hosts == ["host-b", "host-b"]
    events = [reply["payload"]["event"] for _, reply in redis.pushed]
    assert events.count(BUS_GRANT_EVENT) == 2
