"""Live-Postgres integration for per-seat lane writer authority (Stage 1d-i).

Requires ARB_MEMORY_DSN. A skip is not a pass — export the owner DSN first.
Builds the scratch schema from checked-in schema.sql bytes (same as production),
then runs the real grants apply path. No Python-authored function bodies.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN"
)

from agent_redis_bridge.lane_writer import (  # noqa: E402
    LANE_WRITER_FUNCTIONS,
    LANE_WRITER_PRIVILEGE_COVERAGE,
    LANE_WRITER_RELATIONS,
    LaneStoreUnreachable,
    PG_COLUMN_PRIVILEGES,
    PG_TABLE_PRIVILEGES,
    PsycopgLaneWriter,
)
from arb_memory.mcp.grants import (  # noqa: E402
    GATE_READER_ROLE,
    apply_gate_lane_writer_grants,
    apply_gate_reader_grants,
    assert_gate_lane_writer_isolation,
)


_CREATEROLE_SECURITY_SKIP = (
    "SECURITY_SKIP: CREATEROLE required — live lane-writer deny-proof is "
    "INERT on this credential (cannot create roles)"
)


def _role_name(suffix: str) -> str:
    return f"arb_gate_lw_{suffix}_{uuid.uuid4().hex[:8]}"


def _ensure_role(conn, role: str, *, attrs: str = "LOGIN") -> bool:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists:
        return True
    try:
        conn.execute(
            sql.SQL("CREATE ROLE {} " + attrs).format(sql.Identifier(role))
        )
        return True
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return False


def _require_role(conn, role: str, *, attrs: str = "LOGIN") -> None:
    if not _ensure_role(conn, role, attrs=attrs):
        pytest.skip(_CREATEROLE_SECURITY_SKIP)


def _drop_role(conn, role: str) -> None:
    conn.execute("RESET ROLE")
    try:
        conn.execute(
            sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
        )
    except Exception:
        conn.rollback()
    conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _schema(conn) -> str:
    return conn.execute("SELECT current_schema()").fetchone()[0]


def _dsn_for_login(owner_dsn: str, role: str, password: str, schema: str) -> str:
    """Build a DSN that authenticates as ``role`` (session_user binding requires it).

    SECURITY DEFINER functions look up bindings by session_user, so SET ROLE
    from the owner is not a valid test path — it would leave session_user as
    the owner and break the identity binding under test.
    """
    parts = urlsplit(owner_dsn)
    # netloc is user:pass@host:port — rebuild with role/password.
    host = parts.hostname or "localhost"
    port = parts.port
    hostport = f"{host}:{port}" if port else host
    netloc = f"{role}:{password}@{hostport}"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query), parts.fragment)
    )


def _require_login_role(conn, role: str, password: str) -> None:
    """Create a LOGIN role with password (or reset password if it exists)."""
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    try:
        # PASSWORD cannot be a bind parameter in CREATE/ALTER ROLE.
        if exists:
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        else:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        # CONNECT on the current database.
        db = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db), sql.Identifier(role)
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        pytest.skip(_CREATEROLE_SECURITY_SKIP)


def _writer_connect_factory(owner_dsn: str, schema: str, role: str, password: str):
    role_dsn = _dsn_for_login(owner_dsn, role, password, schema)

    def connect(dsn, **kwargs):
        # Authenticate as the seat role so session_user matches the binding.
        return psycopg.connect(role_dsn, **kwargs)

    return connect


def _probed_live_kill_cases() -> list[tuple[str, str, str, str]]:
    """(kind, name, priv, form) derived from LANE_WRITER_PRIVILEGE_COVERAGE."""
    cases: list[tuple[str, str, str, str]] = []
    for (kind, name, priv), tag in sorted(LANE_WRITER_PRIVILEGE_COVERAGE.items()):
        if not tag.startswith("probed-denied-by-"):
            continue
        if kind in {"base_table", "view", "binding_table"}:
            if "relation" in tag:
                cases.append((kind, name, priv, "relation"))
            if "column" in tag and priv in PG_COLUMN_PRIVILEGES:
                cases.append((kind, name, priv, "column"))
        elif kind == "CHANNEL":
            cases.append((kind, name, priv, "channel"))
    return cases


# One real column per relation for column-form GRANT isolation.
_REL_COLUMN: dict[str, str] = {
    "claims": "claim_id",
    "attestations": "claim_id",
    "seat_posture": "seat_id",
    "lease_lanes": "lease_id",
    "lane_writer_bindings": "db_role",
    "claim_admissibility_v": "claim_id",
    "seat_posture_v": "seat_id",
    "lease_lane_v": "lease_id",
}


def test_seat_a_and_b_bound_isolation_matrix(scratch):
    """Positive + cross-seat authority: A gated, B exempt; no forge/cross-retire."""
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    role_a = _role_name("a")
    role_b = _role_name("b")
    pw_a = f"pw_{uuid.uuid4().hex}"
    pw_b = f"pw_{uuid.uuid4().hex}"
    _require_login_role(scratch, role_a, pw_a)
    _require_login_role(scratch, role_b, pw_b)
    try:
        apply_gate_lane_writer_grants(
            scratch, role_a, consumer_id="consumer-a", lane="gated"
        )
        apply_gate_lane_writer_grants(
            scratch, role_b, consumer_id="consumer-b", lane="exempt"
        )

        writer_a = PsycopgLaneWriter(
            "unused",
            expected_role=role_a,
            expected_consumer_id="consumer-a",
            expected_lane="gated",
            schema=schema,
            connect=_writer_connect_factory(owner_dsn, schema, role_a, pw_a),
        )
        writer_b = PsycopgLaneWriter(
            "unused",
            expected_role=role_b,
            expected_consumer_id="consumer-b",
            expected_lane="exempt",
            schema=schema,
            connect=_writer_connect_factory(owner_dsn, schema, role_b, pw_b),
        )
        try:
            writer_a.assert_ready()
            writer_b.assert_ready()

            row_a = writer_a.arm("lease-a-1")
            assert row_a["lane"] == "gated"
            assert row_a["armed_by"] == "consumer-a"

            row_b = writer_b.arm("lease-b-1")
            assert row_b["lane"] == "exempt"
            assert row_b["armed_by"] == "consumer-b"

            # A lists only A.
            listed_a = writer_a.rows()
            assert {r["lease_id"] for r in listed_a} == {"lease-a-1"}
            listed_b = writer_b.rows()
            assert {r["lease_id"] for r in listed_b} == {"lease-b-1"}

            # A cannot retire B; B cannot retire A.
            assert writer_a.retire("lease-b-1") is False
            assert writer_b.retire("lease-a-1") is False
            # Rows still present under owner view.
            assert scratch.execute(
                "SELECT count(*) FROM lease_lanes WHERE lease_id = 'lease-b-1'"
            ).fetchone()[0] == 1
            assert scratch.execute(
                "SELECT count(*) FROM lease_lanes WHERE lease_id = 'lease-a-1'"
            ).fetchone()[0] == 1

            # Own retire succeeds.
            assert writer_a.retire("lease-a-1") is True
            assert writer_b.retire("lease-b-1") is True
        finally:
            writer_a.close()
            writer_b.close()

        # Raw table DML as seat A is 42501 (session authenticated as A).
        with psycopg.connect(
            _dsn_for_login(owner_dsn, role_a, pw_a, schema)
        ) as a_conn:
            a_conn.autocommit = True
            for stmt in (
                "SELECT 1 FROM lease_lanes LIMIT 1",
                "INSERT INTO lease_lanes (lease_id, lane, armed_by) "
                "VALUES ('raw', 'exempt', 'forged')",
                "DELETE FROM lease_lanes WHERE lease_id = 'x'",
                "SELECT 1 FROM lane_writer_bindings LIMIT 1",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    a_conn.execute(stmt)
    finally:
        _drop_role(scratch, role_a)
        _drop_role(scratch, role_b)


def test_reader_still_selects_views_and_cannot_execute_writer_functions(scratch):
    role = f"{GATE_READER_ROLE}_lw_{uuid.uuid4().hex[:8]}"
    _require_role(scratch, role)
    scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        # Apply a writer too so functions have ACLs.
        lw = _role_name("rdr_x")
        _require_role(scratch, lw)
        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(lw)))
        try:
            apply_gate_lane_writer_grants(
                scratch, lw, consumer_id="consumer-r", lane="gated"
            )
            scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            try:
                for view in (
                    "claim_admissibility_v",
                    "seat_posture_v",
                    "lease_lane_v",
                ):
                    scratch.execute(f"SELECT 1 FROM {view} LIMIT 1")
                for call in (
                    "SELECT arm_lease_lane('x')",
                    "SELECT retire_lease_lane('x')",
                    "SELECT * FROM list_lease_lanes()",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        scratch.execute(call)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    scratch.execute(
                        "INSERT INTO lease_lanes (lease_id, lane, armed_by) "
                        "VALUES ('r', 'gated', 'reader')"
                    )
            finally:
                scratch.execute("RESET ROLE")
        finally:
            _drop_role(scratch, lw)
    finally:
        _drop_role(scratch, role)


def test_unbound_role_refuses_function_calls(scratch):
    role = _role_name("unbound")
    _require_role(scratch, role)
    scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
    schema = _schema(scratch)
    try:
        # USAGE + EXECUTE without a binding row.
        scratch.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        for sig in (
            sql.SQL("{}(text)").format(sql.Identifier(schema, "arm_lease_lane")),
            sql.SQL("{}(text)").format(sql.Identifier(schema, "retire_lease_lane")),
            sql.SQL("{}()").format(sql.Identifier(schema, "list_lease_lanes")),
        ):
            scratch.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                    sig, sql.Identifier(role)
                )
            )
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        try:
            with pytest.raises(psycopg.Error, match=r"unbound-lane-writer-role"):
                scratch.execute("SELECT arm_lease_lane('u1')")
        finally:
            scratch.execute("RESET ROLE")
    finally:
        _drop_role(scratch, role)


def test_isolation_assertion_refuses_membership(scratch):
    gate = _role_name("iso_m")
    parent = _role_name("iso_p")
    _require_role(scratch, gate)
    _require_role(scratch, parent, attrs="NOLOGIN")
    try:
        scratch.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(parent), sql.Identifier(gate)
            )
        )
        with pytest.raises(Exception, match=r"member"):
            assert_gate_lane_writer_isolation(scratch, gate)
    finally:
        _drop_role(scratch, gate)
        _drop_role(scratch, parent)


@pytest.mark.parametrize(
    ("kind", "name", "priv", "form"),
    _probed_live_kill_cases(),
    ids=[f"{k}-{n}-{p}-{f}" for k, n, p, f in _probed_live_kill_cases()],
)
def test_readiness_refuses_every_probed_coverage_pair(
    scratch, kind, name, priv, form
):
    """LIVE kill per coverage pair — the kill is the artefact (1c standard)."""
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    role = _role_name(f"cov_{priv[:3].lower()}_{form[:3]}")
    password = f"pw_{uuid.uuid4().hex}"
    _require_login_role(scratch, role, password)
    try:
        if kind == "CHANNEL":
            if name == "membership":
                parent = _role_name("par")
                _require_role(scratch, parent, attrs="NOLOGIN")
                try:
                    apply_gate_lane_writer_grants(
                        scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
                    )
                    scratch.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier(parent), sql.Identifier(role)
                        )
                    )
                    writer = PsycopgLaneWriter(
                        "unused",
                        expected_role=role,
                        expected_consumer_id=f"c-{role[-8:]}",
                        expected_lane="gated",
                        schema=schema,
                        connect=_writer_connect_factory(owner_dsn, schema, role, password),
                    )
                    try:
                        with pytest.raises(
                            LaneStoreUnreachable, match=r"member"
                        ):
                            writer.assert_ready()
                    finally:
                        writer.close()
                finally:
                    _drop_role(scratch, parent)
                return
            if name == "ownership" and priv == "relation":
                apply_gate_lane_writer_grants(
                    scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
                )
                # Transfer ownership of lease_lanes to the runtime role.
                scratch.execute(
                    sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                        sql.Identifier(schema, "lease_lanes"),
                        sql.Identifier(role),
                    )
                )
                writer = PsycopgLaneWriter(
                    "unused",
                    expected_role=role,
                    expected_consumer_id=f"c-{role[-8:]}",
                    expected_lane="gated",
                    schema=schema,
                    connect=_writer_connect_factory(owner_dsn, schema, role, password),
                )
                try:
                    with pytest.raises(LaneStoreUnreachable, match=r"owns relation"):
                        writer.assert_ready()
                finally:
                    writer.close()
                    # Restore owner so schema drop works.
                    owner = scratch.execute("SELECT current_user").fetchone()[0]
                    scratch.execute(
                        sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                            sql.Identifier(schema, "lease_lanes"),
                            sql.Identifier(owner),
                        )
                    )
                return
            if name == "ownership" and priv == "function":
                apply_gate_lane_writer_grants(
                    scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
                )
                scratch.execute(
                    sql.SQL("ALTER FUNCTION {}(text) OWNER TO {}").format(
                        sql.Identifier(schema, "arm_lease_lane"),
                        sql.Identifier(role),
                    )
                )
                writer = PsycopgLaneWriter(
                    "unused",
                    expected_role=role,
                    expected_consumer_id=f"c-{role[-8:]}",
                    expected_lane="gated",
                    schema=schema,
                    connect=_writer_connect_factory(owner_dsn, schema, role, password),
                )
                try:
                    with pytest.raises(LaneStoreUnreachable, match=r"owns function"):
                        writer.assert_ready()
                finally:
                    writer.close()
                    owner = scratch.execute("SELECT current_user").fetchone()[0]
                    scratch.execute(
                        sql.SQL("ALTER FUNCTION {}(text) OWNER TO {}").format(
                            sql.Identifier(schema, "arm_lease_lane"),
                            sql.Identifier(owner),
                        )
                    )
                return
            if name == "PUBLIC":
                apply_gate_lane_writer_grants(
                    scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
                )
                # PUBLIC EXECUTE on arm is ambient — readiness should still pass
                # for the bound role (it has EXECUTE), but the coverage channel
                # is proven by applying PUBLIC then confirming REVOKE in apply.
                # Here we inject a forbidden TABLE privilege via PUBLIC path:
                scratch.execute(
                    sql.SQL("GRANT DELETE ON {} TO PUBLIC").format(
                        sql.Identifier(schema, "lease_lanes")
                    )
                )
                writer = PsycopgLaneWriter(
                    "unused",
                    expected_role=role,
                    expected_consumer_id=f"c-{role[-8:]}",
                    expected_lane="gated",
                    schema=schema,
                    connect=_writer_connect_factory(owner_dsn, schema, role, password),
                )
                try:
                    with pytest.raises(
                        LaneStoreUnreachable,
                        match=r"forbidden privilege DELETE on .*lease_lanes",
                    ):
                        writer.assert_ready()
                finally:
                    writer.close()
                    scratch.execute(
                        sql.SQL("REVOKE DELETE ON {} FROM PUBLIC").format(
                            sql.Identifier(schema, "lease_lanes")
                        )
                    )
                return
            if name == "function_owner":
                # Function owner must be NOLOGIN — prove LOGIN owner fails isolation
                # when we try to use the owner as the runtime role.
                apply_gate_lane_writer_grants(
                    scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
                )
                # Make the runtime role own a function (LOGIN owner of DEFINER).
                scratch.execute(
                    sql.SQL("ALTER FUNCTION {}() OWNER TO {}").format(
                        sql.Identifier(schema, "list_lease_lanes"),
                        sql.Identifier(role),
                    )
                )
                writer = PsycopgLaneWriter(
                    "unused",
                    expected_role=role,
                    expected_consumer_id=f"c-{role[-8:]}",
                    expected_lane="gated",
                    schema=schema,
                    connect=_writer_connect_factory(owner_dsn, schema, role, password),
                )
                try:
                    with pytest.raises(LaneStoreUnreachable, match=r"owns function"):
                        writer.assert_ready()
                finally:
                    writer.close()
                    owner = scratch.execute("SELECT current_user").fetchone()[0]
                    scratch.execute(
                        sql.SQL("ALTER FUNCTION {}() OWNER TO {}").format(
                            sql.Identifier(schema, "list_lease_lanes"),
                            sql.Identifier(owner),
                        )
                    )
                return
            pytest.fail(f"unhandled channel case {(kind, name, priv, form)}")

        # Relation / column form kills.
        apply_gate_lane_writer_grants(
            scratch, role, consumer_id=f"c-{role[-8:]}", lane="gated"
        )
        rel_ident = sql.Identifier(schema, name)
        role_ident = sql.Identifier(role)
        if form == "column":
            col = _REL_COLUMN[name]
            scratch.execute(
                sql.SQL("GRANT {} ({}) ON {} TO {}").format(
                    sql.SQL(priv),
                    sql.Identifier(col),
                    rel_ident,
                    role_ident,
                )
            )
        else:
            scratch.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(priv), rel_ident, role_ident
                )
            )
            rel_q = f'"{schema}".{name}'
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (role, rel_q, priv),
            ).fetchone()[0] is True

        writer = PsycopgLaneWriter(
            "unused",
            expected_role=role,
            expected_consumer_id=f"c-{role[-8:]}",
            expected_lane="gated",
            schema=schema,
            connect=_writer_connect_factory(owner_dsn, schema, role, password),
        )
        try:
            if form == "column":
                match = rf"forbidden column privilege {priv} on .*{name}"
            else:
                match = rf"forbidden privilege {priv} on .*{name}"
            with pytest.raises(LaneStoreUnreachable, match=match):
                writer.assert_ready()
        finally:
            writer.close()
    finally:
        _drop_role(scratch, role)


def test_retire_bound_consumer_predicate_mutation_is_detectable(scratch):
    """Independently drop the bound-consumer DELETE predicate; cross-seat must RED.

    Restores the function from schema.sql before returning.
    """
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    role_a = _role_name("mut_a")
    role_b = _role_name("mut_b")
    pw_a = f"pw_{uuid.uuid4().hex}"
    pw_b = f"pw_{uuid.uuid4().hex}"
    _require_login_role(scratch, role_a, pw_a)
    _require_login_role(scratch, role_b, pw_b)
    schema_path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src"
        / "arb_memory"
        / "schema.sql"
    )
    try:
        apply_gate_lane_writer_grants(
            scratch, role_a, consumer_id="mut-a", lane="gated"
        )
        apply_gate_lane_writer_grants(
            scratch, role_b, consumer_id="mut-b", lane="exempt"
        )
        writer_a = PsycopgLaneWriter(
            "unused",
            expected_role=role_a,
            expected_consumer_id="mut-a",
            expected_lane="gated",
            schema=schema,
            connect=_writer_connect_factory(owner_dsn, schema, role_a, pw_a),
        )
        writer_b = PsycopgLaneWriter(
            "unused",
            expected_role=role_b,
            expected_consumer_id="mut-b",
            expected_lane="exempt",
            schema=schema,
            connect=_writer_connect_factory(owner_dsn, schema, role_b, pw_b),
        )
        try:
            writer_b.arm("lease-mut-b")
            # Mutate: drop bound-consumer predicate on retire.
            # Mirrors schema.sql's shape (search_path pinned to the owning
            # schema) so the ONLY difference from the real function is the
            # dropped predicate. Keeping the old owner-scan here would make this
            # mutation fail for the wrong reason under a leftover schema.
            scratch.execute(
                f"""
                CREATE OR REPLACE FUNCTION retire_lease_lane(p_lease_id text)
                RETURNS boolean
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = "{schema}", pg_catalog
                AS $function$
                DECLARE
                    deleted int;
                BEGIN
                    -- Mutation: bound-consumer predicate removed.
                    DELETE FROM lease_lanes WHERE lease_id = p_lease_id;
                    GET DIAGNOSTICS deleted = ROW_COUNT;
                    RETURN deleted > 0;
                END;
                $function$;
                """
            )
            # With the predicate gone, A can retire B — the cross-seat test RED.
            assert writer_a.retire("lease-mut-b") is True, (
                "mutation did not open the cross-seat retire path"
            )
            # Cross-seat invariant is now violated (row gone).
            assert scratch.execute(
                "SELECT count(*) FROM lease_lanes WHERE lease_id = 'lease-mut-b'"
            ).fetchone()[0] == 0
        finally:
            writer_a.close()
            writer_b.close()
            # Restore from checked-in schema.sql function body.
            text = schema_path.read_text(encoding="utf-8")
            # Re-apply just the three functions by re-executing the function
            # sections from schema.sql (full file is idempotent CREATE OR REPLACE).
            scratch.execute(text)
            # Re-apply grants after restore.
            apply_gate_lane_writer_grants(
                scratch, role_a, consumer_id="mut-a", lane="gated"
            )
            apply_gate_lane_writer_grants(
                scratch, role_b, consumer_id="mut-b", lane="exempt"
            )
            # Prove restore: B arms, A cannot retire.
            writer_a2 = PsycopgLaneWriter(
                "unused",
                expected_role=role_a,
                expected_consumer_id="mut-a",
                expected_lane="gated",
                schema=schema,
                connect=_writer_connect_factory(owner_dsn, schema, role_a, pw_a),
            )
            writer_b2 = PsycopgLaneWriter(
                "unused",
                expected_role=role_b,
                expected_consumer_id="mut-b",
                expected_lane="exempt",
                schema=schema,
                connect=_writer_connect_factory(owner_dsn, schema, role_b, pw_b),
            )
            try:
                writer_b2.arm("lease-mut-b2")
                assert writer_a2.retire("lease-mut-b2") is False
            finally:
                writer_a2.close()
                writer_b2.close()
    finally:
        _drop_role(scratch, role_a)
        _drop_role(scratch, role_b)


def test_pg_table_privileges_matches_server_aclexplode(scratch):
    """PG_TABLE_PRIVILEGES is anchored to the connected server."""
    role = scratch.execute("SELECT current_user").fetchone()[0]
    scratch.execute("CREATE TEMP TABLE _lw_pg_priv_anchor (id int)")
    scratch.execute(
        sql.SQL("GRANT ALL ON _lw_pg_priv_anchor TO {}").format(
            sql.Identifier(role)
        )
    )
    rows = scratch.execute(
        """
        SELECT privilege_type
        FROM aclexplode(
          (SELECT relacl FROM pg_class WHERE oid = '_lw_pg_priv_anchor'::regclass)
        )
        WHERE grantee = (SELECT oid FROM pg_roles WHERE rolname = current_user)
        """
    ).fetchall()
    server_privs = {row[0] for row in rows}
    assert server_privs == set(PG_TABLE_PRIVILEGES), (
        f"server aclexplode privileges {sorted(server_privs)} != "
        f"PG_TABLE_PRIVILEGES {sorted(PG_TABLE_PRIVILEGES)}"
    )
