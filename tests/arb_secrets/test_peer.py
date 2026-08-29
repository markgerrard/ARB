import time
import stat

from arb_secrets.peer import Peer


class FakeRedis(dict):
    @property
    def expires(self):
        if not hasattr(self, "_expires"):
            self._expires = {}
        return self._expires

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self:
            return False
        self[k] = v
        self.expires[k] = ex
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


def test_push_then_claim_returns_secret(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64")
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    lead.push_secret("node", b"DB_URL=postgres://secret", ttl=3600)

    incoming = node.claim_incoming()
    assert len(incoming) == 1
    assert incoming[0].event == "secret_drop"
    assert incoming[0].secret == b"DB_URL=postgres://secret"


def test_request_respond_then_claim_returns_matching_secret(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64", allowed_requesters={"node"})
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    req_id = node.request_secret("lead", "DB_URL", ttl=3600)
    requests = lead.claim_incoming()
    assert len(requests) == 1
    assert requests[0].event == "secret_request"
    assert requests[0].what == "DB_URL"
    assert requests[0].meta["req_id"] == req_id

    lead.respond_to_request(requests[0], b"postgres://secret", ttl=3600)

    replies = node.claim_incoming()
    assert len(replies) == 1
    assert replies[0].event == "secret_reply"
    assert replies[0].what == "DB_URL"
    assert replies[0].secret == b"postgres://secret"


def test_bad_reply_does_not_drop_valid_cobatched_secret(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64")
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    reply_id = lead.respond_to_request(
        type("IncomingRequest", (), {"meta": {"from": "node", "req_id": "bogus", "what": "DB_URL"}})(),
        b"wrong",
        ttl=3600,
    )
    lead.push_secret("node", b"DB_URL=postgres://secret", ttl=3600)

    incoming = node.claim_incoming()

    assert [item.event for item in incoming] == ["secret_reply", "secret_drop"]
    assert incoming[0].meta["rejection"] == "WrongAnswer"
    assert incoming[1].secret == b"DB_URL=postgres://secret"
    assert r["agent_scratch:secrets:status:lead:" + reply_id] == "rejected"


def test_replayed_drop_sets_rejected_status(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64")
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    blob_id = lead.push_secret("node", b"DB_URL=postgres://secret", ttl=3600)
    envelope = r["agent_scratch:agent:node:inbox"][0]
    ciphertext = r["agent_scratch:secrets:blob:node:" + blob_id]
    assert node.claim_incoming()[0].secret == b"DB_URL=postgres://secret"

    r.lpush("agent_scratch:agent:node:inbox", envelope)
    r["agent_scratch:secrets:blob:node:" + blob_id] = ciphertext

    incoming = node.claim_incoming()

    assert len(incoming) == 1
    assert incoming[0].meta["rejection"] == "Replay"
    assert r["agent_scratch:secrets:status:lead:" + blob_id] == "rejected"


def test_reply_for_expired_outstanding_request_is_rejected(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64", allowed_requesters={"node"})
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    req_id = node.request_secret("lead", "DB_URL", ttl=3600)
    requests = lead.claim_incoming()
    node.outstanding[req_id]["expires_at"] = time.time() - 1
    lead.respond_to_request(requests[0], b"postgres://secret", ttl=3600)

    incoming = node.claim_incoming()

    assert len(incoming) == 1
    assert incoming[0].meta["rejection"] == "Expired"
    assert req_id not in node.outstanding


def test_peer_bootstrap_creates_mode_600_private_key(tmp_path):
    r = FakeRedis()
    path = tmp_path / "node.key"

    Peer(r, "node", path, tmp_path / "node-pins.b64")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_claimed_status_ttl_tracks_transfer_ttl(tmp_path):
    r = FakeRedis()
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64")
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64")

    blob_id = lead.push_secret("node", b"DB_URL=postgres://secret", ttl=120)
    node.claim_incoming()

    status_key = "agent_scratch:secrets:status:lead:" + blob_id
    assert 1 <= r.expires[status_key] <= 120


def test_audit_includes_pinned_peer_fingerprint(tmp_path):
    r = FakeRedis()
    audit = []
    lead = Peer(r, "lead", tmp_path / "lead.key", tmp_path / "lead-pins.b64", audit_sink=audit.append)
    node = Peer(r, "node", tmp_path / "node.key", tmp_path / "node-pins.b64", audit_sink=audit.append)

    lead.push_secret("node", b"DB_URL=postgres://secret", ttl=3600)
    node.claim_incoming()

    sent = audit[0]
    claimed = audit[1]
    assert sent["fingerprints"]["node"] == lead.keystore.pin_fingerprint("node")
    assert claimed["fingerprints"]["lead"] == node.keystore.pin_fingerprint("lead")
