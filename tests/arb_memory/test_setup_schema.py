import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from arb_memory import run


@pytest.fixture
def empty_schema_conn():
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        pytest.skip("no ARB_MEMORY_DSN")

    schema = f"arb_setup_schema_{uuid.uuid4().hex}"
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    try:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        yield conn
    finally:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


def _regclass(conn, name):
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    return conn.execute("SELECT to_regclass(%s)", (f"{schema}.{name}",)).fetchone()[0]


def test_setup_schema_creates_only_eval_and_transcript_tables_idempotently(empty_schema_conn):
    run.setup_schema(empty_schema_conn)

    for relation in (
        "eval_event_raw",
        "eval_deadletter",
        "transcript_io",
        "transcript_deadletter",
        "eval_event_raw_run_idx",
        "eval_event_raw_task_idx",
        "eval_event_raw_inserted_at_idx",
        "eval_event_raw_orchestrator_task_sent_idx",
        "transcript_io_task_ts_idx",
        "transcript_io_ts_idx",
    ):
        assert _regclass(empty_schema_conn, relation) is not None

    for memory_or_auth_relation in (
        "artefacts",
        "hints",
        "access_tokens",
    ):
        assert _regclass(empty_schema_conn, memory_or_auth_relation) is None

    run.setup_schema(empty_schema_conn)

    assert _regclass(empty_schema_conn, "eval_event_raw") is not None
    assert _regclass(empty_schema_conn, "artefacts") is None


def test_setup_schema_cli_choice_dispatches(monkeypatch):
    called = []

    monkeypatch.setattr(run, "run_setup_schema", lambda: called.append(True))

    assert run.main(["setup-schema"]) == 0
    assert called == [True]


def test_setup_schema_concurrent_index_works_on_non_autocommit_connection():
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        pytest.skip("no ARB_MEMORY_DSN")

    schema = f"arb_setup_schema_noauto_{uuid.uuid4().hex}"
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    conn.autocommit = False
    try:
        run.setup_schema(conn)
        conn.commit()
        assert _regclass(conn, "eval_event_raw_orchestrator_task_sent_idx") is not None
    finally:
        conn.rollback()
        conn.autocommit = True
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.close()
