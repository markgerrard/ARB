import json

import pytest

pytest.importorskip("redis")
psycopg = pytest.importorskip("psycopg")

from tests.arb_memory.conftest import _wait_until

from arb_memory.audit import (
    AUDIT_MAX_PAYLOAD_BYTES,
    AuditRun,
    AuditConsumer,
    PostgresAuditSink,
    audit_content_hash,
    audit_emit,
    audit_lag,
    audit_stream,
    check_audit_health,
    handle_audit_event,
    next_seq,
)


def _event(run_id="r", seq=1, kind="vote", payload=None, source="orchestrator", ts="2026-06-21T12:00:00Z"):
    if payload is None:
        payload = {"x": 1}
    return {
        "run_id": run_id,
        "seq": seq,
        "source": source,
        "kind": kind,
        "payload": payload,
        "content_hash": audit_content_hash(run_id, seq, source, kind, payload),
        "stream_entry_id": f"{seq}-0",
        "ts": ts,
    }


def test_seq_allocator_is_unique_under_concurrency(redis_bus):
    import threading

    seqs = []
    lock = threading.Lock()

    def grab():
        seq = next_seq(redis_bus, "run1", prefix=redis_bus.prefix)
        with lock:
            seqs.append(seq)

    threads = [threading.Thread(target=grab) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(seqs)) == 50


def test_content_hash_stable_and_field_sensitive():
    hash1 = audit_content_hash("r", 1, "orch", "vote", {"a": 1})
    assert hash1 == audit_content_hash("r", 1, "orch", "vote", {"a": 1})
    assert hash1 != audit_content_hash("r", 1, "orch", "vote", {"a": 2})


def test_oversized_payload_rejected(redis_bus):
    payload = {"blob": "x" * AUDIT_MAX_PAYLOAD_BYTES}

    with pytest.raises(ValueError, match="audit payload exceeds"):
        audit_emit(
            redis_bus,
            run_id="run1",
            seq=1,
            source="orchestrator",
            kind="large",
            payload=payload,
            prefix=redis_bus.prefix,
        )

    assert redis_bus.xlen(f"{redis_bus.prefix}arbmem:audit") == 0


def test_audit_run_increments_seq(redis_bus):
    run = AuditRun(redis_bus, "run1", prefix=redis_bus.prefix)

    id1 = run.emit("orchestrator", "vote", {"x": 1})
    id2 = run.emit("orchestrator", "vote", {"x": 2})

    assert id1 != id2
    rows = redis_bus.xrange(f"{redis_bus.prefix}arbmem:audit")
    seqs = [int(fields["seq"]) for _, fields in rows]
    payloads = [json.loads(fields["payload"]) for _, fields in rows]
    assert seqs == [1, 2]
    assert payloads == [{"x": 1}, {"x": 2}]


def test_audit_idempotent_on_redelivery(scratch):
    event = _event(run_id="r", seq=1, kind="vote", payload={"x": 1})

    assert handle_audit_event(scratch, event, sinks=[PostgresAuditSink()]) == "written"
    assert handle_audit_event(scratch, event, sinks=[PostgresAuditSink()]) == "duplicate"
    assert scratch.execute("SELECT count(*) FROM audit_events WHERE run_id='r'").fetchone()[0] == 1


def test_seq_collision_fails_loud(scratch):
    handle_audit_event(
        scratch,
        _event(run_id="r", seq=1, kind="vote", payload={"x": 1}),
        sinks=[PostgresAuditSink()],
    )

    result = handle_audit_event(
        scratch,
        _event(run_id="r", seq=1, kind="vote", payload={"x": 999}),
        sinks=[PostgresAuditSink()],
    )

    assert result == "dead-lettered"
    assert scratch.execute("SELECT payload FROM audit_events WHERE run_id='r' AND seq=1").fetchone()[0] == {"x": 1}
    deadletter = scratch.execute(
        "SELECT payload, content_hash FROM audit_deadletter WHERE run_id='r' AND seq=1"
    ).fetchone()
    assert deadletter is not None and deadletter[0] == {"x": 999}


def test_audit_event_roundtrip(redis_bus, conn_factory):
    consumer = AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1)
    run = AuditRun(redis_bus, "roundtrip", prefix=redis_bus.prefix)
    stream_id = run.emit("orchestrator", "dispatch", {"task": "review"})

    assert consumer.step() == "written"

    row = conn_factory().execute(
        "SELECT run_id, seq, source, payload, stream_entry_id, content_hash, raw_entry->>'kind' "
        "FROM audit_events WHERE run_id='roundtrip'"
    ).fetchone()
    assert row == (
        "roundtrip",
        1,
        "orchestrator",
        {"task": "review"},
        stream_id,
        audit_content_hash("roundtrip", 1, "orchestrator", "dispatch", {"task": "review"}),
        "dispatch",
    )


