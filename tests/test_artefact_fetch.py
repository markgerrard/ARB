import json
from datetime import datetime, timezone

import psycopg

from arb_memory import fetch
from arb_memory.bus import MemoryConsumer


class FakeRedis:
    def __init__(self):
        self.results = []
        self.expirations = []
        self.acks = []
        self.group_creates = []
        self.xadds = []
        self.popped = None

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        self.group_creates.append((stream, group, id, mkstream))

    def xadd(self, stream, fields, maxlen=None, approximate=None):
        self.xadds.append((stream, fields, maxlen, approximate))
        return "1-0"

    def blpop(self, key, timeout):
        return self.popped

    def lpush(self, key, value):
        self.results.append((key, json.loads(value)))

    def expire(self, key, seconds):
        self.expirations.append((key, seconds))

    def xack(self, stream, group, entry_id):
        self.acks.append((stream, group, entry_id))


class FakeCursor:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error

    def fetchone(self):
        if self.error:
            raise self.error
        return self.row


class FakeConn:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.closed = False
        self.queries = []

    def execute(self, query, params):
        self.queries.append((query, params))
        if self.error:
            raise self.error
        return FakeCursor(self.row)

    def close(self):
        self.closed = True


def _handle(row, fields=None):
    redis = FakeRedis()
    conn = FakeConn(row)
    consumer = fetch.FetchConsumer(redis, lambda: conn, prefix="fleet:")
    result = consumer._handle_entry(
        "1-0",
        fields or {"request_id": "req-1", "artefact_id": "art-1"},
    )
    return result, redis, conn


def test_client_writes_prefixed_request_and_decodes_result(monkeypatch):
    redis = FakeRedis()
    redis.popped = ("result-key", json.dumps({"outcome": "not_found"}))
    monkeypatch.setattr(fetch.uuid, "uuid4", lambda: type("U", (), {"hex": "request-hex"})())

    result = fetch.memory_fetch_by_id(redis, "art-1", version=3, timeout=2, prefix="fleet:")

    assert result == {"outcome": "not_found", "request_id": "request-hex"}
    assert redis.xadds == [
        (
            "fleet:arbmem:artefact:fetch_request",
            {"request_id": "request-hex", "artefact_id": "art-1", "version": "3"},
            10_000,
            True,
        )
    ]


def test_client_attaches_request_id_to_ok_and_transport_error(monkeypatch):
    monkeypatch.setattr(fetch.uuid, "uuid4", lambda: type("U", (), {"hex": "request-hex"})())
    redis = FakeRedis()
    redis.popped = ("result-key", json.dumps({"outcome": "ok", "content": "body"}))
    assert fetch.memory_fetch_by_id(redis, "art-1") == {
        "outcome": "ok",
        "content": "body",
        "request_id": "request-hex",
    }

    class BrokenRedis(FakeRedis):
        def xadd(self, *args, **kwargs):
            raise fetch.redis.ConnectionError("down")

    assert fetch.memory_fetch_by_id(BrokenRedis(), "art-1") == {
        "outcome": "request_unsent",
        "request_id": "request-hex",
    }


def test_client_transport_legs_are_distinguishable_and_are_never_infra_exhausted(monkeypatch):
    """A failed send and a failed receive are different states and must not share a word.

    infra_exhausted is the STORE saying it tried and gave up (emitted only by the consumer).
    Reporting a client-side transport fault as infra_exhausted is what sent a caller chasing
    artefact-store capacity on 2026-08-08 for a fault that was never in the store.
    """
    monkeypatch.setattr(fetch.uuid, "uuid4", lambda: type("U", (), {"hex": "request-hex"})())

    class SendFails(FakeRedis):
        def xadd(self, *args, **kwargs):
            raise fetch.redis.ConnectionError("xadd down")

    class ReceiveFails(FakeRedis):
        def blpop(self, key, timeout):
            raise fetch.redis.ConnectionError("blpop down")

    sent = ReceiveFails()
    assert fetch.memory_fetch_by_id(SendFails(), "art-1")["outcome"] == "request_unsent"
    assert fetch.memory_fetch_by_id(sent, "art-1")["outcome"] == "result_unreadable"
    # result_unreadable must mean the request really did go out — that is the whole difference.
    assert sent.xadds


