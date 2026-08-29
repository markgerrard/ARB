import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from arb_memory.mcp.grants import apply_visibility_grants
from arb_memory.visibility import _backfill_seat


def _test_role(conn):
    role = f"arb_visibility_test_role_{uuid.uuid4().hex}"
    try:
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        pytest.skip("substrate disallows CREATE ROLE; visibility grants proof requires role creation")
    return role


def _cleanup_role(conn, role):
    conn.execute("RESET ROLE")
    conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _has_schema_priv(conn, role, schema, priv):
    return conn.execute(
        "SELECT has_schema_privilege(%s, %s, %s)",
        (role, schema, priv),
    ).fetchone()[0]


def _has_table_priv(conn, role, table, priv):
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"{schema}.{table}", priv),
    ).fetchone()[0]


def _has_auth_table_priv(conn, role, table, priv):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"mcp_auth.{table}", priv),
    ).fetchone()[0]


def _auth_table_exists(conn, table):
    return conn.execute("SELECT to_regclass(%s)", (f"mcp_auth.{table}",)).fetchone()[0] is not None


def _public_has_table_priv(conn, table, priv):
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    return conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.role_table_grants
            WHERE grantee = 'PUBLIC'
              AND table_schema = %s
              AND table_name = %s
              AND privilege_type = %s
        )
        """,
        (schema, table, priv),
    ).fetchone()[0]


def _public_has_auth_table_priv(conn, table, priv):
    return conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.role_table_grants
            WHERE grantee = 'PUBLIC'
              AND table_schema = 'mcp_auth'
              AND table_name = %s
              AND privilege_type = %s
        )
        """,
        (table, priv),
    ).fetchone()[0]


def _assert_auth_table_privs(conn, role, table, expected):
    if not _auth_table_exists(conn, table):
        return
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert _has_auth_table_priv(conn, role, table, priv) is expected


def _assert_public_auth_table_denied(conn, table):
    if not _auth_table_exists(conn, table):
        return
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert _public_has_auth_table_priv(conn, table, priv) is False


def _assert_select_only_table(conn, role, table):
    assert _has_table_priv(conn, role, table, "SELECT") is True
    assert _has_table_priv(conn, role, table, "INSERT") is False
    assert _has_table_priv(conn, role, table, "UPDATE") is False
    assert _has_table_priv(conn, role, table, "DELETE") is False


def _assert_select_only_auth_table(conn, role, table):
    assert _has_auth_table_priv(conn, role, table, "SELECT") is True
    assert _has_auth_table_priv(conn, role, table, "INSERT") is False
    assert _has_auth_table_priv(conn, role, table, "UPDATE") is False
    assert _has_auth_table_priv(conn, role, table, "DELETE") is False


def test_visibility_role_can_read_visibility_surfaces_and_auth_token_only(scratch):
    role = _test_role(scratch)
    try:
        scratch.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.Identifier("mcp_auth", "oauth_clients"),
            sql.Identifier(role),
        ))
        scratch.execute("GRANT SELECT ON mcp_auth.refresh_tokens TO PUBLIC")

        apply_visibility_grants(scratch, role)

        assert _has_schema_priv(scratch, role, "mcp_auth", "USAGE") is True
        assert _has_schema_priv(scratch, role, scratch.execute("SELECT current_schema()").fetchone()[0], "USAGE") is True

        assert _has_table_priv(scratch, role, "audit_events", "SELECT") is True
        _assert_select_only_table(scratch, role, "audit_events")
        _assert_select_only_table(scratch, role, "eval_event_raw")
        _assert_select_only_table(scratch, role, "transcript_io")

        _assert_select_only_auth_table(scratch, role, "access_tokens")

        assert _has_table_priv(scratch, role, "artefacts", "SELECT") is False
        assert _has_table_priv(scratch, role, "hints", "SELECT") is False
        assert _public_has_table_priv(scratch, "artefacts", "SELECT") is False
        assert _public_has_table_priv(scratch, "hints", "SELECT") is False

        for auth_table in ("login_sessions", "login_attempts"):
            _assert_auth_table_privs(scratch, role, auth_table, True)
            _assert_public_auth_table_denied(scratch, auth_table)

        for auth_table in ("oauth_clients", "refresh_tokens"):
            _assert_auth_table_privs(scratch, role, auth_table, False)
            _assert_public_auth_table_denied(scratch, auth_table)

        scratch.execute(
            """
            INSERT INTO eval_event_raw
                (run_id, task_id, seat_id, orchestrator, event_type, sent_at, payload, stream_entry_id)
            VALUES (%s, %s, %s, %s, %s, now(), %s, %s)
            """,
            ("run-vis", "task-vis", "seat-vis", "orch", "task_started", Jsonb({}), f"eval-{uuid.uuid4().hex}"),
        )
        scratch.execute(
            """
            INSERT INTO audit_events (run_id, seq, source, kind, ts, payload)
            VALUES (%s, %s, %s, %s, now(), %s)
            """,
            ("run-vis", 1, "seat:seat-vis", "vote", Jsonb({"actor": "seat:seat-vis", "stance": "approve"})),
        )

        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        events = _backfill_seat(scratch, "task-vis")
        assert {event["source"] for event in events} == {"eval", "audit"}
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute("REVOKE SELECT ON mcp_auth.refresh_tokens FROM PUBLIC")
        _cleanup_role(scratch, role)
