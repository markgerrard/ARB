"""S2: hint_read grants — seeded PUBLIC deny-proof + real SET ROLE insert proofs."""

import os
import uuid

import psycopg
import pytest
from psycopg import sql

from arb_memory.mcp.grants import (
    apply_eval_grants,
    apply_hint_read_consumer_grants,
    apply_hint_read_local_writer_grants,
    apply_local_reader_grants,
    apply_mcp_grants,
)

pytest_plugins = ("tests.arb_memory.conftest",)

HINT_READ_TABLES = ("hint_read", "hint_read_hit", "hint_read_deadletter")
TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE")


def _has_priv(conn, role, table, priv):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"{conn.execute('SELECT current_schema()').fetchone()[0]}.{table}", priv),
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


def _test_role(scratch, skip_reason):
    role = os.environ.get("ARB_HINT_READ_TEST_ROLE")
    if role is not None:
        return role, False

    role = f"arb_hint_read_test_role_{uuid.uuid4().hex}"
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip(skip_reason)
    return role, True


def _cleanup_test_role(scratch, role, created_by_test):
    scratch.execute("RESET ROLE")
    if created_by_test:
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _seed_public_hint_read_grants(scratch):
    """Ambient PUBLIC grants the apply functions must revoke — without this the deny-proof is vacuous."""
    scratch.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON hint_read, hint_read_hit, hint_read_deadletter TO PUBLIC"
    )


def test_local_reader_grants_revoke_seeded_public_on_all_three_hint_read_tables(scratch):
    role, created = _test_role(
        scratch,
        "substrate disallows CREATE ROLE; hint_read PUBLIC revoke proof runs only where role creation is permitted",
    )
    try:
        _seed_public_hint_read_grants(scratch)
        for table in HINT_READ_TABLES:
            assert _has_public_table_priv(scratch, table, "SELECT") is True

        apply_local_reader_grants(scratch, role)

        for table in HINT_READ_TABLES:
            for priv in TABLE_PRIVS:
                assert _has_public_table_priv(scratch, table, priv) is False, (
                    f"PUBLIC still holds {priv} on {table} after apply_local_reader_grants"
                )
            for priv in TABLE_PRIVS:
                assert _has_priv(scratch, role, table, priv) is False, (
                    f"local_reader role still holds {priv} on {table}"
                )
    finally:
        _cleanup_test_role(scratch, role, created)


def test_mcp_grants_revoke_seeded_public_on_all_three_hint_read_tables(scratch):
    role, created = _test_role(
        scratch,
        "substrate disallows CREATE ROLE; mcp PUBLIC revoke proof runs only where role creation is permitted",
    )
    try:
        _seed_public_hint_read_grants(scratch)
        for table in HINT_READ_TABLES:
            assert _has_public_table_priv(scratch, table, "INSERT") is True

        apply_mcp_grants(scratch, role)

        for table in HINT_READ_TABLES:
            for priv in TABLE_PRIVS:
                assert _has_public_table_priv(scratch, table, priv) is False, (
                    f"PUBLIC still holds {priv} on {table} after apply_mcp_grants"
                )
            for priv in TABLE_PRIVS:
                assert _has_priv(scratch, role, table, priv) is False, (
                    f"mcp role still holds {priv} on {table}"
                )
    finally:
        _cleanup_test_role(scratch, role, created)


def test_consumer_role_can_insert_hint_read_deadletter_via_set_role(scratch):
    """Real INSERT under SET ROLE — proves SEQUENCE USAGE on hint_read_deadletter_id_seq (H-01)."""
    role, created = _test_role(
        scratch,
        "substrate disallows CREATE ROLE; consumer real-insert proof runs only where role creation is permitted",
    )
    try:
        # Production order: apply_eval_grants first (schema USAGE), then hint_read consumer.
        apply_eval_grants(scratch, role)
        apply_hint_read_consumer_grants(scratch, role)
        assert _has_priv(scratch, role, "hint_read", "SELECT") is True
        assert _has_priv(scratch, role, "hint_read", "INSERT") is True
        assert _has_priv(scratch, role, "hint_read_hit", "SELECT") is True
        assert _has_priv(scratch, role, "hint_read_hit", "INSERT") is True
        assert _has_priv(scratch, role, "hint_read_deadletter", "SELECT") is True
        assert _has_priv(scratch, role, "hint_read_deadletter", "INSERT") is True
        assert _has_priv(scratch, role, "hint_read_deadletter", "UPDATE") is False
        assert _has_priv(scratch, role, "hint_read_deadletter", "DELETE") is False

        if created:
            scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role
        scratch.execute(
            "INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error) "
            "VALUES (%s, %s::jsonb, %s)",
            (f"dl-{uuid.uuid4().hex}", '{"k":1}', "test-error"),
        )
    finally:
        _cleanup_test_role(scratch, role, created)


def test_local_reader_can_insert_hint_read_but_not_select(scratch):
    role, created = _test_role(
        scratch,
        "substrate disallows CREATE ROLE; local-writer real-insert proof runs only where role creation is permitted",
    )
    try:
        apply_local_reader_grants(scratch, role)
        # Ambient SELECT the writer function must strip; without this the assertion is vacuous
        # (apply_local_reader_grants already REVOKE ALL'd these tables).
        scratch.execute(
            sql.SQL("GRANT SELECT ON hint_read, hint_read_hit TO {}").format(sql.Identifier(role))
        )
        assert _has_priv(scratch, role, "hint_read", "SELECT") is True
        assert _has_priv(scratch, role, "hint_read_hit", "SELECT") is True
        apply_hint_read_local_writer_grants(scratch, role)

        assert _has_priv(scratch, role, "hint_read", "INSERT") is True
        assert _has_priv(scratch, role, "hint_read_hit", "INSERT") is True
        assert _has_priv(scratch, role, "hint_read", "SELECT") is False
        assert _has_priv(scratch, role, "hint_read_hit", "SELECT") is False

        if created:
            scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role

        read_id = str(uuid.uuid4())
        scratch.execute(
            "INSERT INTO hint_read "
            "(read_id, door, outcome, k, hit_count) "
            "VALUES (%s::uuid, 'local', 'ok', 5, 0)",
            (read_id,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            scratch.execute("SELECT count(*) FROM hint_read")
    finally:
        _cleanup_test_role(scratch, role, created)


def test_vault_export_role_has_no_hint_read_access_after_local_reader_grants(scratch):
    """vault_export_role gets apply_local_reader_grants only — never the local-writer grants (G-03)."""
    role, created = _test_role(
        scratch,
        "substrate disallows CREATE ROLE; vault_export deny proof runs only where role creation is permitted",
    )
    try:
        apply_local_reader_grants(scratch, role)

        for table in HINT_READ_TABLES:
            for priv in TABLE_PRIVS:
                assert _has_priv(scratch, role, table, priv) is False, (
                    f"vault_export role unexpectedly holds {priv} on {table}"
                )

        if created:
            scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            scratch.execute(
                "INSERT INTO hint_read "
                "(read_id, door, outcome, k, hit_count) "
                "VALUES (%s::uuid, 'local', 'ok', 1, 0)",
                (str(uuid.uuid4()),),
            )
        scratch.rollback()
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            scratch.execute("SELECT count(*) FROM hint_read")
    finally:
        _cleanup_test_role(scratch, role, created)
