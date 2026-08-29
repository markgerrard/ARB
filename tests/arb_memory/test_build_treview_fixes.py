import json
from contextlib import contextmanager

import psycopg
import redis
from psycopg.types.json import Jsonb

import arb_memory.audit as audit
import arb_memory.bus as bus
import arb_memory.close as close
import arb_memory.consumer_loop as consumer_loop
from arb_memory.consumer_loop import POISON_RETRY_LIMIT, StreamConsumerLoop


class _GroupRedis:
    def __init__(self):
        self.acks = []

    def xgroup_create(self, *args, **kwargs):
        return None

    def xack(self, stream, group, entry_id):
        self.acks.append(entry_id)


class _DeadletterConn:
    closed = False

    def close(self):
        self.closed = True

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params=()):
        if "idempotency_keys" in query:
            raise psycopg.DataError("poison write")
        if "INSERT INTO write_deadletter" in query and params[-1] != "__canary__":
            raise psycopg.OperationalError("deadletter database failed over")
        return self

    def fetchone(self):
        return None


def test_transient_write_deadletter_failure_retains_entry_and_poison_budget():
    redis_client = _GroupRedis()
    conn = _DeadletterConn()
    loop = bus.WriteLoop(redis_client, lambda: conn, embed=lambda text: [])
    fields = {
        "ulid": "ulid-deadletter-transient",
        "payload": json.dumps({
            "ulid": "ulid-deadletter-transient",
            "kind": "hints",
            "artefact": None,
            "hints": [],
        }),
    }

    results = [loop._handle_entry("entry-1", fields) for _ in range(POISON_RETRY_LIMIT)]

    assert results[-1] is None
    assert redis_client.acks == []
    assert loop._poison["entry-1"] == POISON_RETRY_LIMIT


class _ReadAckRedis:
    def __init__(self):
        self.reads = []
        self.ack_stop_states = []
        self.fail_ack = True
        self.owner = None

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == "0":
            return []
        entry_id = f"{len([mode for mode in self.reads if mode == '>'])}-0"
        return [("stream", [(entry_id, {})])]

    def xack(self, stream, group, entry_id):
        if self.fail_ack:
            self.fail_ack = False
            self.ack_stop_states.append(self.owner._stop.is_set())
            raise redis.ConnectionError("transient XACK failure")
        self.owner.stop()


def test_read_xack_failure_backoffs_without_stopping_and_reads_again(monkeypatch):
    waits = []
    monkeypatch.setattr(bus, "backoff_delay", lambda failures: waits.append(failures) or 0)
    redis_client = _ReadAckRedis()
    loop = bus.ReadLoop(redis_client, lambda: object(), embed=lambda text: [], block_ms=0)
    redis_client.owner = loop

    loop.run()

    assert waits == [1]
    assert redis_client.ack_stop_states == [False]
    assert redis_client.reads.count(">") == 2


class _PendingReadAckRedis(_ReadAckRedis):
    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == "0" and self.fail_ack:
            return [("stream", [("pending-1", {})])]
        if mode == "0":
            return []
        return [("stream", [("new-1", {})])]


def test_read_startup_pending_xack_failure_enters_retry_path(monkeypatch):
    waits = []
    monkeypatch.setattr(bus, "backoff_delay", lambda failures: waits.append(failures) or 0)
    redis_client = _PendingReadAckRedis()
    loop = bus.ReadLoop(redis_client, lambda: object(), embed=lambda text: [], block_ms=0)
    redis_client.owner = loop

    loop.run()

    assert waits == [1]
    assert redis_client.reads == ["0", ">"]


def test_backoff_delay_large_failure_count_saturates_without_overflow():
    assert consumer_loop.backoff_delay(10_000) == 30


class _JsonbAdapterConn:
    closed = False

    def __init__(self):
        self.params = []

    def close(self):
        self.closed = True

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params=()):
        self.params.append(params)
        if any(isinstance(value, dict) for value in params):
            raise psycopg.ProgrammingError("cannot adapt type 'dict'")
        return self

    def fetchone(self):
        return None


