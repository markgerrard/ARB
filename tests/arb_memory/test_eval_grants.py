import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from arb_memory.mcp.grants import apply_eval_grants, apply_mcp_grants, apply_transcript_grants

pytest_plugins = ("tests.arb_memory.conftest",)


def _has_priv(conn, role, table, priv):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"{conn.execute('SELECT current_schema()').fetchone()[0]}.{table}", priv),
    ).fetchone()[0]


def _has_seq_priv(conn, role, sequence, priv):
    return conn.execute(
        "SELECT has_sequence_privilege(%s, %s, %s)",
        (role, f"{conn.execute('SELECT current_schema()').fetchone()[0]}.{sequence}", priv),
    ).fetchone()[0]


def _has_public_table_priv(conn, table, priv):
    return conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) acl
            WHERE n.nspname = current_schema()
              AND c.relname = %s
              AND acl.grantee = 0
              AND acl.privilege_type = %s
        )
        """,
        (table, priv.upper()),
    ).fetchone()[0]


def _has_public_seq_priv(conn, sequence, priv):
    return conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) acl
            WHERE n.nspname = current_schema()
              AND c.relname = %s
              AND acl.grantee = 0
              AND acl.privilege_type = %s
        )
        """,
        (sequence, priv.upper()),
    ).fetchone()[0]


def _mcp_dsn(schema):
    from arb_memory.mcp.config import mcp_role_name

    dsn = os.environ.get("ARB_MEMORY_MCP_DSN")
    if dsn is None:
        dsn = os.environ["ARB_MEMORY_DSN"].replace(
            "arb_memory:", f"{mcp_role_name()}:", 1
        )
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _eval_test_role(scratch, skip_reason):
    role = os.environ.get("ARB_EVAL_TEST_ROLE")
    if role is not None:
        return role, False

    role = f"arb_eval_test_role_{uuid.uuid4().hex}"
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip(skip_reason)
    return role, True


def _cleanup_eval_test_role(scratch, role, created_by_test):
    scratch.execute("RESET ROLE")
    if created_by_test:
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_eval_role_can_actually_insert_both_eval_tables(scratch):
    # plan-panel P0: privilege-checks alone are vacuous -- the role must actually be able to INSERT
    # (catches a missing sequence grant: "permission denied for sequence" only fires on a real insert).
    role, created_by_test = _eval_test_role(
        scratch,
        "substrate disallows CREATE ROLE; eval-role real-insert proof runs only where role creation is permitted",
    )

    try:
        apply_eval_grants(scratch, role)
        assert _has_priv(scratch, role, "eval_event_raw", "SELECT") is True
        assert _has_priv(scratch, role, "eval_event_raw", "INSERT") is True
        assert _has_priv(scratch, role, "eval_deadletter", "SELECT") is True
        assert _has_priv(scratch, role, "eval_deadletter", "INSERT") is True
        assert _has_seq_priv(scratch, role, "eval_event_raw_id_seq", "USAGE") is True
        assert _has_seq_priv(scratch, role, "eval_event_raw_id_seq", "SELECT") is False
        assert _has_seq_priv(scratch, role, "eval_deadletter_id_seq", "USAGE") is True
        assert _has_seq_priv(scratch, role, "eval_deadletter_id_seq", "SELECT") is False

        if created_by_test:
            scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role
        scratch.execute(
            "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
            "VALUES ('r','t','task_started', now(), '{}', 'seq-test-1')"
        )
        scratch.execute(
            "INSERT INTO eval_deadletter (run_id, task_id, event_type, raw_entry, error, stream_entry_id) "
            "VALUES ('r','t','task_started', '{}', 'x', 'dl-seq-test-1')"
        )
    finally:
        _cleanup_eval_test_role(scratch, role, created_by_test)


def test_eval_grants_revoke_seeded_public_access(scratch):
    role, created_by_test = _eval_test_role(
        scratch,
        "substrate disallows CREATE ROLE; eval PUBLIC revoke proof runs only where role creation is permitted",
    )

    try:
        scratch.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON eval_event_raw, eval_deadletter TO PUBLIC")
        scratch.execute("GRANT USAGE, SELECT ON SEQUENCE eval_event_raw_id_seq, eval_deadletter_id_seq TO PUBLIC")

        apply_eval_grants(scratch, role)

        for table in ("eval_event_raw", "eval_deadletter"):
            assert _has_public_table_priv(scratch, table, "SELECT") is False
            assert _has_public_table_priv(scratch, table, "INSERT") is False
            assert _has_public_table_priv(scratch, table, "UPDATE") is False
            assert _has_public_table_priv(scratch, table, "DELETE") is False
        for sequence in ("eval_event_raw_id_seq", "eval_deadletter_id_seq"):
            assert _has_public_seq_priv(scratch, sequence, "USAGE") is False
            assert _has_public_seq_priv(scratch, sequence, "SELECT") is False
    finally:
        _cleanup_eval_test_role(scratch, role, created_by_test)


