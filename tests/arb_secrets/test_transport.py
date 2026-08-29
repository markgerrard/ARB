import json
import os

import pytest

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


def test_claim_blob_is_consume_once():
    t = Transport(FakeRedis())
    t.put_blob("b", "1", b"ct", 3600)
    assert t.claim_blob("b", "1") == b"ct"
    assert t.claim_blob("b", "1") is None


def test_status_transitions():
    t = Transport(FakeRedis())
    t.set_status("send", "1", "sent", 3600)
    assert t.get_status("send", "1") == "sent"
    t.set_status("send", "1", "claimed", 3600)
    assert t.get_status("send", "1") == "claimed"


def test_outstanding_roundtrip_and_delete():
    t = Transport(FakeRedis())
    t.put_outstanding("node", "r1", {"holder": "lead", "what": "DB_URL"}, 3600)
    assert t.get_outstanding("node", "r1") == {"holder": "lead", "what": "DB_URL"}
    t.del_outstanding("node", "r1")
    assert t.get_outstanding("node", "r1") is None


def test_push_pointer_uses_notify_inbox():
    r = FakeRedis()
    t = Transport(r)
    envelope = {"kind": "notify", "payload": {"event": "secret_drop", "data": {}}}
    t.push_pointer("node", envelope)
    assert json.loads(r["agent_scratch:agent:node:inbox"][0]) == envelope


def test_mark_seen_rejects_replay_fake():
    t = Transport(FakeRedis())
    assert t.mark_seen("self", "m1", 60) is True
    assert t.mark_seen("self", "m1", 60) is False


@pytest.mark.skipif(not os.environ.get("ARB_DEV_REDIS_URL"), reason="needs dev redis")
def test_mark_seen_rejects_replay_real_redis():
    redis = pytest.importorskip("redis")

    r = redis.Redis.from_url(os.environ["ARB_DEV_REDIS_URL"])
    t = Transport(r)
    mid = "m-" + os.urandom(6).hex()
    key = t.seen_key("self", mid)
    try:
        assert t.mark_seen("self", mid, 60) is True
        assert t.mark_seen("self", mid, 60) is False
    finally:
        r.delete(key)
