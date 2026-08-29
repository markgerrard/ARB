import os
import secrets
import subprocess
import sys

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg import sql

from arb_memory.mcp.grants import apply_local_reader_grants


VAULT_EXPORT_ROLE_PREFIX = "arbmem_vault_export_test_"
SENSITIVE_TABLES = (
    "mcp_auth.oauth_clients",
    "mcp_auth.auth_codes",
    "mcp_auth.access_tokens",
    "mcp_auth.refresh_tokens",
    "mcp_auth.login_sessions",
    "mcp_auth.login_attempts",
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


@pytest.fixture
def vault_export_role(scratch):
    role = f"{VAULT_EXPORT_ROLE_PREFIX}{secrets.token_hex(4)}"
    password = f"vault-export-test-{secrets.token_hex(16)}"
    try:
        scratch.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE; vault-export deny-proof requires role creation")
    try:
        yield role, password
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _has_priv(conn, role, obj, privilege):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, obj, privilege),
    ).fetchone()[0]


def _dsn_with_schema(dsn, schema):
    params = conninfo_to_dict(dsn)
    params["options"] = f"-csearch_path={schema},public"
    return make_conninfo(**params)


def test_vault_export_role_is_select_only_on_hints_and_artefacts(scratch, vault_export_role):
    role, _password = vault_export_role

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


def test_grants_command_applies_vault_export_role(scratch, vault_export_role):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    role, _password = vault_export_role

    env = os.environ.copy()
    env["ARB_MEMORY_DSN"] = _dsn_with_schema(os.environ["ARB_MEMORY_DSN"], schema)
    env["ARB_VAULT_EXPORT_ROLE"] = role
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")

    res = subprocess.run(
        [sys.executable, "-m", "arb_memory", "grants"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert f"vault-export-role='{role}'" in res.stdout
    assert _has_priv(scratch, role, "hints", "SELECT")
    assert _has_priv(scratch, role, "artefacts", "SELECT")
