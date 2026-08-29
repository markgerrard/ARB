import os
import uuid

import psycopg
import pytest
from psycopg import sql

from arb_memory.mcp.grants import apply_retention_grants
from arb_memory.eval import purge_expired as purge_eval_expired
from arb_memory.transcript import purge_expired as purge_transcript_expired


def _role(scratch):
    role = f"arb_retention_test_{uuid.uuid4().hex}"
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE")
    return role


def test_retention_role_is_raw_delete_only_and_cannot_read_spans(scratch):
    role = _role(scratch)
    try:
        apply_retention_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        for table in ("eval_event_raw", "transcript_io"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, f"{schema}.{table}")
            ).fetchone()[0] is True
            assert scratch.execute(
                "SELECT has_column_privilege(%s, %s, 'inserted_at', 'SELECT')",
                (role, f"{schema}.{table}"),
            ).fetchone()[0] is True
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"{schema}.{table}")
            ).fetchone()[0] is True
        for table in ("eval_turn", "eval_tool_call", "eval_task", "span_deadletter"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"{schema}.{table}")
            ).fetchone()[0] is False
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_retention_role_executes_ctid_purges_from_second_role_connection(scratch):
    role = _role(scratch)
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    try:
        apply_retention_grants(scratch, role)
        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(
            "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id, inserted_at) "
            "VALUES ('retention', 'eval', 'task_started', now(), '{}', 'retention-eval-old', now() - interval '90 days')"
        )
        scratch.execute(
            "INSERT INTO transcript_io (run_id, task_id, item_id, kind, content, ts, stream_entry_id, inserted_at) "
            "VALUES ('retention', 'transcript', 'i', 'text', 'old', now(), 'retention-transcript-old', now() - interval '90 days')"
        )
        with psycopg.connect(os.environ["ARB_MEMORY_DSN"]) as role_conn:
            role_conn.autocommit = True
            role_conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            role_conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert purge_eval_expired(role_conn, older_than_days=30) == 1
            assert purge_transcript_expired(role_conn, older_than_days=30) == 1
        assert scratch.execute(
            "SELECT count(*) FROM eval_event_raw WHERE stream_entry_id='retention-eval-old'"
        ).fetchone()[0] == 0
        assert scratch.execute(
            "SELECT count(*) FROM transcript_io WHERE stream_entry_id='retention-transcript-old'"
        ).fetchone()[0] == 0
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {} ").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