def test_write_deadletter_canary_uses_jsonb_adapter_on_real_execute_path():
    conn = _JsonbAdapterConn()
    loop = bus.WriteLoop(_GroupRedis(), lambda: conn, embed=lambda text: [])

    assert loop._canary_deadletter_sink() is True
    assert isinstance(conn.params[-1][1], Jsonb)


def test_close_deadletter_canary_uses_jsonb_adapter_on_real_execute_path():
    conn = _JsonbAdapterConn()
    loop = close.CloseConsumer(_GroupRedis(), lambda: conn, block_ms=0)

    assert loop._canary_deadletter_sink() is True
    assert isinstance(conn.params[-1][0], Jsonb)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _StaleConn:
    closed = False

    def __init__(self, stale):
        self.stale = stale
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params=()):
        if "INSERT INTO idempotency_keys" in query:
            if self.stale:
                raise psycopg.OperationalError("stale connection after failover")
            return _Result(None)
        if "SELECT receipt FROM idempotency_keys" in query:
            return _Result(({"artefact_outcome": "stored"},))
        raise AssertionError(f"unexpected query: {query}")


def test_write_loop_reconnects_after_transient_stale_connection_error():
    redis_client = _GroupRedis()
    stale = _StaleConn(stale=True)
    fresh = _StaleConn(stale=False)
    connections = iter([stale, fresh])
    loop = bus.WriteLoop(redis_client, lambda: next(connections), embed=lambda text: [])
    fields = {
        "ulid": "ulid-after-failover",
        "payload": json.dumps({
            "ulid": "ulid-after-failover",
            "kind": "hints",
            "artefact": None,
            "hints": [],
        }),
    }

    assert loop._handle_entry("entry-1", fields) is None
    assert loop.conn is fresh
    assert stale.close_calls == 1
    assert loop._handle_entry("entry-1", fields) is True
    assert redis_client.acks == ["entry-1"]


def test_write_loop_keeps_working_conn_when_reconnect_factory_fails():
    redis_client = _GroupRedis()
    stale = _StaleConn(stale=True)
    calls = 0

    def conn_factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            return stale
        raise psycopg.OperationalError("database still unavailable")

    loop = bus.WriteLoop(redis_client, conn_factory, embed=lambda text: [])
    fields = {
        "ulid": "ulid-reconnect-factory-fails",
        "payload": json.dumps({
            "ulid": "ulid-reconnect-factory-fails",
            "kind": "hints",
            "artefact": None,
            "hints": [],
        }),
    }

    assert loop._handle_entry("entry-1", fields) is None
    assert loop.conn is stale
    assert stale.close_calls == 0
    assert redis_client.acks == []


def test_write_loop_reconnect_does_not_double_close_same_or_closed_conn():
    redis_client = _GroupRedis()
    same = _StaleConn(stale=True)
    same_loop = bus.WriteLoop(redis_client, lambda: same, embed=lambda text: [])

    assert same_loop._handle_entry("same-entry", {
        "ulid": "ulid-same-conn",
        "payload": json.dumps({
            "ulid": "ulid-same-conn",
            "kind": "hints",
            "artefact": None,
            "hints": [],
        }),
    }) is None
    assert same.close_calls == 0

    already_closed = _StaleConn(stale=True)
    already_closed.close()
    fresh = _StaleConn(stale=False)
    connections = iter([already_closed, fresh])
    closed_loop = bus.WriteLoop(redis_client, lambda: next(connections), embed=lambda text: [])

    assert closed_loop._handle_entry("closed-entry", {
        "ulid": "ulid-closed-conn",
        "payload": json.dumps({
            "ulid": "ulid-closed-conn",
            "kind": "hints",
            "artefact": None,
            "hints": [],
        }),
    }) is None
    assert already_closed.close_calls == 1
    assert closed_loop.conn is fresh


