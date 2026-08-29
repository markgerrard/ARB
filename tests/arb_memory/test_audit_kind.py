from arb_memory.audit import PostgresAuditSink, audit_content_hash


def _event(run_id, seq, source, kind, payload):
    ch = audit_content_hash(run_id, seq, source, kind, payload)
    return {
        "run_id": run_id, "seq": seq, "source": source, "kind": kind,
        "payload": payload, "content_hash": ch, "stream_entry_id": f"{seq}-0",
        "ts": "2026-06-23T00:00:00+00:00",
    }


def test_sink_writes_kind_column(conn_factory):
    conn = conn_factory()
    sink = PostgresAuditSink()
    assert sink.write(conn, _event("run-k1", 1, "orchestrator", "dispatch", {"actor": "seat:codex"})) == "written"
    row = conn.execute(
        "SELECT kind FROM audit_events WHERE run_id=%s AND seq=%s", ("run-k1", 1)
    ).fetchone()
    assert row[0] == "dispatch"


def test_kind_column_matches_payload_kind_when_present(conn_factory):
    conn = conn_factory()
    sink = PostgresAuditSink()
    sink.write(conn, _event("run-k2", 1, "orchestrator", "vote", {"kind": "vote", "actor": "seat:m3"}))
    row = conn.execute(
        "SELECT kind, payload->>'kind' FROM audit_events WHERE run_id=%s AND seq=%s", ("run-k2", 1)
    ).fetchone()
    assert row[0] == "vote"
    # plan-panel M3 P1: assert agreement (not the lenient `in (None, "vote")`) — a column/payload
    # kind drift must red. The seeded payload carries kind='vote', so it must be present and equal.
    assert row[1] == row[0] == "vote"