def test_eval_role_cannot_touch_audit(scratch):
    role, created_by_test = _eval_test_role(
        scratch,
        "substrate disallows CREATE ROLE; eval-role audit privilege proof runs only where role creation is permitted",
    )

    try:
        apply_eval_grants(scratch, role)
        assert _has_priv(scratch, role, "audit_events", "INSERT") is False
    finally:
        _cleanup_eval_test_role(scratch, role, created_by_test)


def test_transcript_role_can_actually_insert_transcript_tables(scratch):
    role, created_by_test = _eval_test_role(
        scratch,
        "substrate disallows CREATE ROLE; transcript-role real-insert proof runs only where role creation is permitted",
    )

    try:
        apply_transcript_grants(scratch, role)
        assert _has_priv(scratch, role, "transcript_io", "SELECT") is True
        assert _has_priv(scratch, role, "transcript_io", "INSERT") is True
        assert _has_priv(scratch, role, "transcript_deadletter", "SELECT") is True
        assert _has_priv(scratch, role, "transcript_deadletter", "INSERT") is True
        assert _has_seq_priv(scratch, role, "transcript_io_id_seq", "USAGE") is True
        assert _has_seq_priv(scratch, role, "transcript_deadletter_id_seq", "USAGE") is True

        if created_by_test:
            scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role
        scratch.execute(
            "INSERT INTO transcript_io "
            "(run_id, task_id, turn_index, item_id, seq, kind, content, ts, stream_entry_id) "
            "VALUES ('r','t',0,'i',1,'model_text','content', now(), 'transcript-seq-test-1')"
        )
        scratch.execute(
            "INSERT INTO transcript_deadletter (run_id, task_id, kind, raw_entry, error, stream_entry_id) "
            "VALUES ('r','t','model_text', '{}', 'x', 'transcript-dl-seq-test-1')"
        )
    finally:
        _cleanup_eval_test_role(scratch, role, created_by_test)


def test_mcp_role_has_no_audit_or_eval_access(scratch):
    from arb_memory.mcp.config import mcp_role_name

    role = mcp_role_name()
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    apply_mcp_grants(scratch, role)  # conftest already applied; re-apply is idempotent
    assert _has_priv(scratch, role, "audit_events", "SELECT") is False
    assert _has_priv(scratch, role, "eval_event_raw", "SELECT") is False
    assert _has_priv(scratch, role, "eval_event_raw", "INSERT") is False
    assert _has_priv(scratch, role, "eval_deadletter", "SELECT") is False
    assert _has_priv(scratch, role, "eval_deadletter", "INSERT") is False
    assert _has_priv(scratch, role, "transcript_io", "SELECT") is False
    assert _has_priv(scratch, role, "transcript_io", "INSERT") is False
    assert _has_priv(scratch, role, "transcript_deadletter", "SELECT") is False
    assert _has_priv(scratch, role, "transcript_deadletter", "INSERT") is False
    assert _has_seq_priv(scratch, role, "eval_event_raw_id_seq", "USAGE") is False
    assert _has_seq_priv(scratch, role, "eval_deadletter_id_seq", "USAGE") is False
    assert _has_seq_priv(scratch, role, "transcript_io_id_seq", "USAGE") is False
    assert _has_seq_priv(scratch, role, "transcript_deadletter_id_seq", "USAGE") is False

    with psycopg.connect(_mcp_dsn(schema)) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == role
        for table in ("eval_event_raw", "eval_deadletter", "transcript_io", "transcript_deadletter"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                if table == "eval_event_raw":
                    conn.execute(
                        "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
                        "VALUES ('mcp-r','mcp-t','task_started', now(), '{}', 'mcp-seq-test-1')"
                    )
                elif table == "eval_deadletter":
                    conn.execute(
                        "INSERT INTO eval_deadletter (run_id, task_id, event_type, raw_entry, error, stream_entry_id) "
                        "VALUES ('mcp-r','mcp-t','task_started', '{}', 'x', 'mcp-dl-seq-test-1')"
                    )
                elif table == "transcript_io":
                    conn.execute(
                        "INSERT INTO transcript_io "
                        "(run_id, task_id, turn_index, item_id, seq, kind, content, ts, stream_entry_id) "
                        "VALUES ('mcp-r','mcp-t',0,'i',1,'model_text','x', now(), 'mcp-transcript-seq-test-1')"
                    )
                else:
                    conn.execute(
                        "INSERT INTO transcript_deadletter (run_id, task_id, kind, raw_entry, error, stream_entry_id) "
                        "VALUES ('mcp-r','mcp-t','model_text', '{}', 'x', 'mcp-transcript-dl-seq-test-1')"
                    )
            conn.rollback()
