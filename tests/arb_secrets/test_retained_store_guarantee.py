import json
import os
import uuid

import pytest

from arb_secrets.peer import Peer


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

    def lrange(self, k, start, end):
        values = self.get(k) or []
        if end == -1:
            end = len(values) - 1
        return values[start : end + 1]

    def rpop(self, k):
        values = self.get(k) or []
        if not values:
            return None
        return values.pop()


@pytest.fixture
def dev_redis():
    pytest.importorskip("redis")
    if not os.environ.get("ARB_DEV_REDIS_URL"):
        pytest.skip("needs dev redis")
    import redis

    suffix = uuid.uuid4().hex
    r = redis.Redis.from_url(os.environ["ARB_DEV_REDIS_URL"])
    yield r, suffix
    for key in r.scan_iter(f"*{suffix}*"):
        r.delete(key)


def two_peers(dev_redis, tmp_path):
    r, suffix = dev_redis
    peer_a = Peer(
        r,
        f"secrets-a-{suffix}",
        tmp_path / "a.key",
        tmp_path / "a-pins.b64",
        allowed_requesters={f"secrets-b-{suffix}"},
    )
    peer_b = Peer(
        r,
        f"secrets-b-{suffix}",
        tmp_path / "b.key",
        tmp_path / "b-pins.b64",
        allowed_requesters={f"secrets-a-{suffix}"},
    )
    return peer_a, peer_b


def two_fake_peers(tmp_path):
    r = FakeRedis()
    suffix = uuid.uuid4().hex
    peer_a = Peer(
        r,
        f"secrets-a-{suffix}",
        tmp_path / "a.key",
        tmp_path / "a-pins.b64",
        allowed_requesters={f"secrets-b-{suffix}"},
    )
    peer_b = Peer(
        r,
        f"secrets-b-{suffix}",
        tmp_path / "b.key",
        tmp_path / "b-pins.b64",
        allowed_requesters={f"secrets-a-{suffix}"},
    )
    return peer_a, peer_b


def assert_pointer_only(raw, *forbidden_bytes):
    assert len(raw) >= 1, "vacuous pass guard: the pointer must actually land in the inbox"
    inbox_bytes = b"".join(x if isinstance(x, bytes) else x.encode() for x in raw)
    for value in forbidden_bytes:
        assert value not in inbox_bytes
    for item in raw:
        env = json.loads(item)
        assert env["kind"] == "notify"
        data = env["payload"]["data"]
        assert "blob_key" in data
        assert not any(k in data for k in ("secret", "what", "echo_what", "ciphertext"))


def test_no_secret_bytes_reach_inbox_fake_default(tmp_path):
    peer_a, peer_b = two_fake_peers(tmp_path)
    secret = b"SUPER-SECRET-CRED-" + uuid.uuid4().hex.encode()
    blob_id = peer_a.push_secret(peer_b.agent_id, secret, ttl=60)
    ciphertext = peer_a.redis[f"agent_scratch:secrets:blob:{peer_b.agent_id}:{blob_id}"]

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_b.agent_id}:inbox", 0, -1)

    assert_pointer_only(raw, secret, ciphertext)


def test_no_what_reaches_inbox_on_request_fake_default(tmp_path):
    peer_a, peer_b = two_fake_peers(tmp_path)
    what = f"prod-stripe-key-acct-{uuid.uuid4().hex}"
    req_id = peer_a.request_secret(peer_b.agent_id, what, ttl=60)
    ciphertext = peer_a.redis[f"agent_scratch:secrets:blob:{peer_b.agent_id}:{req_id}"]

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_b.agent_id}:inbox", 0, -1)

    assert_pointer_only(raw, what.encode(), ciphertext)


def test_no_reply_body_reaches_inbox_fake_default(tmp_path):
    peer_a, peer_b = two_fake_peers(tmp_path)
    what = f"prod-api-key-{uuid.uuid4().hex}"
    secret = b"REPLY-SECRET-" + uuid.uuid4().hex.encode()
    peer_a.request_secret(peer_b.agent_id, what, ttl=60)
    requests = peer_b.claim_incoming()
    reply_id = peer_b.respond_to_request(requests[0], secret, ttl=60)
    ciphertext = peer_b.redis[f"agent_scratch:secrets:blob:{peer_a.agent_id}:{reply_id}"]

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_a.agent_id}:inbox", 0, -1)

    assert_pointer_only(raw, secret, what.encode(), ciphertext)


def test_no_secret_bytes_reach_inbox(dev_redis, tmp_path):
    _, suffix = dev_redis
    peer_a, peer_b = two_peers(dev_redis, tmp_path)
    secret = b"SUPER-SECRET-CRED-" + suffix.encode()
    peer_a.push_secret(peer_b.agent_id, secret, ttl=60)

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_b.agent_id}:inbox", 0, -1)
    assert len(raw) >= 1, "vacuous pass guard: the pointer must actually land in the inbox"
    blob = b"".join(x if isinstance(x, bytes) else x.encode() for x in raw)
    assert secret not in blob
    for item in raw:
        env = json.loads(item)
        assert env["kind"] == "notify"
        data = env["payload"]["data"]
        assert "blob_key" in data
        assert not any(k in data for k in ("secret", "what", "echo_what", "ciphertext"))


def test_no_what_reaches_inbox_on_request(dev_redis, tmp_path):
    _, suffix = dev_redis
    peer_a, peer_b = two_peers(dev_redis, tmp_path)
    what = f"prod-stripe-key-acct-{suffix}"
    peer_a.request_secret(peer_b.agent_id, what, ttl=60)

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_b.agent_id}:inbox", 0, -1)
    assert len(raw) >= 1
    blob = b"".join(x if isinstance(x, bytes) else x.encode() for x in raw).lower()
    assert what.encode().lower() not in blob
    for item in raw:
        data = json.loads(item)["payload"]["data"]
        assert "blob_key" in data
        assert not any(k in data for k in ("secret", "what", "echo_what", "ciphertext"))


def test_no_reply_body_reaches_inbox(dev_redis, tmp_path):
    _, suffix = dev_redis
    peer_a, peer_b = two_peers(dev_redis, tmp_path)
    what = f"prod-api-key-{suffix}"
    secret = b"REPLY-SECRET-" + suffix.encode()
    peer_a.request_secret(peer_b.agent_id, what, ttl=60)
    requests = peer_b.claim_incoming()
    peer_b.respond_to_request(requests[0], secret, ttl=60)

    raw = peer_a.redis.lrange(f"agent_scratch:agent:{peer_a.agent_id}:inbox", 0, -1)
    assert len(raw) >= 1
    blob = b"".join(x if isinstance(x, bytes) else x.encode() for x in raw).lower()
    assert secret.lower() not in blob
    assert what.encode().lower() not in blob
    for item in raw:
        data = json.loads(item)["payload"]["data"]
        assert "blob_key" in data
        assert not any(k in data for k in ("secret", "what", "echo_what", "ciphertext"))
