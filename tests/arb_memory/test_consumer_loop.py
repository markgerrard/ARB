import threading
from pathlib import Path

import pytest
import psycopg
import redis
import redis as redis_module

import arb_memory.consumer_loop as consumer_loop
from arb_memory.consumer_loop import POISON_RETRY_LIMIT, StreamConsumerLoop, classify_infra_error
from arb_memory.bus import GROUP, ReadLoop


class FakeRedis:
    def __init__(self):
        self.reads = []
        self.acks = []
        self.consumer = None
        self.pending_available = False

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == ">":
            entries = [] if self.pending_available else [("1-0", {"kind": "new"})]
            self.pending_available = True
        elif mode == "0" and self.pending_available:
            entries = [("2-0", {"kind": "pending"})]
            self.pending_available = False
        else:
            entries = []
        return [("stream", entries)] if entries else []

    def xack(self, stream, group, entry_id):
        self.acks.append(entry_id)


class DemoLoop(StreamConsumerLoop):
    group = "group"

    def __init__(self, redis):
        self.redis = redis
        self.stream = "stream"
        self.consumer = "consumer"
        self.block_ms = 0
        self.handled = []
        self._init_consumer_loop(ledger_prefix="")

    def _handle_entry(self, entry_id, fields):
        self.handled.append((entry_id, fields))
        self._ack(entry_id)
        if len(self.handled) >= 2:
            self.stop()
        return True


class RetryRedis(FakeRedis):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.entry = ("1-0", {"kind": "retry"})
        self.acked = False

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == "0":
            return [] if self.acked or len(self.reads) == 1 else [("stream", [self.entry])]
        if mode == ">":
            return [("stream", [self.entry])] if len(self.reads) == 2 else []
        return [("stream", [self.entry])] if not self.acked else []

    def xack(self, stream, group, entry_id):
        self.acked = True
        super().xack(stream, group, entry_id)


class RetryLoop(StreamConsumerLoop):
    group = "group"

    def __init__(self, redis, failures):
        self.redis = redis
        self.stream = "stream"
        self.consumer = "consumer"
        self.block_ms = 0
        self.failures = iter(failures)
        self.deadletters = []
        self._init_consumer_loop(ledger_prefix="")

    def _handle_entry(self, entry_id, fields):
        failure = next(self.failures, None)
        if failure is not None:
            return self._retry_or_exhaust(
                entry_id,
                fields,
                failure,
                lambda: self.deadletters.append(entry_id),
                on_terminal=self.stop,
            )
        self._poison.pop(entry_id, None)
        self._ack(entry_id)
        self.stop()
        return True


def test_run_uses_cursor_pending_read_and_stop_is_interruptible():
    redis = FakeRedis()
    loop = DemoLoop(redis)

    thread = threading.Thread(target=loop.run)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert redis.reads[:3] == ["0", ">", "0"]
    assert redis.acks == ["1-0", "2-0"]


def test_truthy_terminal_handler_does_not_stop_pending_work():
    redis = FakeRedis()
    loop = DemoLoop(redis)
    loop.run()

    assert [entry_id for entry_id, _ in loop.handled] == ["1-0", "2-0"]


class ReadCleanupRedis:
    def __init__(self):
        self.reads = []
        self.acks = []
        self.owner = None

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == "0" and len(self.reads) == 1:
            return [("stream", [("stale-1", {"cid": "c", "reply": "reply:c", "query": "q"})])]
        if mode == ">":
            self.owner.stop()
        return []

    def xack(self, stream, group, entry_id):
        self.acks.append((stream, group, entry_id))


def test_read_loop_run_cleans_pending_without_replaying_reply():
    redis = ReadCleanupRedis()
    loop = ReadLoop(redis, lambda: object(), embed=lambda text: [], prefix="fleet:", block_ms=0)
    redis.owner = loop
    loop.run()

    assert redis.reads == ["0", "0", ">"]
    assert redis.acks == [("fleet:arbmem:reads", GROUP, "stale-1")]


@pytest.mark.parametrize(
    "error, expected",
    [
        (psycopg.OperationalError("down"), "transient"),
        (psycopg.InterfaceError("closed"), "transient"),
        (psycopg.DataError("bad value"), "poison"),
        (psycopg.IntegrityError("constraint"), "poison"),
        (psycopg.ProgrammingError("query"), "poison"),
        (redis.DataError("bad encoding"), "poison"),
        (redis.ResponseError("WRONGTYPE"), "transient"),
        (redis.ReadOnlyError("failover"), "transient"),
        (redis.OutOfMemoryError("full"), "transient"),
        (redis.ConnectionError("down"), "transient"),
        (type("UnknownRedisError", (redis.RedisError,), {})(), "transient"),
    ],
)
def test_classify_infra_error_is_fail_safe(error, expected):
    assert classify_infra_error(error) == expected