def test_ok_head_result_has_complete_text_envelope():
    created = datetime(2026, 7, 20, tzinfo=timezone.utc)
    row = ("art-1", 4, "body", None, "text/markdown", "server-hash", "author-1", created)

    result, redis, conn = _handle(row)

    assert result == {
        "outcome": "ok",
        "artefact_id": "art-1",
        "version": 4,
        "content": "body",
        "content_mime": "text/markdown",
        "content_hash": "server-hash",
        "author": "author-1",
        "created_at": created,
    }
    assert redis.results[0][1]["created_at"] == "2026-07-20 00:00:00+00:00"
    assert "ORDER BY version DESC" in conn.queries[0][0]
    assert redis.acks == [("fleet:arbmem:artefact:fetch_request", fetch.GROUP, "1-0")]
    assert redis.expirations == [
        ("fleet:arbmem:artefact:fetch_result:req-1", fetch.RESULT_TTL_SECONDS)
    ]


def test_zero_rows_is_not_found_and_acked():
    result, redis, _ = _handle(None)

    assert result == {"outcome": "not_found"}
    assert redis.results[0][1] == {"outcome": "not_found"}
    assert redis.acks


def test_malformed_request_returns_malformed_and_acks():
    result, redis, conn = _handle(None, {"request_id": "req-bad", "version": "x"})

    assert result == "malformed"
    assert redis.results[0][1] == {"outcome": "malformed"}
    assert redis.acks
    assert conn.queries == []


def test_binary_row_returns_binary_unsupported():
    row = ("art-bin", 1, None, b"\x00\x01", "application/octet-stream", "hash", "a", "now")

    result, redis, _ = _handle(row)

    assert result == {"outcome": "binary_unsupported"}
    assert redis.results[0][1] == {"outcome": "binary_unsupported"}
    assert redis.acks


def test_db_fault_exhausts_to_infra_never_not_found(monkeypatch):
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    redis = FakeRedis()
    conn = FakeConn(error=psycopg.DataError("bad query"))
    consumer = fetch.FetchConsumer(redis, lambda: conn, prefix="fleet:")

    result = consumer._handle_entry(
        "2-0", {"request_id": "req-infra", "artefact_id": "art-1"}
    )

    assert result is True
    assert redis.results == [
        ("fleet:arbmem:artefact:fetch_result:req-infra", {"outcome": "infra_exhausted"})
    ]
    assert all(envelope != {"outcome": "not_found"} for _, envelope in redis.results)
    assert redis.acks == [("fleet:arbmem:artefact:fetch_request", fetch.GROUP, "2-0")]


def test_transient_db_fault_recirculates_without_result_or_exhaustion(monkeypatch):
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    redis = FakeRedis()
    conn = FakeConn(error=psycopg.OperationalError("database unavailable"))
    consumer = fetch.FetchConsumer(redis, lambda: conn, prefix="fleet:")
    fields = {"request_id": "req-transient", "artefact_id": "art-transient"}

    results = [consumer._handle_entry("2-transient", fields) for _ in range(3)]

    assert results == [None, None, None]
    assert redis.results == []
    assert redis.acks == []
    assert consumer._poison == {}


def test_poison_exhaustion_uses_noop_deadletter_and_acks(monkeypatch):
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    redis = FakeRedis()
    conn = FakeConn(error=psycopg.DataError("poison"))
    consumer = fetch.FetchConsumer(redis, lambda: conn, prefix="fleet:")

    consumer._handle_entry(
        "3-0", {"request_id": "req-poison", "artefact_id": "art-poison"}
    )

    assert len(conn.queries) == 1
    assert redis.results[0][1] == {"outcome": "infra_exhausted"}
    assert redis.acks == [("fleet:arbmem:artefact:fetch_request", fetch.GROUP, "3-0")]
    assert not consumer._deadletter_sink_open


def test_memory_consumer_fetch_loop_opens_its_own_connection():
    redis = FakeRedis()
    connections = [FakeConn(), FakeConn(), FakeConn(None)]
    made = []

    def conn_factory():
        conn = connections[len(made)]
        made.append(conn)
        return conn

    consumer = MemoryConsumer(redis, conn_factory, embed=lambda text: [], prefix="fleet:")
    consumer.fetch_loop._handle_entry(
        "4-0", {"request_id": "req-own-conn", "artefact_id": "art-missing"}
    )

    assert consumer.write_loop.conn is connections[0]
    assert consumer.read_loop.conn is connections[1]
    assert made[2] is connections[2]
    assert connections[2].closed
    assert redis.results[-1][1] == {"outcome": "not_found"}