def test_audit_xack_after_commit(redis_bus, conn_factory, monkeypatch):
    import arb_memory.audit as audit

    consumer = AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1)
    audit_emit(
        redis_bus,
        run_id="retry",
        seq=1,
        source="orchestrator",
        kind="vote",
        payload={"x": 1},
        prefix=redis_bus.prefix,
    )
    real_handle = audit.handle_audit_event
    calls = {"count": 0}

    def fail_once(conn, event, *, sinks):
        calls["count"] += 1
        if calls["count"] == 1:
            raise psycopg.OperationalError("temporary db failure")
        return real_handle(conn, event, sinks=sinks)

    monkeypatch.setattr(audit, "handle_audit_event", fail_once)

    assert consumer.step() is None
    assert redis_bus.xpending(audit_stream(redis_bus.prefix), "arbmem-audit")["pending"] == 1
    assert consumer.drain_pending() == 1
    assert redis_bus.xpending(audit_stream(redis_bus.prefix), "arbmem-audit")["pending"] == 0
    assert conn_factory().execute("SELECT count(*) FROM audit_events WHERE run_id='retry'").fetchone()[0] == 1
    assert conn_factory().execute(
        "SELECT count(*) FROM audit_deadletter WHERE run_id='retry'"
    ).fetchone()[0] == 0


def test_duplicate_verdict_is_deadlettered_and_acked(redis_bus, conn_factory):
    stream = audit_stream(redis_bus.prefix)
    consumer = AuditConsumer(
        redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1
    )
    run = AuditRun(redis_bus, "duplicate-verdict", prefix=redis_bus.prefix)
    run.emit("orchestrator", "verdict", {"decision": "approve"})
    run.emit("orchestrator", "verdict", {"decision": "approve", "retry": True})

    assert consumer.step() == "written"
    assert consumer.step() == "duplicate_verdict"
    assert redis_bus.xpending(stream, "arbmem-audit")["pending"] == 0

    conn = conn_factory()
    verdicts = conn.execute(
        "SELECT payload FROM audit_events WHERE run_id='duplicate-verdict' AND kind='verdict'"
    ).fetchall()
    assert verdicts == [({"decision": "approve"},)]
    deadletter = conn.execute(
        "SELECT error, payload FROM audit_deadletter "
        "WHERE run_id='duplicate-verdict' AND kind='verdict'"
    ).fetchone()
    assert deadletter == ("duplicate verdict for run_id", {"decision": "approve", "retry": True})


def test_sink_side_programming_error_is_retried_without_ack(redis_bus, conn_factory):
    class BrokenSink:
        def write(self, conn, event):
            raise psycopg.ProgrammingError("missing audit_events")

    stream = audit_stream(redis_bus.prefix)
    consumer = AuditConsumer(
        redis_bus,
        conn_factory,
        prefix=redis_bus.prefix,
        consumer="audit-test",
        block_ms=1,
        sinks=[BrokenSink()],
    )
    audit_emit(
        redis_bus,
        run_id="programming-error",
        seq=1,
        source="orchestrator",
        kind="vote",
        payload={"x": 1},
        prefix=redis_bus.prefix,
    )

    assert consumer.step() is None
    assert redis_bus.xpending(stream, "arbmem-audit")["pending"] == 1
    assert conn_factory().execute("SELECT count(*) FROM audit_events WHERE run_id='programming-error'").fetchone()[0] == 0


def test_audit_poison_deadlettered(redis_bus, conn_factory):
    stream = audit_stream(redis_bus.prefix)
    consumer = AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=10)
    redis_bus.xadd(stream, {"run_id": "poison", "payload": "{"})
    audit_emit(
        redis_bus,
        run_id="after-poison",
        seq=1,
        source="orchestrator",
        kind="vote",
        payload={"ok": True},
        prefix=redis_bus.prefix,
    )

    try:
        consumer.start()
        _wait_until(
            lambda: conn_factory()
            .execute("SELECT count(*) FROM audit_events WHERE run_id='after-poison'")
            .fetchone()[0]
            == 1
        )
        assert consumer.thread.is_alive()
    finally:
        consumer.stop()

    assert redis_bus.xpending(stream, "arbmem-audit")["pending"] == 0
    row = conn_factory().execute(
        "SELECT run_id, raw_entry->>'payload' FROM audit_deadletter WHERE run_id='poison'"
    ).fetchone()
    assert row == ("poison", "{")


def test_handler_error_deadlettered_not_dropped(redis_bus, conn_factory):
    # A deterministic, non-infra failure INSIDE the sink/handler (after the event
    # parsed cleanly) must not silently drop the event: it is acked (so it does not
    # crash-loop the consumer) but preserved in the deadletter, recoverable later.
    # This guards the future sink-seam (object-store / training-export), where a
    # serialize/validation bug would otherwise leak evidence at audit.py:361.
    class ExplodingSink:
        def write(self, conn, event):
            raise ValueError("handler bug: cannot serialize event")

    stream = audit_stream(redis_bus.prefix)
    consumer = AuditConsumer(
        redis_bus,
        conn_factory,
        prefix=redis_bus.prefix,
        consumer="audit-test",
        block_ms=1,
        sinks=[ExplodingSink()],
    )
    audit_emit(
        redis_bus,
        run_id="handler-error",
        seq=1,
        source="orchestrator",
        kind="vote",
        payload={"x": 1},
        prefix=redis_bus.prefix,
    )

    assert consumer.step() == "dead-lettered"
    assert redis_bus.xpending(stream, "arbmem-audit")["pending"] == 0
    row = conn_factory().execute(
        "SELECT run_id, raw_entry->>'payload' FROM audit_deadletter WHERE run_id='handler-error'"
    ).fetchone()
    assert row == ("handler-error", '{"x":1}')