def test_run_bounds_consecutive_poison_and_deadletters_once():
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [redis_module.DataError("bad")] * POISON_RETRY_LIMIT)
    thread = threading.Thread(target=loop.run)
    thread.start()
    thread.join(timeout=1)
    if thread.is_alive():
        loop.stop()
        thread.join(timeout=1)
        pytest.fail("poison entry was not bounded")

    assert len(loop.deadletters) == 1
    assert redis.acks == ["1-0"]
    assert loop._poison == {}


def test_run_transient_redeliveries_do_not_consume_poison_budget():
    redis = RetryRedis(None)
    failures = [redis_module.ConnectionError("down")] * 2 + [redis_module.DataError("bad")] * POISON_RETRY_LIMIT
    loop = RetryLoop(redis, failures)
    loop.run()

    assert len(loop.deadletters) == 1
    assert loop._poison == {}


def test_run_transient_failure_retries_without_deadletter():
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [redis_module.ConnectionError("down"), None])
    loop.run()

    assert loop.deadletters == []
    assert redis.acks == ["1-0"]


def test_run_bare_response_error_retries_and_never_deadletters():
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [redis_module.ResponseError("WRONGTYPE")] * POISON_RETRY_LIMIT + [None])
    loop.run()

    assert loop.deadletters == []
    assert redis.acks == ["1-0"]
    assert loop._infra_this_iteration is False


def test_transient_classification_deny_proof_never_exhausts(monkeypatch):
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: 0)
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [redis_module.ConnectionError("down")] * POISON_RETRY_LIMIT + [None])
    loop.run()

    assert loop.deadletters == []
    assert redis.acks == ["1-0"]


def test_backoff_is_monotonic_and_capped():
    assert [consumer_loop.backoff_delay(n, base=0.5, cap=2) for n in range(5)] == [0.5, 1, 2, 2, 2]


def test_run_backoff_fires_for_transient_failure(monkeypatch):
    waits = []
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: waits.append(failures) or 0)
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [psycopg.OperationalError("down"), None])
    loop.run()

    assert waits == [1]


def test_poison_retries_do_not_set_infra_backoff_flag(monkeypatch):
    waits = []
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: waits.append(failures) or 0)
    redis = RetryRedis(None)
    loop = RetryLoop(redis, [redis_module.DataError("bad")] * POISON_RETRY_LIMIT)
    loop.run()

    assert waits == []


class AckFailureRedis(RetryRedis):
    def __init__(self):
        super().__init__(None)
        self.fail_ack = True

    def xack(self, stream, group, entry_id):
        if self.fail_ack:
            self.fail_ack = False
            raise redis_module.ConnectionError("ack down")
        super().xack(stream, group, entry_id)


def test_ack_failure_sets_flag_and_redelivers(monkeypatch):
    waits = []
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: waits.append(failures) or 0)
    redis = AckFailureRedis()
    loop = RetryLoop(redis, [None, None])
    loop.run()

    assert waits == [1]
    assert redis.acks == ["1-0"]


class RedisDownRedis(RetryRedis):
    def __init__(self, owner):
        super().__init__(None)
        self.owner = owner
        self.down = True

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        if mode == ">" and self.down:
            self.down = False
            raise redis_module.ConnectionError("redis down")
        result = super().xreadgroup(group, consumer, streams, count=count, block=block)
        if mode == ">" and not result:
            self.owner.stop()
        return result


def test_run_backoff_fires_for_redis_xread_failure(monkeypatch):
    waits = []
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: waits.append(failures) or 0)
    loop = RetryLoop.__new__(RetryLoop)
    redis = RedisDownRedis(loop)
    RetryLoop.__init__(loop, redis, [])
    redis.owner = loop
    loop.run()

    assert waits == [1]


