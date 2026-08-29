from __future__ import annotations

from datetime import timedelta
import json

import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from arb_registration.crypto import load_or_create_key, public_identity
from arb_registration.protocol import (
    DENY_EVENT,
    GRANT_EVENT,
    PROFILE_READY_EVENT,
    PROVISION_EVENT,
    REQUEST_EVENT,
    notify_envelope,
    signed_data,
)
from arb_registration.registrar import Registrar
from arb_registration.store import RegistrationStore, iso, utcnow


class FakeRedis:
    def __init__(self): self.pushed = []
    def lpush(self, key, value): self.pushed.append((key, json.loads(value)))


class FakeBuzz:
    mark_pubkey = "aa" * 32
    def __init__(self):
        self.messages = []
        self.provisioned = []
        self.thread_messages = []
        self.reaction_items = {}
        self.reacted = []
        self.replies = []
        self.timeline = []
        self.profile_visible = False
        self.standing_warnings = []
        self.auth_tag = None
        self.owner_auth_enabled = False
    def registrar_standing_warnings(self, channels): return self.standing_warnings
    def owner_auth_tag(self, pubkey): return self.auth_tag
    def post_approval(self, *args): self.messages.append(args); return "event-1"
    def thread(self, event): return self.thread_messages
    def reactions(self, event): return self.reaction_items
    def react(self, event, emoji):
        self.timeline.append("react")
        self.reacted.append((event, emoji))
    def reply(self, event, content): self.replies.append((event, content))
    def provision(self, pubkey, channels):
        self.timeline.append("provision")
        self.provisioned.append((pubkey, channels))
    def verify_profile(self, pubkey, name):
        self.timeline.append("verify_profile")
        self.profile_visible = True
    def bind_owner(self, pubkey):
        assert self.profile_visible, "a new seat has no users row before first profile publication"
        self.timeline.append("bind_owner")


def test_valid_signed_request_reserves_token_and_posts_approval(tmp_path):
    store = RegistrationStore(tmp_path / "store.sqlite3")
    token, _ = store.mint("throwaway", "host-b", timedelta(hours=1))
    secret = load_or_create_key(tmp_path / "key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {
        "token": token, "name": "throwaway", "host": "host-b", "pubkey": pubkey,
        "reply_agent_id": "reply", "channels": ["chan"], "client_nonce": "nonce",
        "issued_at": "now",
    }, secret, compressed)
    buzz = FakeBuzz()
    registrar = Registrar(store=store, redis_client=FakeRedis(), prefix="test:", agent_id="registrar", relay_url="wss://relay", buzz=buzz)
    registrar.handle(notify_envelope("reply", "registrar", REQUEST_EVENT, data))
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["approval_event_id"] == "event-1"
    assert buzz.messages[0][1:4] == ("throwaway", "host-b", pubkey)


def test_registration_posts_channel_admin_precheck_warning(tmp_path):
    registrar, store, _, buzz, _, _, _ = _pending_registration(tmp_path)
    assert store.pending()
    buzz.messages.clear()
    buzz.standing_warnings = ["channel `chan`: registrar role `member` is not admin"]

    token, _ = store.mint("second", "host-b", timedelta(hours=1))
    secret = load_or_create_key(tmp_path / "second.key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {
        "token": token, "name": "second", "host": "host-b", "pubkey": pubkey,
        "reply_agent_id": "reply-2", "channels": ["chan"], "client_nonce": "nonce-2",
        "issued_at": "now",
    }, secret, compressed)
    registrar.handle(notify_envelope("reply-2", "registrar", REQUEST_EVENT, data))

    assert buzz.messages[0][-1] == buzz.standing_warnings


def test_invalid_signature_is_silently_ignored_without_consuming_token(tmp_path):
    store = RegistrationStore(tmp_path / "store.sqlite3")
    token, record = store.mint("throwaway", "host-b", timedelta(hours=1))
    secret = load_or_create_key(tmp_path / "key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {
        "token": token, "name": "throwaway", "host": "host-b", "pubkey": pubkey,
        "reply_agent_id": "reply", "channels": [], "client_nonce": "nonce",
        "issued_at": "now",
    }, secret, compressed)
    data["host"] = "attacker"
    buzz = FakeBuzz()
    Registrar(store=store, redis_client=FakeRedis(), prefix="test:", agent_id="registrar", relay_url="wss://relay", buzz=buzz).handle(
        notify_envelope("reply", "registrar", REQUEST_EVENT, data)
    )
    assert not store.pending()
    assert store.list_tokens()[0].status == "active"
    assert buzz.messages == []


