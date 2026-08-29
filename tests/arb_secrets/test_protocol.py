from types import SimpleNamespace

import pytest

from arb_crypto import box_open, generate_keypair
from arb_secrets.keystore import KeyStore
from arb_secrets.protocol import (
    AuthFail,
    Expired,
    Replay,
    SEEN_TTL,
    Unauthorized,
    WrongAnswer,
    WrongHolder,
    accept_drop,
    accept_reply,
    accept_request,
    build_drop,
    build_reply,
    build_request,
)
from arb_secrets.transport import Transport


class FakeRedis(dict):
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self:
            return False
        self[k] = v
        return True

    def get(self, k):
        return dict.get(self, k)

    def getdel(self, k):
        return self.pop(k, None)

    def delete(self, k):
        return 1 if self.pop(k, None) is not None else 0

    def lpush(self, k, v):
        self.setdefault(k, []).insert(0, v)


@pytest.fixture
def env(tmp_path):
    r = FakeRedis()
    transport = Transport(r)
    peers = {}
    for name in ("lead", "node", "node2", "spoofer"):
        priv, pub = generate_keypair()
        ks = KeyStore(r, name, tmp_path / f"{name}-pins.b64")
        ks.privkey = priv
        ks.publish(pub)
        peers[name] = (priv, pub, ks)
    return SimpleNamespace(
        redis=r,
        transport=transport,
        lead_priv=peers["lead"][0],
        lead_pub=peers["lead"][1],
        lead_ks=peers["lead"][2],
        node_priv=peers["node"][0],
        node_pub=peers["node"][1],
        node_ks=peers["node"][2],
        node2_priv=peers["node2"][0],
        node2_pub=peers["node2"][1],
        node2_ks=peers["node2"][2],
        spoofer_priv=peers["spoofer"][0],
        spoofer_pub=peers["spoofer"][1],
        spoofer_ks=peers["spoofer"][2],
    )


def test_reply_binds_question_wrong_secret_under_right_req_id_rejected(env):
    r1, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    r2, _, _ = build_request(env.node_priv, "node", "lead", "API_KEY", 3600, env.node_ks)
    id1 = r1["payload"]["data"]["req_id"]
    id2 = r2["payload"]["data"]["req_id"]
    outstanding = {
        id1: {"holder": "lead", "what": "DB_URL"},
        id2: {"holder": "lead", "what": "API_KEY"},
    }
    reply_env, _, ct = build_reply(
        env.lead_priv,
        "lead",
        {"from": "node", "req_id": id1, "what": "API_KEY"},
        b"the-api-key",
        3600,
        env.lead_ks,
    )
    with pytest.raises(WrongAnswer):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, outstanding)


def test_reply_from_wrong_but_pinned_holder_rejected(env):
    req, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    rid = req["payload"]["data"]["req_id"]
    outstanding = {rid: {"holder": "lead", "what": "DB_URL"}}
    reply_env, _, ct = build_reply(
        env.node2_priv,
        "node2",
        {"from": "node", "req_id": rid, "what": "DB_URL"},
        b"evil",
        3600,
        env.node2_ks,
    )
    with pytest.raises(WrongHolder):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, outstanding)


def test_reply_sealed_to_pinned_key_ignores_supplied_reply_to(env):
    req = {"from": "node", "req_id": "q1", "what": "DB_URL", "reply_to_pubkey": env.spoofer_pub}
    reply_env, _, ct = build_reply(env.lead_priv, "lead", req, b"sec", 3600, env.lead_ks)
    with pytest.raises(Exception):
        box_open(ct, env.spoofer_priv, env.lead_pub)
    assert b"sec" in box_open(ct, env.node_priv, env.lead_pub)
    assert reply_env["payload"]["data"]["to"] == "node"


def test_replayed_reply_rejected(env):
    req, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    rid = req["payload"]["data"]["req_id"]
    outstanding = {rid: {"holder": "lead", "what": "DB_URL"}}
    reply_env, _, ct = build_reply(
        env.lead_priv,
        "lead",
        {"from": "node", "req_id": rid, "what": "DB_URL"},
        b"s",
        3600,
        env.lead_ks,
    )
    accept_reply(reply_env, ct, "node", env.node_ks, env.transport, dict(outstanding))
    with pytest.raises(Replay):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, dict(outstanding))


def test_expired_drop_rejected(env):
    d_env, _, ct = build_drop(env.lead_priv, "lead", "node", b"s", -1, env.lead_ks)
    with pytest.raises(Expired):
        accept_drop(d_env, ct, "node", env.node_ks, env.transport)


def test_unauthorized_requester_denied(env):
    req, _, ct = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    with pytest.raises(Unauthorized):
        accept_request(req, ct, "lead", env.lead_ks, env.transport, allowed_requesters=set())


def test_ttl_longer_than_seen_window_refused(env):
    with pytest.raises(ValueError):
        build_drop(env.lead_priv, "lead", "node", b"s", SEEN_TTL + 1, env.lead_ks)


def test_drop_rejects_mismatched_outer_inner_id(env):
    d_env, _, ct = build_drop(env.lead_priv, "lead", "node", b"s", 3600, env.lead_ks)
    d_env["payload"]["data"]["msg_id"] = "wrong-outer-id"

    with pytest.raises(AuthFail):
        accept_drop(d_env, ct, "node", env.node_ks, env.transport)


def test_reply_rejects_mismatched_outer_inner_request_id(env):
    req, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    rid = req["payload"]["data"]["req_id"]
    outstanding = {rid: {"holder": "lead", "what": "DB_URL", "expires_at": req["payload"]["data"]["expires_at"]}}
    reply_env, _, ct = build_reply(
        env.lead_priv,
        "lead",
        {"from": "node", "req_id": rid, "what": "DB_URL"},
        b"s",
        3600,
        env.lead_ks,
    )
    reply_env["payload"]["data"]["in_reply_to"] = "wrong-outer-id"

    with pytest.raises(AuthFail):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, outstanding)