class StarvationRedis:
    def __init__(self):
        self.reads = []
        self.acked = []
        self.owner = None

    def xgroup_create(self, *args, **kwargs):
        return None

    def xreadgroup(self, group, consumer, streams, *, count=1, block=None):
        mode = next(iter(streams.values()))
        self.reads.append(mode)
        if mode == ">" or (mode == "0" and len(self.reads) == 1):
            return []
        if mode == "0":
            return [("stream", [("a-0", {"kind": "transient"})])]
        if mode == "0-0":
            return [("stream", [("a-0", {"kind": "transient"})])]
        if mode == "a-0":
            return [("stream", [("b-0", {"kind": "poison"})])]
        return []

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)
        if entry_id == "b-0":
            self.owner.stop()


class StarvationLoop(StreamConsumerLoop):
    group = "group"

    def __init__(self, redis):
        self.redis = redis
        self.stream = "stream"
        self.consumer = "consumer"
        self.block_ms = 0
        self.deadletters = []
        self._init_consumer_loop(ledger_prefix="")

    def _handle_entry(self, entry_id, fields):
        if entry_id == "a-0":
            return self._retry_or_exhaust(entry_id, fields, psycopg.OperationalError("down"), lambda: None)
        return self._retry_or_exhaust(
            entry_id,
            fields,
            redis_module.DataError("bad"),
            lambda: self.deadletters.append(entry_id),
        )


def test_cursor_allows_newer_poison_pending_entry_past_stuck_transient(monkeypatch):
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: 0)
    redis = StarvationRedis()
    loop = StarvationLoop(redis)
    redis.owner = loop
    loop.run()

    assert redis.acked == ["b-0"]
    assert loop.deadletters == ["b-0"]
    assert "a-0" in redis.reads and "b-0" in redis.reads


class CircuitLoop(RetryLoop):
    def __init__(self, redis, canary_results):
        self.canary_results = iter(canary_results)
        self.max_poison_size = 0
        self.fail_deadletter = True
        super().__init__(redis, [redis_module.DataError("entry poison")] * 100)

    def _canary_deadletter_sink(self):
        result = next(self.canary_results, False)
        self.max_poison_size = max(self.max_poison_size, len(self._poison))
        return result

    def _handle_entry(self, entry_id, fields):
        def deadletter():
            if self.fail_deadletter:
                self.fail_deadletter = False
                raise redis_module.DataError("sink poison")
            self.deadletters.append(entry_id)

        result = self._retry_or_exhaust(
            entry_id,
            fields,
            redis_module.DataError("entry poison"),
            deadletter,
            on_terminal=self.stop,
        )
        self.max_poison_size = max(self.max_poison_size, len(self._poison))
        return result


def test_sink_circuit_bounds_counters_and_recovers(monkeypatch):
    monkeypatch.setattr(consumer_loop, "backoff_delay", lambda failures: 0)
    redis = RetryRedis(None)
    loop = CircuitLoop(redis, [False, False, True])

    loop.run()

    assert loop.max_poison_size <= 1
    assert loop._deadletter_sink_open is False
    assert redis.acks == ["1-0"]


def test_row_specific_deadletter_failure_is_terminal_and_alarms(caplog):
    class RowSpecificLoop(RetryLoop):
        def _canary_deadletter_sink(self):
            return True

    redis = RetryRedis(None)
    loop = RowSpecificLoop(redis, [redis_module.DataError("entry poison")] * POISON_RETRY_LIMIT)
    loop.failures = iter([redis_module.DataError("entry poison")] * POISON_RETRY_LIMIT)
    loop._retry_or_exhaust = lambda *args, **kwargs: StreamConsumerLoop._retry_or_exhaust(loop, *args, **kwargs)

    def fail_deadletter(*args, **kwargs):
        raise redis_module.DataError("cannot store")

    loop._handle_entry = lambda entry_id, fields: loop._retry_or_exhaust(
        entry_id,
        fields,
        redis_module.DataError("entry poison"),
        fail_deadletter,
        on_terminal=loop.stop,
    )
    loop.run()

    assert redis.acks == ["1-0"]
    assert loop._poison == {}
    assert "deadletter-unstorable" in caplog.text


def test_sanitize_json_replaces_nul_and_invalid_utf8():
    assert consumer_loop.sanitize_json({"payload": "a\x00b", "raw": b"\xff"}) == {
        "payload": "ab",
        "raw": "�",
    }


def test_deadletter_migrations_and_conflicts_are_entry_id_based():
    schema = (Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE audit_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text" in schema
    assert "ALTER TABLE write_deadletter ADD COLUMN IF NOT EXISTS stream_entry_id text" in schema
    assert "audit_deadletter_stream_entry_id_idx" in schema
    assert "write_deadletter_stream_entry_id_idx" in schema