class _WriteRunRedis:
    def __init__(self, entries):
        self.entries = iter(entries)
        self.acks = []
        self.ack_stop_states = []
        self.owner = None

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        if next(iter(streams.values())) == "0":
            return []
        try:
            entry = next(self.entries)
        except StopIteration:
            return []
        return [("stream", [entry])]

    def xack(self, stream, group, entry_id):
        self.acks.append(entry_id)
        self.ack_stop_states.append(self.owner._stop.is_set())
        if entry_id == "valid-entry":
            self.owner.stop()


class _WritePathConn:
    closed = False

    def __init__(self):
        self.deadletters = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, query, params=()):
        if "INSERT INTO write_deadletter" in query:
            self.deadletters.append(params)
        return _Result(None)

    def close(self):
        self.closed = True


def test_noninfra_write_error_deadletters_acks_and_loop_continues(monkeypatch):
    bad_intent = {
        "ulid": "ulid-handler-value-error",
        "kind": "hints",
        "artefact": None,
        "hints": [{"text": "bad", "source": "test", "author": "test"}],
    }
    valid_intent = {
        "ulid": "ulid-after-handler-value-error",
        "kind": "hints",
        "artefact": None,
        "hints": [],
    }
    redis_client = _WriteRunRedis([
        ("bad-entry", {"ulid": bad_intent["ulid"], "payload": json.dumps(bad_intent)}),
        ("bad-entry", {"ulid": bad_intent["ulid"], "payload": json.dumps(bad_intent)}),
        ("valid-entry", {"ulid": valid_intent["ulid"], "payload": json.dumps(valid_intent)}),
    ])
    initial_conn = _WritePathConn()
    deadletter_conn = _WritePathConn()
    connections = iter([initial_conn, deadletter_conn])

    def embed(text):
        if text == "bad":
            raise ValueError("embedding input is invalid")
        return []

    monkeypatch.setattr("arb_memory.store._register_vector", lambda conn: None)
    loop = bus.WriteLoop(redis_client, lambda: next(connections), embed=embed, block_ms=0)
    redis_client.owner = loop
    loop._poison_retry_limit = 2

    loop.run()

    assert redis_client.acks == ["bad-entry", "valid-entry"]
    assert redis_client.ack_stop_states == [False, False]
    assert loop._stop.is_set()
    assert loop._infra_this_iteration is False
    assert deadletter_conn.deadletters[0][0] == bad_intent["ulid"]
    assert deadletter_conn.deadletters[0][2] == "embedding input is invalid"


def test_retry_loop_does_not_depend_on_orphaned_running_attribute():
    class _Loop(StreamConsumerLoop):
        group = "group"

        def __init__(self):
            self.redis = _RetryRedis()
            self.stream = "stream"
            self.consumer = "consumer"
            self.block_ms = 0
            self.handled = 0
            self._init_consumer_loop(ledger_prefix="")

        def _handle_entry(self, entry_id, fields):
            self.handled += 1
            self.stop()
            return True

    class _RetryRedis:
        def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
            mode = next(iter(streams.values()))
            if mode == "0":
                return []
            return [("stream", [("entry-1", {})])]

    loop = _Loop()
    loop.running = False
    loop.run()

    assert loop.handled == 1


def test_retryable_write_error_helper_is_removed():
    assert not hasattr(bus, "_is_retryable_write_error")


def test_consumer_start_stop_race_does_not_clear_stop_before_run(monkeypatch):
    captured = []

    class _ManualThread:
        def __init__(self, *, target, daemon):
            captured.append(target)

        def start(self):
            return None

        def join(self, timeout=None):
            return None

    class _Redis:
        def xgroup_create(self, *args, **kwargs):
            return None

        def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
            return []

    class _CountingConsumer(audit.AuditConsumer):
        def __init__(self, *args, **kwargs):
            self.ticks = 0
            super().__init__(*args, **kwargs)

        def _tick(self):
            self.ticks += 1
            self.stop()

    monkeypatch.setattr(audit.threading, "Thread", _ManualThread)
    consumer = _CountingConsumer(_Redis(), lambda: object(), block_ms=0)
    consumer.start()
    consumer.stop()
    captured[0]()

    assert consumer.ticks == 0
