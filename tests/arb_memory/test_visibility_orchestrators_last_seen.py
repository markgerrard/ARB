from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_orchestrator_list
from arb_memory.visibility import build_visibility_app
from arb_memory.visibility import update_last_seen


RESOURCE = "https://mem.example.com"


class FakeRedis:
    def __init__(self, *, last_seen=None, stream_entries=None):
        self.last_seen = dict(last_seen or {})
        self.stream_entries = list(stream_entries or [])
        self.hgetall_calls = []
        self.hdel_calls = []
        self.xrevrange_calls = []

    def hgetall(self, key):
        self.hgetall_calls.append(key)
        return dict(self.last_seen)

    def hdel(self, key, *fields):
        self.hdel_calls.append((key, fields))
        for field in fields:
            self.last_seen.pop(field, None)

    def xrevrange(self, key, count=200):
        self.xrevrange_calls.append((key, count))
        return self.stream_entries[:count]


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _put_access_token(conn):
    token = f"vis-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=_future(),
    )
    return token


def _client(monkeypatch, fake):
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: fake)
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    return TestClient(app)


def test_build_orchestrator_list_filters_stale_and_sorts_by_recency():
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    hash_dict = {
        "A": (now - timedelta(minutes=5)).isoformat(),
        "B": (now - timedelta(minutes=1)).isoformat(),
        "C": (now - timedelta(hours=25)).isoformat(),
    }

    assert build_orchestrator_list(hash_dict, now, stale_secs=24 * 60 * 60) == ["B", "A"]


def test_build_orchestrator_list_keeps_crowded_orchestrators_from_hash():
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    hash_dict = {
        "chatty": (now - timedelta(seconds=1)).isoformat(),
        "quiet": (now - timedelta(seconds=5)).isoformat(),
    }

    assert build_orchestrator_list(hash_dict, now, stale_secs=60) == ["chatty", "quiet"]


def test_update_last_seen_newer_wins_and_older_is_ignored():
    older = "2026-07-04T11:59:00+00:00"
    newer = "2026-07-04T12:00:00+00:00"

    assert update_last_seen(older, newer) == newer
    assert update_last_seen(newer, older) == newer


def test_orchestrators_reads_hash_sorted_and_still_401s_without_token(scratch, monkeypatch):
    now = datetime.now(timezone.utc)
    fake = FakeRedis(
        last_seen={
            "older-orch": (now - timedelta(minutes=3)).isoformat(),
            "newer-orch": (now - timedelta(minutes=1)).isoformat(),
        },
        stream_entries=[
            ("200-0", {"orchestrator": "chatty", "sent_at": now.isoformat()}),
        ]
        * 200,
    )
    client = _client(monkeypatch, fake)
    token = _put_access_token(scratch)

    assert client.get("/orchestrators").status_code == 401
    response = client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"orchestrators": ["newer-orch", "older-orch"], "tees": []}
    assert fake.hgetall_calls == ["agent_scratch:orchestrators:last_seen"]
    assert fake.xrevrange_calls == []


def test_orchestrators_empty_hash_falls_back_to_tail_scan(scratch, monkeypatch):
    now = datetime.now(timezone.utc)
    fake = FakeRedis(
        last_seen={},
        stream_entries=[
            ("3-0", {"orchestrator": "chatty", "sent_at": now.isoformat()}),
            ("2-0", {"orchestrator": "chatty", "sent_at": now.isoformat()}),
            ("1-0", {"orchestrator": "quiet", "sent_at": (now - timedelta(minutes=1)).isoformat()}),
        ],
    )
    client = _client(monkeypatch, fake)
    token = _put_access_token(scratch)

    response = client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"orchestrators": ["chatty", "quiet"], "tees": []}
    assert fake.xrevrange_calls == [("agent_scratch:events:live", 200)]


class OutageRedis(FakeRedis):
    def hgetall(self, key):
        raise ConnectionError("valkey unreachable")

    def xrevrange(self, key, count=200):
        raise ConnectionError("valkey unreachable")


def test_orchestrators_redis_outage_returns_503_not_uncaught_500(scratch, monkeypatch):
    # VIS-2 (panel-confirmed): the hgetall failure was caught but fell through to
    # an UNGUARDED _orchestrators_from_tail() (xrevrange), which raised uncaught —
    # surfacing as a bare 500 instead of a clean 503.
    fake = OutageRedis()
    client = _client(monkeypatch, fake)
    token = _put_access_token(scratch)

    response = client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
