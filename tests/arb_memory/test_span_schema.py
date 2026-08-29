import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")


def test_setup_schema_creates_span_tables_and_natural_keys(empty_schema_conn):
    from arb_memory import run

    run.setup_schema(empty_schema_conn)
    for table in ("eval_turn", "eval_tool_call", "eval_task", "span_deadletter"):
        assert empty_schema_conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]

    constraints = empty_schema_conn.execute(
        """
        SELECT table_name, constraint_name, pg_get_constraintdef(c.oid)
        FROM information_schema.table_constraints tc
        JOIN pg_constraint c ON c.conname = tc.constraint_name
        WHERE tc.table_schema = current_schema()
          AND tc.table_name IN ('eval_turn', 'eval_tool_call', 'eval_task', 'span_deadletter')
          AND tc.constraint_type = 'UNIQUE'
        """
    ).fetchall()
    defs = {table: definition for table, _name, definition in constraints}
    assert "(run_id, task_id, turn_index)" in defs["eval_turn"]
    assert "(run_id, task_id, tool_call_id)" in defs["eval_tool_call"]
    assert "(run_id, task_id)" in defs["eval_task"]
    assert "stream_entry_id" in defs["span_deadletter"]
    assert all("attempt_epoch" not in definition for definition in defs.values())


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("eval_turn", "run_id, task_id, attempt_epoch, turn_index, outcome"),
        ("eval_tool_call", "run_id, task_id, attempt_epoch, turn_index, tool_call_id, outcome"),
        ("eval_task", "run_id, task_id, attempt_epoch, outcome"),
    ],
)
def test_span_outcome_checks_reject_unknown_values(empty_schema_conn, table, columns):
    from arb_memory import run

    run.setup_schema(empty_schema_conn)
    values = {
        "eval_turn": "'r', 't', 1, 0, 'bogus'",
        "eval_tool_call": "'r', 't', 1, 0, 'c', 'bogus'",
        "eval_task": "'r', 't', 1, 'bogus'",
    }
    with pytest.raises(psycopg.errors.CheckViolation):
        empty_schema_conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({values[table]})"
        )


def test_schema_sql_alone_creates_span_tables(scratch):
    for table in ("eval_turn", "eval_tool_call", "eval_task", "span_deadletter"):
        assert scratch.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