def test_deadletter_idempotent_on_redelivery(scratch):
    handle_audit_event(
        scratch,
        _event(run_id="r", seq=1, kind="vote", payload={"x": 1}),
        sinks=[PostgresAuditSink()],
    )
    conflict = _event(run_id="r", seq=1, kind="vote", payload={"x": 999})

    assert handle_audit_event(scratch, conflict, sinks=[PostgresAuditSink()]) == "dead-lettered"
    assert handle_audit_event(scratch, conflict, sinks=[PostgresAuditSink()]) == "dead-lettered"

    assert scratch.execute(
        "SELECT count(*) FROM audit_deadletter WHERE run_id='r' AND seq=1 AND content_hash=%s",
        (conflict["content_hash"],),
    ).fetchone()[0] == 1


def test_seq_ttl_is_long_and_refreshed(redis_bus):
    first = next_seq(redis_bus, "ttl-run", prefix=redis_bus.prefix)
    key = f"{redis_bus.prefix}arbmem:audit:run:ttl-run:seq"
    redis_bus.expire(key, 1)
    second = next_seq(redis_bus, "ttl-run", prefix=redis_bus.prefix)

    assert (first, second) == (1, 2)
    assert redis_bus.ttl(key) >= 30 * 24 * 60 * 60


def test_seq_orders_within_a_run(redis_bus, conn_factory):
    consumer = AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1)
    for seq, ts in [
        (1, "2026-06-21T12:00:03Z"),
        (2, "2026-06-21T12:00:02Z"),
        (3, "2026-06-21T12:00:01Z"),
    ]:
        audit_emit(
            redis_bus,
            run_id="ordered",
            seq=seq,
            source="orchestrator",
            kind="seat-position",
            payload={"seq": seq},
            ts=ts,
            prefix=redis_bus.prefix,
        )

    assert [consumer.step(), consumer.step(), consumer.step()] == ["written", "written", "written"]
    conn = conn_factory()
    by_seq = [row[0]["seq"] for row in conn.execute("SELECT payload FROM audit_events ORDER BY seq").fetchall()]
    by_ts = [row[0]["seq"] for row in conn.execute("SELECT payload FROM audit_events ORDER BY ts").fetchall()]

    assert by_seq == [1, 2, 3]
    assert by_ts == [3, 2, 1]


def test_lag_alarm_fires_when_behind(redis_bus, conn_factory):
    stream = audit_stream(redis_bus.prefix)
    AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1)
    for seq in range(1, 4):
        audit_emit(
            redis_bus,
            run_id="lagged",
            seq=seq,
            source="orchestrator",
            kind="vote",
            payload={"seq": seq},
            prefix=redis_bus.prefix,
        )

    rows = redis_bus.xreadgroup("arbmem-audit", "audit-test", {stream: ">"}, count=3)
    assert rows and len(rows[0][1]) == 3

    lag = audit_lag(redis_bus, prefix=redis_bus.prefix)
    health = check_audit_health(redis_bus, prefix=redis_bus.prefix, threshold=2)

    redis_bus.xtrim(stream, maxlen=1, approximate=False)
    lag = audit_lag(redis_bus, prefix=redis_bus.prefix)
    health = check_audit_health(redis_bus, prefix=redis_bus.prefix, threshold=2)

    assert lag["stream_length"] < 2
    assert lag["group"] == "arbmem-audit"
    assert lag["pending"] == 3
    assert health["alarm"] is True
    assert health["lag"]["pending"] == 3


def test_run_join_superset_of_disagreement(redis_bus, conn_factory):
    consumer = AuditConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="audit-test", block_ms=1)
    run = AuditRun(redis_bus, "panel-run", prefix=redis_bus.prefix)
    run.emit("orchestrator", "dispatch", {"task": "decide"})
    for seat in ["codex", "agy", "opus"]:
        run.emit("seat", "seat-position", {"seat": seat, "position": f"{seat}-yes"})
    run.emit("orchestrator", "verdict", {"winner": "yes"})

    assert [consumer.step() for _ in range(5)] == ["written"] * 5

    rows = conn_factory().execute(
        "SELECT seq, raw_entry->>'kind', payload FROM audit_events WHERE run_id=%s ORDER BY seq",
        ("panel-run",),
    ).fetchall()
    assert rows == [
        (1, "dispatch", {"task": "decide"}),
        (2, "seat-position", {"seat": "codex", "position": "codex-yes"}),
        (3, "seat-position", {"seat": "agy", "position": "agy-yes"}),
        (4, "seat-position", {"seat": "opus", "position": "opus-yes"}),
        (5, "verdict", {"winner": "yes"}),
    ]
