import os
import secrets
import subprocess
import sys

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg import sql

from arb_memory.mcp.grants import apply_local_reader_grants


LOCAL_READER_ROLE_PREFIX = "arbmem_local_reader_test_"
AUTH_TABLES = (
    "oauth_clients",
    "auth_codes",
    "access_tokens",
    "refresh_tokens",
    "login_sessions",
    "login_attempts",
)
SENSITIVE_TABLES = tuple(f"mcp_auth.{table}" for table in AUTH_TABLES) + (
    "audit_events",
    "audit_deadletter",
    "audit_close_deadletter",
    "eval_event_raw",
    "eval_deadletter",
    "transcript_io",
    "transcript_deadletter",
    "write_deadletter",
    "idempotency_keys",
)
INJECTED_PRIVILEGES = (
    ("hints", "INSERT"),
    ("artefacts", "UPDATE"),
    ("mcp_auth.oauth_clients", "SELECT"),
    ("audit_events", "SELECT"),
    ("eval_event_raw", "INSERT"),
    ("transcript_io", "SELECT"),
    ("write_deadletter", "SELECT"),
    ("idempotency_keys", "SELECT"),
)


@pytest.fixture
def local_reader_role(scratch):
    role = f"{LOCAL_READER_ROLE_PREFIX}{secrets.token_hex(4)}"
    password = f"local-reader-test-{secrets.token_hex(16)}"
    try:
        scratch.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE; local reader deny-proof requires role creation")
    try:
        yield role, password
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _local_reader_dsn(schema, role, password):
    params = conninfo_to_dict(os.environ["ARB_MEMORY_DSN"])
    params["user"] = role
    params["password"] = password
    params["options"] = f"-csearch_path={schema},public"
    return make_conninfo(**params)


def _dsn_with_schema(dsn, schema):
    params = conninfo_to_dict(dsn)
    params["options"] = f"-csearch_path={schema},public"
    return make_conninfo(**params)


def _has_priv(conn, role, obj, privilege):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, obj, privilege),
    ).fetchone()[0]


def _grant(conn, role, obj, privilege):
    if "." in obj:
        schema_name, table_name = obj.split(".", 1)
        table_ident = sql.Identifier(schema_name, table_name)
    else:
        table_ident = sql.Identifier(obj)
    conn.execute(
        sql.SQL("GRANT {} ON {} TO {}").format(
            sql.SQL(privilege),
            table_ident,
            sql.Identifier(role),
        )
    )


def test_local_reader_privileges(scratch, local_reader_role):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    role, password = local_reader_role

    apply_local_reader_grants(scratch, role)

    assert _has_priv(scratch, role, "hints", "SELECT")
    assert _has_priv(scratch, role, "artefacts", "SELECT")

    for obj in ("hints", "artefacts"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(scratch, role, obj, privilege), (
                f"{role} must not {privilege} {obj}"
            )

    for obj in SENSITIVE_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(scratch, role, obj, privilege), (
                f"{role} must not {privilege} {obj}"
            )

    with psycopg.connect(_local_reader_dsn(schema, role, password)) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == role
        conn.execute("SELECT 1 FROM hints LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO hints (text, embedding, content_hash) VALUES (%s, %s, %s)",
                    ("x", "[" + ",".join(["0"] * 1536) + "]", "deadbeef"),
                )


def test_revokes_are_load_bearing(scratch, local_reader_role):
    role, _password = local_reader_role

    for obj, privilege in INJECTED_PRIVILEGES:
        _grant(scratch, role, obj, privilege)
        assert _has_priv(scratch, role, obj, privilege)

    apply_local_reader_grants(scratch, role)

    for obj, privilege in INJECTED_PRIVILEGES:
        assert not _has_priv(scratch, role, obj, privilege), (
            f"REVOKE missing for {privilege} {obj}"
        )


def test_has_table_privilege_includes_public_grants(scratch, local_reader_role):
    role, _password = local_reader_role
    scratch.execute("BEGIN")
    try:
        scratch.execute("GRANT SELECT ON audit_events TO PUBLIC")
        assert _has_priv(scratch, role, "audit_events", "SELECT")
    finally:
        scratch.execute("ROLLBACK")


def test_grants_command_applies_local_reader_role(scratch, local_reader_role):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    role, _password = local_reader_role
    _grant(scratch, role, "write_deadletter", "SELECT")
    assert _has_priv(scratch, role, "write_deadletter", "SELECT")

    env = os.environ.copy()
    env["ARB_MEMORY_DSN"] = _dsn_with_schema(os.environ["ARB_MEMORY_DSN"], schema)
    env["ARB_MEMORY_LOCAL_READER_ROLE"] = role
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")

    res = subprocess.run(
        [sys.executable, "-m", "arb_memory", "grants"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert f"local-reader-role='{role}'" in res.stdout
    assert not _has_priv(scratch, role, "write_deadletter", "SELECT")
