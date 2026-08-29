import os
import subprocess
import sys
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from arb_memory.mcp.config import mcp_role_name
from arb_memory.mcp.grants import GATE_READER_ROLE


def _dsn_with_schema(dsn, schema):
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _ensure_role(conn, role, *, attrs=""):
    exists = conn.execute(
        "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = %s)",
        (role,),
    ).fetchone()[0]
    if exists:
        return False
    try:
        stmt = "CREATE ROLE {}" + (f" {attrs}" if attrs else "")
        conn.execute(sql.SQL(stmt).format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        pytest.skip("substrate disallows CREATE ROLE; grants command deny-proof requires mcp role")
    return True


def _cleanup_role(conn, role, created_by_test):
    conn.execute("RESET ROLE")
    if created_by_test:
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _has_priv(conn, role, table, priv):
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, f"{schema}.{table}", priv),
    ).fetchone()[0]


def _grants_env(command_dsn, **extra):
    """Subprocess env for `python -m arb_memory grants`.

    Scrub ambient gate vars so an operator shell cannot change the subject.
    """
    scrub = {
        "ARB_EVAL_CONSUMER_ROLE",
        "ARB_GATE_READER_ROLE",
        "ARB_GATE_READER_DSN",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in scrub
    }
    env["ARB_MEMORY_DSN"] = command_dsn
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    env.update(extra)
    return env


def test_grants_command_revokes_eval_from_mcp_role(scratch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]

    mcp_role = mcp_role_name()
    created_by_test = _ensure_role(scratch, mcp_role)
    command_dsn = _dsn_with_schema(dsn, schema)
    with psycopg.connect(command_dsn) as conn:
        consumer_role = conn.info.user

    try:
        scratch.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema),
            sql.Identifier(mcp_role),
        ))
        scratch.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(mcp_role),
        ))
        assert _has_priv(scratch, mcp_role, "eval_event_raw", "SELECT") is True

        res = subprocess.run(
            [sys.executable, "-m", "arb_memory", "grants"],
            capture_output=True,
            text=True,
            env=_grants_env(command_dsn),
        )
        assert res.returncode == 0, res.stderr

        assert _has_priv(scratch, mcp_role, "eval_event_raw", "SELECT") is False
        assert _has_priv(scratch, consumer_role, "eval_event_raw", "INSERT") is True
    finally:
        _cleanup_role(scratch, mcp_role, created_by_test)


def test_grants_command_applies_gate_reader_role(scratch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    role = f"{GATE_READER_ROLE}_run_{uuid.uuid4().hex[:8]}"
    created = _ensure_role(scratch, role, attrs="LOGIN")
    if not created and not scratch.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone():
        pytest.skip("cannot create roles on this credential")
    command_dsn = _dsn_with_schema(dsn, schema)
    try:
        res = subprocess.run(
            [sys.executable, "-m", "arb_memory", "grants"],
            capture_output=True,
            text=True,
            env=_grants_env(command_dsn, ARB_GATE_READER_ROLE=role),
        )
        assert res.returncode == 0, res.stderr + res.stdout
        assert role in res.stdout or "gate-reader" in res.stdout

        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            assert _has_priv(scratch, role, view, "SELECT") is True, view
        for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert _has_priv(scratch, role, table, priv) is False, (
                    f"{role} holds {priv} on {table}"
                )
    finally:
        _cleanup_role(scratch, role, True)


def test_grants_command_refuses_unisolated_gate_reader_role(scratch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    gate = f"{GATE_READER_ROLE}_ni_{uuid.uuid4().hex[:8]}"
    parent = f"{GATE_READER_ROLE}_parent_{uuid.uuid4().hex[:8]}"
    created_gate = _ensure_role(scratch, gate, attrs="LOGIN")
    created_parent = _ensure_role(scratch, parent, attrs="NOLOGIN")
    if not created_gate:
        pytest.skip("cannot create roles on this credential")
    command_dsn = _dsn_with_schema(dsn, schema)
    try:
        scratch.execute(sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(parent), sql.Identifier(gate)
        ))
        res = subprocess.run(
            [sys.executable, "-m", "arb_memory", "grants"],
            capture_output=True,
            text=True,
            env=_grants_env(command_dsn, ARB_GATE_READER_ROLE=gate),
        )
        assert res.returncode != 0, res.stdout
        combined = res.stderr + res.stdout
        assert "GateRoleNotIsolated" in combined or "member" in combined.lower()
        # Must not have certified the role with SELECT on the views.
        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            # After abort, either no grant or the command rolled back.
            # The helper raises before GRANT, so SELECT should be False.
            assert _has_priv(scratch, gate, view, "SELECT") is False, view
    finally:
        _cleanup_role(scratch, gate, True)
        _cleanup_role(scratch, parent, created_parent)
