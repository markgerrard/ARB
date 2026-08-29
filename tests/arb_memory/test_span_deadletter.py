import os
import uuid

import psycopg
import pytest
from psycopg import sql

from arb_memory.eval import PostgresEvalSink, _close_turn
from arb_memory.mcp.grants import apply_eval_grants


def _eval_test_role(scratch):
    role = f"arb_eval_deadletter_test_{uuid.uuid4().hex}"
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE; role-connected deadletter proof requires it")
    return role


def _cleanup_eval_test_role(scratch, role):
    scratch.execute("RESET ROLE")
    scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _event(entry, event_type, payload):
    return {
        "run_id": "dead-run",
        "task_id": "dead-task",
        "seat_id": "codex",
        "orchestrator": "test",
        "event_type": event_type,
        "sent_at": "2026-07-15T00:00:00+00:00",
        "payload": {"attempt_epoch": 1, **payload},
        "stream_entry_id": entry,
    }


def test_orphan_finish_is_deadlettered_without_partial_span(conn_factory):
    conn = conn_factory()
    event = _event("orphan", "turn_completed", {"turn_index": 7})
    PostgresEvalSink().write(conn, event)
    PostgresEvalSink().write(conn, event)
    assert conn.execute("SELECT count(*) FROM eval_turn").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM span_deadletter WHERE stream_entry_id='orphan'"
    ).fetchone()[0] == 1


def test_eval_role_can_deadletter_span_from_second_connection(scratch):
    role = _eval_test_role(scratch)
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    try:
        apply_eval_grants(scratch, role)
        assert scratch.execute(
            "SELECT has_column_privilege(%s, %s, 'stream_entry_id', 'SELECT')",
            (role, f"{schema}.span_deadletter"),
        ).fetchone()[0] is True
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT')",
            (role, f"{schema}.span_deadletter"),
        ).fetchone()[0] is False
        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        with psycopg.connect(os.environ["ARB_MEMORY_DSN"]) as role_conn:
            role_conn.autocommit = True
            role_conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            role_conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert role_conn.execute("SELECT current_user").fetchone()[0] == role
            PostgresEvalSink().write(
                role_conn,
                _event("role-deadletter", "turn_completed", {"turn_index": 7}),
            )
        assert scratch.execute(
            "SELECT count(*) FROM span_deadletter WHERE stream_entry_id='role-deadletter'"
        ).fetchone()[0] == 1
    finally:
        _cleanup_eval_test_role(scratch, role)


def test_orphan_finalized_is_deadlettered_when_turn_row_is_absent(conn_factory):
    conn = conn_factory()
    PostgresEvalSink().write(conn, _event("orphan-final", "turn_finalized", {
        "turn_index": 7, "event_ts": "2026-07-15T00:00:01+00:00",
    }))
    assert conn.execute("SELECT count(*) FROM eval_turn").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM span_deadletter WHERE stream_entry_id='orphan-final'"
    ).fetchone()[0] == 1


def test_missing_pairing_id_is_deadlettered_without_fifo(conn_factory):
    conn = conn_factory()
    PostgresEvalSink().write(conn, _event("missing", "command_finished", {}))
    assert conn.execute("SELECT count(*) FROM eval_tool_call").fetchone()[0] == 0
    assert conn.execute(
        "SELECT error FROM span_deadletter WHERE stream_entry_id='missing'"
    ).fetchone()[0] == "command_finished missing tool_call_id"


def test_task_finish_without_open_children_is_not_deadletter(conn_factory):
    conn = conn_factory()
    PostgresEvalSink().write(conn, _event("task", "task_finished", {"ok": True}))
    assert conn.execute("SELECT count(*) FROM span_deadletter").fetchone()[0] == 0


def test_refuse_reopen_of_retracted_row_is_not_deadletter(conn_factory):
    conn = conn_factory()
    sink = PostgresEvalSink()
    sink.write(conn, _event("start", "turn_started", {"turn_index": 2}))
    sink.write(conn, _event("retract", "turn_finality_retracted", {"turn_index": 2}))
    sink.write(conn, _event("final", "turn_finalized", {
        "turn_index": 2,
        "event_ts": "2026-07-15T00:00:01+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00",
    }))
    assert conn.execute("SELECT count(*) FROM span_deadletter").fetchone()[0] == 0


def test_python_sticky_retract_guard_returns_before_update(conn_factory):
    conn = conn_factory()
    sink = PostgresEvalSink()
    sink.write(conn, _event("start-python", "turn_started", {"turn_index": 4}))
    sink.write(conn, _event("retract-python", "turn_finality_retracted", {"turn_index": 4}))

    class SpyConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.queries = []

        def execute(self, query, *args, **kwargs):
            self.queries.append(str(query))
            return self.wrapped.execute(query, *args, **kwargs)

    spy = SpyConnection(conn)
    _close_turn(spy, _event("final-python", "turn_finalized", {
        "turn_index": 4, "event_ts": "2026-07-15T00:00:05+00:00",
    }), 1, finality=True)
    assert not any("UPDATE eval_turn" in query for query in spy.queries)


def test_turn_close_sql_repeats_open_gate(conn_factory):
    conn = conn_factory()
    sink = PostgresEvalSink()
    sink.write(conn, _event("start-open-gate", "turn_started", {"turn_index": 8}))

    class SpyConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.queries = []

        def execute(self, query, *args, **kwargs):
            self.queries.append(str(query))
            return self.wrapped.execute(query, *args, **kwargs)

    spy = SpyConnection(conn)
    _close_turn(spy, _event("finish-open-gate", "turn_completed", {"turn_index": 8}), 1)
    update = next(query for query in spy.queries if "UPDATE eval_turn" in query)
    assert "outcome='open'" in update


def test_sql_sticky_retract_guard_blocks_a_retracted_row_reaching_update(conn_factory):
    conn = conn_factory()
    sink = PostgresEvalSink()
    sink.write(conn, _event("start-sql", "turn_started", {"turn_index": 3}))
    sink.write(conn, _event("retract-sql", "turn_finality_retracted", {"turn_index": 3}))
    conn.execute(
        "UPDATE eval_turn SET outcome='recovered' WHERE run_id='dead-run' AND task_id='dead-task' AND turn_index=3"
    )
    before = conn.execute(
        "SELECT completed_at, latency_ms, outcome, finality_evidence FROM eval_turn WHERE turn_index=3"
    ).fetchone()
    sink.write(conn, _event("final-sql", "turn_finalized", {
        "turn_index": 3, "event_ts": "2026-07-15T00:00:05+00:00",
    }))
    assert conn.execute(
        "SELECT completed_at, latency_ms, outcome, finality_evidence FROM eval_turn WHERE turn_index=3"
    ).fetchone() == before