def _pending_registration(tmp_path):
    store = RegistrationStore(tmp_path / "state.sqlite3")
    token, _ = store.mint("throwaway", "host-b", timedelta(hours=1))
    secret = load_or_create_key(tmp_path / "seat.key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {
        "token": token, "name": "throwaway", "host": "host-b", "pubkey": pubkey,
        "reply_agent_id": "reply", "channels": ["chan"], "client_nonce": "nonce",
        "issued_at": "now",
    }, secret, compressed)
    redis = FakeRedis()
    buzz = FakeBuzz()
    registrar = Registrar(
        store=store, redis_client=redis, prefix="test:", agent_id="registrar",
        relay_url="wss://relay", buzz=buzz,
    )
    registrar.handle(notify_envelope("reply", "registrar", REQUEST_EVENT, data))
    return registrar, store, redis, buzz, secret, pubkey, compressed


def test_only_mark_reply_can_deny_and_token_stays_burned(tmp_path):
    registrar, store, redis, buzz, _, _, _ = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    buzz.thread_messages = [
        {"pubkey": "bb" * 32, "content": f"approve {request_id}"},
        {"pubkey": buzz.mark_pubkey, "content": f"deny {request_id}"},
    ]
    registrar.poll_approvals()
    assert store.request(request_id)["status"] == "denied"
    assert store.list_tokens()[0].status == "denied"
    reply = redis.pushed[-1][1]["payload"]
    assert reply["event"] == DENY_EVENT
    assert reply["data"]["client_nonce"] == "nonce"
    assert "Denied — request" in buzz.replies[-1][1]


def test_approval_provisions_then_profile_readback_grants(tmp_path):
    registrar, store, redis, buzz, secret, pubkey, compressed = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    buzz.thread_messages = [{"pubkey": buzz.mark_pubkey, "content": f"approve {request_id}"}]
    registrar.poll_approvals()
    assert store.request(request_id)["status"] == "provisioned"
    assert buzz.provisioned == [(pubkey, ["chan"])]
    assert redis.pushed[-1][1]["payload"]["event"] == PROVISION_EVENT

    ready = signed_data(PROFILE_READY_EVENT, {
        "request_id": request_id, "pubkey": pubkey, "profile_name": "throwaway",
        "client_nonce": "nonce", "issued_at": "now",
    }, secret, compressed)
    registrar.handle(notify_envelope("reply", "registrar", PROFILE_READY_EVENT, ready))
    assert store.request(request_id)["status"] == "approved"
    assert store.list_tokens()[0].status == "approved"
    grant = redis.pushed[-1][1]["payload"]
    assert grant["event"] == GRANT_EVENT
    assert grant["data"]["profile_verified"] is True
    assert "owner bound" in buzz.replies[-1][1]
    assert "auth-tag ceremony remains manual" in buzz.replies[-1][1]
    assert buzz.timeline == ["react", "provision", "verify_profile", "bind_owner"]


def test_configured_owner_auth_tag_is_provisioned_and_reported(tmp_path):
    registrar, store, redis, buzz, secret, pubkey, compressed = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    buzz.auth_tag = '["auth","owner","kind=0","signature"]'
    buzz.owner_auth_enabled = True
    buzz.thread_messages = [{"pubkey": buzz.mark_pubkey, "content": f"approve {request_id}"}]

    registrar.poll_approvals()

    provision = redis.pushed[-1][1]["payload"]
    assert provision["event"] == PROVISION_EVENT
    assert provision["data"]["auth_tag"] == buzz.auth_tag

    ready = signed_data(PROFILE_READY_EVENT, {
        "request_id": request_id, "pubkey": pubkey, "profile_name": "throwaway",
        "client_nonce": "nonce", "issued_at": "now",
    }, secret, compressed)
    registrar.handle(notify_envelope("reply", "registrar", PROFILE_READY_EVENT, ready))
    assert "NIP-OA kind-0 owner auth tag supplied" in buzz.replies[-1][1]


