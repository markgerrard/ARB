"""S1: hint_read / hint_read_hit / hint_read_deadletter schema.

Frozen guide §6 + BUILD-CHARTER D-2 (query_hmac default; query_text nullable opt-in).
"""
import os
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

SCHEMA_SQL = Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql"

HINT_READ_TABLES = ("hint_read", "hint_read_hit", "hint_read_deadletter")


def test_hint_read_tables_exist(scratch):
    for table in HINT_READ_TABLES:
        oid = scratch.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
        assert oid is not None, f"table {table} missing"


def test_hint_read_columns_include_hmac_shape(scratch):
    """BUILD-CHARTER D-2: query_hmac + nullable query_text; not the guide's NOT NULL query_text."""
    cols = {
        r[0]: (r[1], r[2], r[3])
        for r in scratch.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'hint_read'
            """
        ).fetchall()
    }
    required = {
        "read_id",
        "door",
        "outcome",
        "run_id",
        "seat_id",
        "query_hmac",
        "query_text",
        "query_truncated",
        "k",
        "hit_count",
        "cid",
        "stream_entry_id",
        "served_at",
    }
    assert required <= set(cols), f"missing columns: {required - set(cols)}"
    assert cols["query_hmac"][1] == "YES"
    assert cols["query_text"][1] == "YES"
    assert cols["query_truncated"][1] == "NO"
    assert cols["query_truncated"][2] is not None
    assert "false" in cols["query_truncated"][2]


def test_query_truncated_not_null_default_false(scratch):
    read_id = str(uuid.uuid4())
    scratch.execute(
        """
        INSERT INTO hint_read (read_id, door, outcome, k, hit_count)
        VALUES (%s, 'local', 'ok', 5, 0)
        """,
        (read_id,),
    )
    row = scratch.execute(
        "SELECT query_truncated, query_hmac, query_text FROM hint_read WHERE read_id = %s",
        (read_id,),
    ).fetchone()
    assert row[0] is False
    assert row[1] is None
    assert row[2] is None

    with pytest.raises(psycopg.errors.NotNullViolation):
        scratch.execute(
            """
            INSERT INTO hint_read (read_id, door, outcome, k, hit_count, query_truncated)
            VALUES (%s, 'local', 'ok', 1, 0, NULL)
            """,
            (str(uuid.uuid4()),),
        )


def test_hint_read_hit_fk_cascades_on_delete(scratch):
    read_id = str(uuid.uuid4())
    scratch.execute(
        """
        INSERT INTO hint_read (read_id, door, outcome, k, hit_count)
        VALUES (%s, 'bus', 'ok', 3, 1)
        """,
        (read_id,),
    )
    scratch.execute(
        """
        INSERT INTO hint_read_hit (read_id, rank, hint_id)
        VALUES (%s, 1, 42)
        """,
        (read_id,),
    )
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read_hit WHERE read_id = %s", (read_id,)
        ).fetchone()[0]
        == 1
    )
    scratch.execute("DELETE FROM hint_read WHERE read_id = %s", (read_id,))
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read_hit WHERE read_id = %s", (read_id,)
        ).fetchone()[0]
        == 0
    )


def test_hint_read_deadletter_exists_with_unique_stream_entry_id(scratch):
    cols = {
        r[0]
        for r in scratch.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'hint_read_deadletter'
            """
        ).fetchall()
    }
    assert {"id", "stream_entry_id", "raw_entry", "error", "ts"} <= cols

    scratch.execute(
        """
        INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error)
        VALUES ('stream:1-0', '{}'::jsonb, 'bad')
        """
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        scratch.execute(
            """
            INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error)
            VALUES ('stream:1-0', '{}'::jsonb, 'again')
            """
        )


def test_schema_sql_reappliable_with_hint_read_tables(scratch):
    """schema.sql must remain re-appliable — applying it twice in one session must not error."""
    schema_sql = SCHEMA_SQL.read_text(encoding="utf-8")
    scratch.execute(schema_sql)  # second apply (scratch already applied once)
    for table in HINT_READ_TABLES:
        oid = scratch.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
        assert oid is not None, f"table {table} missing after re-apply"
