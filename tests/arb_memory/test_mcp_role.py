import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest

from arb_memory.mcp.config import mcp_role_name


def _mcp_dsn(schema):
    dsn = os.environ.get("ARB_MEMORY_MCP_DSN")
    if dsn is None:
        dsn = os.environ["ARB_MEMORY_DSN"].replace(
            "arb_memory:", f"{mcp_role_name()}:"
        )
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def test_mcp_role_cannot_write_memory_or_audit(scratch, fake_embed):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    with psycopg.connect(_mcp_dsn(schema)) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == mcp_role_name()
        assert conn.execute("SELECT count(*) FROM hints").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM artefacts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'hints', 'INSERT')"
        ).fetchone()[0] is False
        # audit is the most sensitive table: the door must have NO access at all, not just no-write.
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'audit_events', 'SELECT')"
        ).fetchone()[0] is False

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """
                INSERT INTO artefacts (artefact_id, version, content, content_hash)
                VALUES ('a1', 1, 'x', 'hash-a1')
                """
            )
        conn.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO hints (text, embedding, content_hash) VALUES (%s, %s, %s)",
                ("x", fake_embed("x"), "hash-h1"),
            )
        conn.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """
                INSERT INTO audit_events (run_id, seq, source, payload)
                VALUES ('run-1', 1, 'test', '{}'::jsonb)
                """
            )


def test_mcp_role_can_write_its_own_auth_schema(scratch):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    with psycopg.connect(_mcp_dsn(schema)) as conn:
        conn.execute(
            """
            INSERT INTO mcp_auth.oauth_clients (client_id, redirect_uris, metadata)
            VALUES ('c1', '[]'::jsonb, '{}'::jsonb)
            """
        )
        conn.execute("DELETE FROM mcp_auth.oauth_clients WHERE client_id = 'c1'")