def test_new_seat_owner_bind_waits_until_profile_creates_users_row(tmp_path):
    registrar, store, _, buzz, secret, pubkey, compressed = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    buzz.thread_messages = [{"pubkey": buzz.mark_pubkey, "content": f"approve {request_id}"}]

    registrar.poll_approvals()

    assert buzz.timeline == ["react", "provision"]
    assert buzz.profile_visible is False

    ready = signed_data(PROFILE_READY_EVENT, {
        "request_id": request_id, "pubkey": pubkey, "profile_name": "throwaway",
        "client_nonce": "nonce", "issued_at": "now",
    }, secret, compressed)
    registrar.handle(notify_envelope("reply", "registrar", PROFILE_READY_EVENT, ready))

    assert buzz.timeline == ["react", "provision", "verify_profile", "bind_owner"]
    assert store.request(request_id)["status"] == "approved"


def test_mark_check_reaction_approves_and_eyes_precedes_provision(tmp_path):
    registrar, store, _, buzz, _, pubkey, _ = _pending_registration(tmp_path)
    buzz.reaction_items = {"✅": {buzz.mark_pubkey}}
    registrar.poll_approvals()
    request = store.pending()[0]
    assert request["decision"] == "approve"
    assert request["decision_source"] == "reaction:✅"
    assert buzz.reacted == [("event-1", "👀")]
    assert buzz.timeline[:2] == ["react", "provision"]
    assert buzz.provisioned == [(pubkey, ["chan"])]


def test_first_reaction_wins_and_contradiction_is_noted(tmp_path):
    registrar, store, _, buzz, _, _, _ = _pending_registration(tmp_path)
    buzz.reaction_items = {
        "✅": {buzz.mark_pubkey},
        "❌": {buzz.mark_pubkey},
    }
    registrar.poll_approvals()
    request = store.pending()[0]
    assert request["decision"] == "approve"
    assert request["contradiction_noted"] == 1
    assert "contradictory signal" in buzz.replies[0][1]


def test_provision_failure_is_bounded_and_reported_to_thread_and_client(tmp_path):
    registrar, store, redis, buzz, _, _, _ = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    store.set_decision(request_id, "approve", "reply:event")

    def fail(*args):
        raise RuntimeError("plain-text-shape regression")

    buzz.provision = fail
    for _ in range(5):
        registrar._approve(store.request(request_id))
    request = store.request(request_id)
    assert request["status"] == "provision_failed"
    assert request["provision_attempts"] == 5
    assert store.list_tokens()[0].status == "denied"
    assert "exhausted 5 attempts" in buzz.replies[-1][1]
    assert redis.pushed[-1][1]["payload"]["event"] == DENY_EVENT


def test_provision_failure_names_channel_in_every_thread_outcome(tmp_path):
    registrar, store, redis, buzz, _, _, _ = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    store.set_decision(request_id, "approve", "reply:event")

    class ChannelFailure(RuntimeError):
        channel = "databases"

    buzz.provision = lambda *args: (_ for _ in ()).throw(ChannelFailure())
    for attempt in range(5):
        registrar._approve(store.request(request_id))
        assert "channel `databases`" in buzz.replies[-1][1]

    deny = redis.pushed[-1][1]["payload"]
    assert "channel `databases`" in deny["data"]["reason"]


def test_accepted_approval_is_not_reclassified_as_timeout(tmp_path):
    registrar, store, _, buzz, _, _, _ = _pending_registration(tmp_path)
    request_id = store.pending()[0]["id"]
    with store.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE requests SET created_at=? WHERE id=?",
            (iso(utcnow() - timedelta(hours=25)), request_id),
        )
    store.set_decision(request_id, "approve", "reply:event")
    registrar.poll_approvals()
    assert store.request(request_id)["status"] == "provisioned"
    assert buzz.provisioned
