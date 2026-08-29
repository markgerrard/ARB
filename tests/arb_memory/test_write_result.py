import json

import pytest

from arb_memory import bus


class _RunRedis:
    def __init__(self, entries):
        self.entries = list(entries)
        self.acked = []
        self.published = []
        self.expirations = []
        self.loop = None

    def xgroup_create(self, *args, **kwargs):
        pass

    def xreadgroup(self, group, consumer, streams, **kwargs):
        if next(iter(streams.values())) == "0":
            return []
        if self.entries:
            return [(next(iter(streams)), [self.entries.pop(0)])]
        self.loop.stop()
        return []

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)

    def lpush(self, key, value):
        self.published.append((key, json.loads(value)))

    def expire(self, key, ttl):
        self.expirations.append((key, ttl))


def test_write_loop_run_publishes_requested_results_and_leaves_unrequested_writes_silent(monkeypatch):
    receipt = {
        "artefact_outcome": "stored",
        "artefact_id": "d.md",
        "version": 1,
        "hints_stored": 0,
    }
    entries = [
        ("1-0", {"ulid": "U1", "request_id": "request-1", "payload": json.dumps({"ulid": "U1"})}),
        ("2-0", {"ulid": "U2", "payload": json.dumps({"ulid": "U2"})}),
        ("3-0", {"ulid": "U3", "request_id": "request-3", "payload": json.dumps({"ulid": "U3"})}),
        ("4-0", {"ulid": "U4", "request_id": "request-4", "payload": "{"}),
    ]
    redis = _RunRedis(entries)
    loop = bus.WriteLoop(redis, lambda: object(), embed=lambda text: [], prefix="test:")
    redis.loop = loop

    monkeypatch.setattr(loop, "_deadletter", lambda *args: True)
    monkeypatch.setattr(
        bus,
        "handle_write_intent",
        lambda _conn, intent, **kwargs: (receipt, intent["ulid"] == "U3"),
    )

    loop.run()

    assert redis.published == [
        (bus.write_result_key("request-1", "test:"), {**receipt, "duplicate": False}),
        (bus.write_result_key("request-3", "test:"), {**receipt, "duplicate": True}),
        (
            bus.write_result_key("request-4", "test:"),
            {"artefact_outcome": "failed", "reason": "deadlettered", "duplicate": False},
        ),
    ]
    assert redis.expirations == [(key, bus.WRITE_RESULT_TTL) for key, _ in redis.published]
    assert redis.acked == ["1-0", "2-0", "3-0", "4-0"]


def test_memory_write_places_request_id_in_the_top_level_stream_fields(redis_bus):
    ulid = bus.memory_write(
        redis_bus,
        hints=[{"text": "t"}],
        request_id="request-1",
        prefix=redis_bus.prefix,
    )

    _, fields = redis_bus.xrange(bus.writes_stream(redis_bus.prefix))[0]
    assert fields["ulid"] == ulid
    assert fields["request_id"] == "request-1"
    assert "request_id" not in json.loads(fields["payload"])


def test_write_loop_acks_refusal_on_first_delivery(monkeypatch):
    """AC5: refused_version_mismatch is a receipt (acked once), not a deadletter.

    Exercises the real handle_write_intent path: only write_artefact_and_hints is
    stubbed to return the refusal; idempotency claim + receipt persist run for real
    against a fake conn. Fails if the loop treats a non-exception refusal as poison.
    """
    from contextlib import nullcontext

    refusal = {
        "artefact_outcome": "refused_version_mismatch",
        "artefact_id": "doc",
        "version": 2,
        "hints_stored": 0,
    }

    class _FakeResult:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _IdempotencyConn:
        def __init__(self):
            self.receipt = None
            self.closed = False

        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            q = query if isinstance(query, str) else str(query)
            if "INSERT INTO idempotency_keys" in q:
                return _FakeResult(("U-refuse",))
            if "UPDATE idempotency_keys SET receipt" in q:
                self.receipt = params[0]
                return _FakeResult(None)
            raise AssertionError(f"unexpected query: {q}")

    entries = [
        (
            "1-0",
            {
                "ulid": "U-refuse",
                "request_id": "request-refuse",
                "payload": json.dumps({
                    "ulid": "U-refuse",
                    "artefact": {
                        "artefact_id": "doc",
                        "content": "C",
                        "expected_version": 1,
                    },
                    "hints": [],
                }),
            },
        ),
    ]
    conn = _IdempotencyConn()
    redis = _RunRedis(entries)
    loop = bus.WriteLoop(redis, lambda: conn, embed=lambda text: [], prefix="test:")
    redis.loop = loop

    monkeypatch.setattr(loop, "_deadletter", lambda *args: True)
    # Stub only the store write — handle_write_intent itself must run.
    import arb_memory.store as store_mod

    monkeypatch.setattr(
        store_mod,
        "write_artefact_and_hints",
        lambda _conn, **kwargs: refusal,
    )

    loop.run()

    assert redis.acked == ["1-0"]
    assert redis.published == [
        (
            bus.write_result_key("request-refuse", "test:"),
            {**refusal, "duplicate": False},
        ),
    ]
    assert conn.receipt is not None  # real handle_write_intent persisted the receipt



def test_memory_write_refuses_expected_version_without_result_channel(redis_bus):
    """AC15: third door — expected_version with no request_id raises; XADD nothing."""
    stream = bus.writes_stream(redis_bus.prefix)
    before = len(redis_bus.xrange(stream))

    with pytest.raises(ValueError, match="expected_version requires a result channel"):
        bus.memory_write(
            redis_bus,
            artefact={
                "artefact_id": "doc",
                "content": "C",
                "expected_version": 3,
            },
            prefix=redis_bus.prefix,
        )

    assert len(redis_bus.xrange(stream)) == before
