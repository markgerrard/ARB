import json

from arb_memory.eval import (
    EVAL_MAX_PAYLOAD_BYTES,
    EvalConsumer,
    PostgresEvalSink,
    check_eval_health,
    eval_lag,
)
from arb_memory.eval_config import EVAL_GROUP


def _xadd(redis, stream, **fields):
    return redis.xadd(stream, fields)


def _drain(consumer):
    # drain everything currently pending/new
    n = 0
    while consumer.step() is not None:
        n += 1
    return n


def _make(redis_bus, conn_factory):
    prefix = redis_bus.prefix
    stream = f"{prefix}eval:events"
    consumer = EvalConsumer(redis_bus, conn_factory, prefix=prefix, block_ms=50)
    return stream, consumer


def test_event_lands_with_stream_entry_id(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r1", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    _drain(consumer)
    conn = conn_factory()
    row = conn.execute(
        "SELECT run_id, task_id, event_type, stream_entry_id FROM eval_event_raw WHERE run_id='r1'"
    ).fetchone()
    assert row == ("r1", "t1", "task_started", eid)


def test_eval_event_persists_schema_version(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="run-sv", task_id="task-sv", seat_id="codex-x",
                event_type="turn-end", schema_version="v9-test",
                sent_at="2026-06-25T00:00:00+00:00", payload="{}")
    _drain(consumer)
    conn = conn_factory()
    row = conn.execute(
        "SELECT schema_version FROM eval_event_raw WHERE stream_entry_id = %s", (eid,)
    ).fetchone()
    assert row[0] == "v9-test"


def test_eval_event_persists_orchestrator(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="run-orch", task_id="task-orch", seat_id="codex-x",
                event_type="turn-end", orchestrator="claude-bridge-dev",
                sent_at="2026-06-25T00:00:00+00:00", payload="{}")
    _drain(consumer)
    conn = conn_factory()
    row = conn.execute(
        "SELECT orchestrator FROM eval_event_raw WHERE stream_entry_id = %s", (eid,)
    ).fetchone()
    assert row[0] == "claude-bridge-dev"


def test_redelivery_is_idempotent(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r2", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    _drain(consumer)
    # re-present the SAME entry id by writing it back through the sink (simulates PEL redelivery)
    conn = conn_factory()
    sink = PostgresEvalSink()
    dup = sink.write(conn, {"run_id": "r2", "task_id": "t1", "seat_id": "codex",
                            "event_type": "task_started", "sent_at": "2026-06-23T00:00:00+00:00",
                            "payload": {}, "stream_entry_id": eid})
    assert dup == "duplicate"
    n = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='r2'").fetchone()[0]
    assert n == 1


def test_crash_recovery_redrains_pending(redis_bus, conn_factory):
    # entry delivered but NOT acked -> a fresh consumer (same group) re-reads from PEL -> no-op insert
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(redis_bus, stream, run_id="r3", task_id="t1", seat_id="codex",
                event_type="task_finished", sent_at="2026-06-23T00:00:00+00:00",
                payload=json.dumps({"ok": True}))
    # read WITHOUT ack to leave it in PEL
    rows = redis_bus.xreadgroup(EVAL_GROUP, "crasher", {stream: ">"}, count=1)
    assert rows  # delivered, unacked
    # a fresh consumer drains pending
    consumer2 = EvalConsumer(redis_bus, conn_factory, prefix=redis_bus.prefix, consumer="crasher", block_ms=50)
    consumer2.drain_pending()
    conn = conn_factory()
    n = conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='r3'").fetchone()[0]
    assert n == 1


def _drain_until(consumer, predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        consumer.step()
        if predicate():
            return
    raise AssertionError("drain predicate timed out")


def test_missing_run_id_deadletters_not_silent_insert(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    _xadd(redis_bus, stream, task_id="t1", seat_id="codex",
          event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")  # NO run_id
    conn = conn_factory()
    _drain_until(consumer, lambda: conn.execute(
        "SELECT count(*) FROM eval_deadletter WHERE event_type='task_started'").fetchone()[0] == 1)
    assert conn.execute("SELECT count(*) FROM eval_event_raw").fetchone()[0] == 0
    # the deadletter row must store run_id as NULL (not '' — else a foreign-row check could be fooled)
    assert conn.execute(
        "SELECT run_id IS NULL FROM eval_deadletter WHERE event_type='task_started'").fetchone()[0] is True


def test_nonfatal_sink_error_deadletters_and_acks_no_loop(redis_bus, conn_factory):
    # the nested except-Exception handler: a sink that raises a non-infra error must dead-letter +
    # ack (not infinite-loop). Mirrors the audit consumer's poison-entry handling.
    class _BoomSink:
        def write(self, conn, event):
            raise ValueError("synthetic sink bug")

    stream, consumer = _make(redis_bus, conn_factory)
    consumer.sinks = [_BoomSink()]
    eid = _xadd(redis_bus, stream, run_id="r-boom", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")
    assert consumer.step() == "dead-lettered"
    # entry is acked (no longer pending) -> a second step does not re-process it
    pend = redis_bus.xpending(stream, EVAL_GROUP)["pending"]
    assert pend == 0
    conn = conn_factory()
    assert conn.execute("SELECT count(*) FROM eval_deadletter WHERE stream_entry_id=%s", (eid,)).fetchone()[0] == 1


def test_eval_lag_alarm_fires_when_behind(redis_bus, conn_factory):
    stream, _consumer = _make(redis_bus, conn_factory)
    for idx in range(3):
        _xadd(redis_bus, stream, run_id="lagged", task_id=f"t{idx}", seat_id="codex",
              event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload="{}")

    rows = redis_bus.xreadgroup(EVAL_GROUP, "eval-test", {stream: ">"}, count=3)
    assert rows and len(rows[0][1]) == 3

    redis_bus.xtrim(stream, maxlen=1, approximate=False)
    lag = eval_lag(redis_bus, prefix=redis_bus.prefix)
    health = check_eval_health(redis_bus, prefix=redis_bus.prefix, threshold=2)

    assert lag["stream_length"] < 2
    assert lag["group"] == EVAL_GROUP
    assert lag["pending"] == 3
    assert health["alarm"] is True
    assert health["lag"]["pending"] == 3


def test_oversized_payload_deadletters_not_raw_insert(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    payload = json.dumps({"blob": "x" * EVAL_MAX_PAYLOAD_BYTES})
    eid = _xadd(redis_bus, stream, run_id="too-large", task_id="t1", seat_id="codex",
                event_type="task_started", sent_at="2026-06-23T00:00:00+00:00", payload=payload)

    assert consumer.step() == "dead-lettered"
    conn = conn_factory()
    assert conn.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='too-large'").fetchone()[0] == 0
    row = conn.execute(
        "SELECT run_id, error FROM eval_deadletter WHERE stream_entry_id=%s",
        (eid,),
    ).fetchone()
    assert row == ("too-large", "eval payload exceeds EVAL_MAX_PAYLOAD_BYTES")
    assert redis_bus.xpending(stream, EVAL_GROUP)["pending"] == 0
