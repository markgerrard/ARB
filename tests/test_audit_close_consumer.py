import json

from arb_memory import close


class FakeRedis:
    def __init__(self):
        self.results = []
        self.expirations = []
        self.acks = []
        self.group_creates = []

    def xgroup_create(self, stream, group, id="$", mkstream=False):
        self.group_creates.append((stream, group, id, mkstream))

    def lpush(self, key, value):
        self.results.append((key, json.loads(value)))

    def expire(self, key, seconds):
        self.expirations.append((key, seconds))

    def xack(self, stream, group, entry_id):
        self.acks.append((stream, group, entry_id))


class LoopRedis(FakeRedis):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.read_modes = []
        self.new_reads = 0
        self.consumer = None

    def xreadgroup(self, group, consumer, streams, count=1, block=None):
        mode = next(iter(streams.values()))
        self.read_modes.append(mode)
        if mode == "0":
            if self.new_reads == 1 and not self.acks:
                return [(self.stream, [self.entry])]
            return []
        self.new_reads += 1
        if self.new_reads == 1:
            return [(self.stream, [self.entry])]
        self.consumer.stop()
        return []

    @property
    def stream(self):
        return "fleet:arbmem:audit:close_request"

    def xack(self, stream, group, entry_id):
        super().xack(stream, group, entry_id)
        self.consumer.stop()


class FakeConn:
    closed = False

    def close(self):
        self.closed = True


def _consumer(monkeypatch, redis):
    consumer = close.CloseConsumer(redis, lambda: FakeConn(), prefix="fleet:")
    monkeypatch.setattr(
        close,
        "close_core",
        lambda *args, **kwargs: close.CloseResult("emitted", 0, []),
    )
    return consumer


def test_happy_request_writes_result_and_acks(monkeypatch):
    redis = FakeRedis()
    consumer = _consumer(monkeypatch, redis)

    result = consumer._handle_entry(
        "1-0",
        {
            "request_id": "req-1",
            "run_id": "run-1",
            "verdict": json.dumps({"kind": "verdict"}),
            "requested_by": "orchestrator",
        },
    )

    assert result.outcome == "emitted"
    assert redis.results == [("fleet:arbmem:audit:close_result:req-1", {"outcome": "emitted", "exit_code": 0, "gaps": []})]
    assert redis.expirations == [("fleet:arbmem:audit:close_result:req-1", close.RESULT_TTL_SECONDS)]
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "1-0")]


def test_reconcile_refusal_is_normal_result_and_ack(monkeypatch):
    redis = FakeRedis()
    consumer = close.CloseConsumer(redis, lambda: FakeConn(), prefix="fleet:")
    monkeypatch.setattr(
        close,
        "close_core",
        lambda *args, **kwargs: close.CloseResult("refused_reconcile", 4, ["seat missing"]),
    )

    result = consumer._handle_entry(
        "1-1",
        {"request_id": "req-refused", "run_id": "run-1", "verdict": json.dumps({"kind": "verdict"})},
    )

    assert result.exit_code == 4
    assert redis.results[0][1] == {
        "outcome": "refused_reconcile",
        "exit_code": 4,
        "gaps": ["seat missing"],
    }
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "1-1")]


def test_malformed_request_deadletters_writes_result_and_acks(monkeypatch):
    redis = FakeRedis()
    consumer = _consumer(monkeypatch, redis)
    deadletters = []
    monkeypatch.setattr(
        close,
        "deadletter_malformed_close_request",
        lambda conn, entry_id, fields, error: deadletters.append((entry_id, fields, str(error))),
    )

    result = consumer._handle_entry(
        "2-0",
        {"request_id": "req-bad", "run_id": "run-1", "verdict": "not-json"},
    )

    assert result == "malformed"
    assert redis.results[0][1] == {"outcome": "malformed", "exit_code": 2, "gaps": []}
    assert deadletters and deadletters[0][0] == "2-0"
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "2-0")]


def test_malformed_request_without_request_id_still_has_result(monkeypatch):
    redis = FakeRedis()
    consumer = _consumer(monkeypatch, redis)
    monkeypatch.setattr(close, "deadletter_malformed_close_request", lambda *args: None)

    assert consumer._handle_entry("2-1", {"run_id": "run-1", "verdict": "{}"}) == "malformed"
    assert redis.results[0][0] == "fleet:arbmem:audit:close_result:2-1"
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "2-1")]


def test_transient_close_failure_does_not_ack(monkeypatch):
    redis = FakeRedis()
    consumer = close.CloseConsumer(redis, lambda: (_ for _ in ()).throw(close.psycopg.OperationalError("down")), prefix="fleet:")

    assert consumer._handle_entry(
        "3-0",
        {
            "request_id": "req-retry",
            "run_id": "run-1",
            "verdict": json.dumps({"kind": "verdict"}),
        },
    ) is None
    assert redis.acks == []


def test_run_retries_transient_entry_from_pel_and_acks(monkeypatch):
    entry = (
        "3-1",
        {
            "request_id": "req-retry-loop",
            "run_id": "run-1",
            "verdict": json.dumps({"kind": "verdict"}),
        },
    )
    redis = LoopRedis(entry)
    consumer = close.CloseConsumer(redis, lambda: FakeConn(), prefix="fleet:", block_ms=0)
    redis.consumer = consumer
    outcomes = iter([close.redis.ConnectionError("temporary"), close.CloseResult("emitted", 0, [])])

    def emit_once(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(close, "close_core", emit_once)
    consumer.run()

    assert redis.read_modes == ["0", ">", "0"]
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "3-1")]
    assert redis.results == [
        ("fleet:arbmem:audit:close_result:req-retry-loop", {"outcome": "emitted", "exit_code": 0, "gaps": []})
    ]


def test_unexpected_handler_error_deadletters_and_acks(monkeypatch):
    redis = FakeRedis()
    consumer = _consumer(monkeypatch, redis)
    deadletters = []
    monkeypatch.setattr(
        close,
        "close_core",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("serialize bug")),
    )
    monkeypatch.setattr(
        close,
        "deadletter_malformed_close_request",
        lambda conn, entry_id, fields, error: deadletters.append((entry_id, str(error))),
    )

    result = consumer._handle_entry(
        "4-0",
        {"request_id": "req-bug", "run_id": "run-1", "verdict": json.dumps({"kind": "verdict"})},
    )

    assert result == "dead-lettered"
    assert deadletters == [("4-0", "serialize bug")]
    assert redis.acks == [("fleet:arbmem:audit:close_request", close.GROUP, "4-0")]
