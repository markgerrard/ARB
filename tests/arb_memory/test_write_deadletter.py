import json

from arb_memory import bus


class _StubRedis:
    def __init__(self):
        self.acked = []

    def xgroup_create(self, *a, **k):
        pass

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)


class _RecordingWriteLoop(bus.WriteLoop):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.published = []

    def _publish_result(self, entry_id, fields, receipt, is_replay):
        self.published.append((entry_id, fields, receipt, is_replay))


def test_publish_result_hook_receives_success_replay_and_malformed_terminal_entries(monkeypatch):
    loop = _RecordingWriteLoop(_StubRedis(), lambda: object(), embed=lambda text: [])
    receipt = {
        "artefact_outcome": "stored",
        "artefact_id": "d.md",
        "version": 1,
        "hints_stored": 0,
    }
    fields = {
        "ulid": "ulid-hook-1",
        "payload": json.dumps({"ulid": "ulid-hook-1", "kind": "hints", "artefact": None, "hints": []}),
    }
    monkeypatch.setattr(bus, "handle_write_intent", lambda *args, **kwargs: (receipt, False))

    assert loop._handle_entry("1-0", fields) is True
    assert loop.published == [("1-0", fields, receipt, False)]

    replay_fields = {
        "ulid": "ulid-hook-2",
        "payload": json.dumps({"ulid": "ulid-hook-2", "kind": "hints", "artefact": None, "hints": []}),
    }
    monkeypatch.setattr(bus, "handle_write_intent", lambda *args, **kwargs: (None, True))
    assert loop._handle_entry("2-0", replay_fields) is True
    assert loop.published[-1] == ("2-0", replay_fields, None, True)

    malformed_fields = {"ulid": "ulid-hook-3", "payload": "{"}
    monkeypatch.setattr(loop, "_deadletter", lambda *args: True)
    assert loop._handle_entry("3-0", malformed_fields) is True
    assert loop.published[-1] == ("3-0", malformed_fields, None, False)


def test_publish_result_default_is_noop():
    loop = bus.WriteLoop(_StubRedis(), lambda: object(), embed=lambda text: [])

    assert loop._publish_result("1-0", {}, None, False) is None


def test_write_loop_calls_conn_factory_once_and_keeps_it():
    calls = []
    conn = object()

    def conn_factory():
        calls.append(True)
        return conn

    loop = bus.WriteLoop(_StubRedis(), conn_factory, embed=lambda text: [])

    assert calls == [True]
    assert loop.conn is conn
    assert loop.conn_factory is conn_factory


def test_deterministic_bad_intent_is_deadlettered_not_dropped(conn_factory, fake_embed, monkeypatch):
    # Deterministic-bad intents deadletter only once the poison retry budget is
    # exhausted; pin it to 1 so this asserts the terminal outcome, not the pacing.
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    conn = conn_factory()
    loop = bus.WriteLoop(_StubRedis(), conn_factory, embed=fake_embed)
    intent = {
        "ulid": "ulid-dl-1",
        "kind": "hints",
        "artefact": None,
        "hints": [{"text": "orphan", "artefact_id": "nope", "artefact_version": 7}],
    }
    fields = {"ulid": "ulid-dl-1", "payload": json.dumps(intent)}

    handled = loop._handle_entry("1-0", fields)

    assert handled is True
    assert loop.redis.acked == ["1-0"]
    row = conn.execute(
        "SELECT error FROM write_deadletter WHERE ulid = %s",
        ("ulid-dl-1",),
    ).fetchone()
    assert row is not None


def test_failed_deadletter_does_not_ack_entry(conn_factory, fake_embed, monkeypatch):
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    conn = conn_factory()
    loop = bus.WriteLoop(_StubRedis(), conn_factory, embed=fake_embed)
    intent = {
        "ulid": "ulid-dl-fail",
        "kind": "hints",
        "artefact": None,
        "hints": [{"text": "orphan", "artefact_id": "nope", "artefact_version": 7}],
    }
    fields = {"ulid": "ulid-dl-fail", "payload": json.dumps(intent)}
    monkeypatch.setattr(loop, "_deadletter", lambda *args: False)
    # A failed deadletter only holds the entry when the sink itself is down. With a
    # healthy sink the canary passes and the entry is acked as unstorable instead
    # (covered by test_consumer_loop.py's deadletter-unstorable case), so force the
    # sink-down branch to assert the no-ack invariant this test is named for.
    monkeypatch.setattr(loop, "_canary_deadletter_sink", lambda: False)

    handled = loop._handle_entry("1-0", fields)

    assert handled is False
    assert loop.redis.acked == []
    assert loop._deadletter_sink_open is True
