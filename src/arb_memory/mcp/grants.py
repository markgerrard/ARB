from __future__ import annotations

from psycopg import sql


def apply_local_reader_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)

    # PUBLIC revoke names all three hint_read tables (ERRATA: guide's two-table PUBLIC
    # line is insufficient under the narrower apply sequence tests seed).
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "hint_read"),
            sql.Identifier(schema, "hint_read_hit"),
            sql.Identifier(schema, "hint_read_deadletter"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident)
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}, {} TO {}").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA mcp_auth FROM {}").format(role_ident)
    )
    conn.execute(
        sql.SQL("REVOKE USAGE ON SCHEMA mcp_auth FROM {}").format(role_ident)
    )
    for table in (
        "audit_events",
        "audit_deadletter",
        "audit_close_deadletter",
        "eval_event_raw",
        "eval_deadletter",
        "transcript_io",
        "transcript_deadletter",
        "write_deadletter",
        "idempotency_keys",
        "eval_turn",
        "eval_tool_call",
        "eval_task",
        "span_deadletter",
        "hint_read",
        "hint_read_hit",
        "hint_read_deadletter",
    ):
        conn.execute(
            sql.SQL("REVOKE ALL ON {} FROM {}").format(
                sql.Identifier(schema, table),
                role_ident,
            )
        )


def apply_mcp_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)

    # PUBLIC revoke names all three hint_read tables (ERRATA: guide's two-table PUBLIC
    # line is insufficient under the narrower apply sequence tests seed).
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "hint_read"),
            sql.Identifier(schema, "hint_read_hit"),
            sql.Identifier(schema, "hint_read_deadletter"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA mcp_auth TO {}").format(role_ident)
    )
    conn.execute(
        sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mcp_auth TO {}"
        ).format(role_ident)
    )
    conn.execute(
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA mcp_auth "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(role_ident)
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident)
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}, {} TO {}").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM {}").format(
            sql.Identifier(schema, "audit_events"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM {}").format(
            sql.Identifier(schema, "transcript_io"),
            sql.Identifier(schema, "transcript_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_turn"),
            sql.Identifier(schema, "eval_tool_call"),
            sql.Identifier(schema, "eval_task"),
            sql.Identifier(schema, "span_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {} FROM {}").format(
            sql.Identifier(schema, "hint_read"),
            sql.Identifier(schema, "hint_read_hit"),
            sql.Identifier(schema, "hint_read_deadletter"),
            role_ident,
        )
    )


def apply_hint_read_local_writer_grants(conn, role: str) -> None:
    """INSERT-only on hint_read/hint_read_hit, for the local-MCP reader role — and ONLY that
    role. Deliberately NOT folded into apply_local_reader_grants, which run.py also applies to
    vault_export_role; doing so there would create a third hint_read writer (G-03)."""
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    tables = sql.SQL(", ").join(
        [sql.Identifier(schema, "hint_read"), sql.Identifier(schema, "hint_read_hit")]
    )
    conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(tables))
    conn.execute(sql.SQL("GRANT INSERT ON {} TO {}").format(tables, role_ident))
    conn.execute(sql.SQL("REVOKE SELECT, UPDATE, DELETE ON {} FROM {}").format(tables, role_ident))


def apply_hint_read_consumer_grants(conn, role: str) -> None:
    """SELECT+INSERT for HintReadConsumer's role, mirroring apply_eval_grants's shape for
    eval_event_raw/eval_deadletter -- INCLUDING the sequence grant v4 omitted (H-01):
    bigserial's nextval() needs SEQUENCE USAGE, which PUBLIC does not carry by default, and
    hint_read_deadletter.id is bigserial. Applied to the SAME role as apply_eval_grants --
    HintReadConsumer and EvalConsumer share the bus-consumer identity."""
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    tables = sql.SQL(", ").join([
        sql.Identifier(schema, "hint_read"),
        sql.Identifier(schema, "hint_read_hit"),
        sql.Identifier(schema, "hint_read_deadletter"),
    ])
    seq = sql.Identifier(schema, "hint_read_deadletter_id_seq")
    conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(tables))
    conn.execute(sql.SQL("REVOKE ALL ON SEQUENCE {} FROM PUBLIC").format(seq))
    conn.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(tables, role_ident))
    conn.execute(sql.SQL("REVOKE UPDATE, DELETE ON {} FROM {}").format(tables, role_ident))
    conn.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(seq, role_ident))


def apply_transcript_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "transcript_io"),
            sql.Identifier(schema, "transcript_deadletter"),
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON SEQUENCE {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "transcript_io_id_seq"),
            sql.Identifier(schema, "transcript_deadletter_id_seq"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident)
    )
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT ON {}, {} TO {}").format(
            sql.Identifier(schema, "transcript_io"),
            sql.Identifier(schema, "transcript_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.Identifier(schema, "transcript_io"),
            sql.Identifier(schema, "transcript_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SEQUENCE {}, {} TO {}").format(
            sql.Identifier(schema, "transcript_io_id_seq"),
            sql.Identifier(schema, "transcript_deadletter_id_seq"),
            role_ident,
        )
    )


def apply_visibility_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    visibility_tables = [
        sql.Identifier(schema, "eval_event_raw"),
        sql.Identifier(schema, "transcript_io"),
    ]
    if conn.execute("SELECT to_regclass(%s)", (f"{schema}.audit_events",)).fetchone()[0] is not None:
        visibility_tables.append(sql.Identifier(schema, "audit_events"))
    mcp_auth_siblings = [
        sql.Identifier("mcp_auth", table)
        for table in ("oauth_clients", "refresh_tokens")
        if conn.execute("SELECT to_regclass(%s)", (f"mcp_auth.{table}",)).fetchone()[0] is not None
    ]
    login_tables = [
        sql.Identifier("mcp_auth", table)
        for table in ("login_sessions", "login_attempts")
        if conn.execute("SELECT to_regclass(%s)", (f"mcp_auth.{table}",)).fetchone()[0] is not None
    ]

    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident)
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA mcp_auth TO {}").format(role_ident)
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
        )
    )
    if mcp_auth_siblings:
        conn.execute(
            sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(
                sql.SQL(", ").join(mcp_auth_siblings),
            )
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON {} FROM {}").format(
                sql.SQL(", ").join(mcp_auth_siblings),
                role_ident,
            )
        )
    if login_tables:
        conn.execute(
            sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(
                sql.SQL(", ").join(login_tables),
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                sql.SQL(", ").join(login_tables),
                role_ident,
            )
        )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}, {} TO {}").format(
            sql.SQL(", ").join(visibility_tables),
            sql.Identifier("mcp_auth", "access_tokens"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.SQL(", ").join(visibility_tables),
            sql.Identifier("mcp_auth", "access_tokens"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM {}").format(
            sql.Identifier(schema, "hints"),
            sql.Identifier(schema, "artefacts"),
            role_ident,
        )
    )


def apply_eval_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON SEQUENCE {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "eval_event_raw_id_seq"),
            sql.Identifier(schema, "eval_deadletter_id_seq"),
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {}, {} FROM PUBLIC").format(
            sql.Identifier(schema, "eval_turn"),
            sql.Identifier(schema, "eval_tool_call"),
            sql.Identifier(schema, "eval_task"),
            sql.Identifier(schema, "span_deadletter"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident)
    )
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT ON {}, {} TO {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("REVOKE UPDATE, DELETE ON {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_event_raw"),
            sql.Identifier(schema, "eval_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {}, {}, {} TO {}").format(
            sql.Identifier(schema, "eval_turn"),
            sql.Identifier(schema, "eval_tool_call"),
            sql.Identifier(schema, "eval_task"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT INSERT ON {} TO {}").format(
            sql.Identifier(schema, "span_deadletter"), role_ident
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
            sql.Identifier("stream_entry_id"),
            sql.Identifier(schema, "span_deadletter"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SEQUENCE {}, {} TO {}").format(
            sql.Identifier(schema, "eval_event_raw_id_seq"),
            sql.Identifier(schema, "eval_deadletter_id_seq"),
            role_ident,
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SEQUENCE {}, {}, {}, {} TO {}").format(
            sql.Identifier(schema, "eval_turn_id_seq"),
            sql.Identifier(schema, "eval_tool_call_id_seq"),
            sql.Identifier(schema, "eval_task_id_seq"),
            sql.Identifier(schema, "span_deadletter_id_seq"),
            role_ident,
        )
    )


def apply_retention_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident))
    for table in ("eval_event_raw", "transcript_io", "hint_read"):
        table_ident = sql.Identifier(schema, table)
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(table_ident, role_ident))
        conn.execute(sql.SQL("GRANT DELETE ON {} TO {}").format(table_ident, role_ident))
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(table_ident, role_ident))
    conn.execute(
        sql.SQL("REVOKE ALL ON {}, {}, {}, {} FROM {}").format(
            sql.Identifier(schema, "eval_turn"),
            sql.Identifier(schema, "eval_tool_call"),
            sql.Identifier(schema, "eval_task"),
            sql.Identifier(schema, "span_deadletter"),
            role_ident,
        )
    )


GATE_READER_ROLE = "arb_gate_reader"

# Everything the gate reader must not be able to mint, and must not own.
GATE_RELATIONS = (
    "claims",
    "attestations",
    "seat_posture",
    "lease_lanes",
    "claim_admissibility_v",
    "seat_posture_v",
    "lease_lane_v",
)

# Per-seat lane-writer function inventory (bodies in schema.sql only).
GATE_LANE_WRITER_FUNCTIONS = (
    "arm_lease_lane",
    "retire_lease_lane",
    "list_lease_lanes",
)

# Relations a lane writer must neither privilege nor own (includes binding table).
GATE_LANE_WRITER_RELATIONS = GATE_RELATIONS + ("lane_writer_bindings",)

# Shared/fleet-wide writer role name is deliberately refused — 1d-i is per-seat.
FORBIDDEN_SHARED_LANE_WRITER_ROLE = "arb_gate_lane_writer"


class GateRoleNotIsolated(RuntimeError):
    """`role` cannot hold the SELECT-only trust story on this cluster."""


def assert_gate_role_isolation(conn, role: str) -> None:
    """Refuse unless `role` can ACTUALLY hold "reads confirmation state, cannot mint it".

    Relation ACLs are only half the property. Two paths bypass every GRANT/REVOKE this
    module issues, and both were reproduced on PostgreSQL 17.10 during panel run
    panel-gate-slice1b-r2-20260726T153801Z-ded470:

      MEMBERSHIP -- a privilege-bearing parent role. `SET ROLE <parent>` then INSERT minted
                    a seat_posture row ('via-set-role').
      OWNERSHIP  -- a role owning a gate relation re-grants itself after this helper's
                    revoke and writes ('owner-regrant').

    **NOINHERIT IS NOT A SUFFICIENT REMEDY and must not be used as the condition.** It
    blocks *automatic* inheritance only. Measured: with the gate role created NOINHERIT the
    automatic write was REFUSED 42501, but `SET ROLE <parent>` followed by INSERT still
    succeeded. An earlier revision of this docstring prescribed NOINHERIT and was wrong; a
    deployer following it would have believed the property held while the path stayed open.
    Deny-proof for that specific claim:
    tests/arb_memory/test_gate_grants.py::test_noinherit_alone_does_not_establish_isolation.

    Raises GateRoleNotIsolated rather than returning a bool: the caller is a deployment step
    establishing a security property, and a silently-ignored return value is how such a check
    becomes decorative.
    """
    memberships = [
        row[0]
        for row in conn.execute(
            "SELECT g.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.roleid "
            "WHERE r.rolname = %s",
            (role,),
        ).fetchall()
    ]
    if memberships:
        raise GateRoleNotIsolated(
            f"{role} is a member of {memberships}; membership authorises SET ROLE regardless "
            f"of NOINHERIT, so the SELECT-only trust story is not establishable. Provision a "
            f"dedicated role with no memberships."
        )

    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    owned = [
        row[0]
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE r.rolname = %s AND n.nspname = %s AND c.relname = ANY(%s)",
            (role, schema, list(GATE_RELATIONS)),
        ).fetchall()
    ]
    if owned:
        raise GateRoleNotIsolated(
            f"{role} owns gate relation(s) {owned}; an owner can GRANT itself write access "
            f"back after any revoke. The gate role must own none of them."
        )


def apply_gate_reader_grants(conn, role: str) -> None:
    """SELECT on the three gate views and nothing else.

    The bridge holds this role. Within what this function controls -- direct, PUBLIC and
    column-level ACLs on the gate relations -- it can read confirmation state, posture and
    lane and cannot mint any of them. That is NOT an unconditional property of the role: see
    assert_gate_role_isolation, which this function calls first and which covers the two
    paths ACLs cannot reach (membership and ownership). **"the gate reader structurally
    cannot mint" MUST NOT enter an arc trail on the ACL work alone** -- it holds only for a
    role that also passes the isolation assertion on the deployed cluster.

    Role creation is deliberately NOT here: roles are cluster-global and schema.sql is
    applied per-schema, so creation belongs to the deployment step that owns the cluster.

    Revoking from the named role is NOT sufficient, and that is why the PUBLIC revokes below
    exist. PostgreSQL effective privilege is the UNION of direct, PUBLIC and inherited-role
    ACLs, so an ambient PUBLIC grant survives a role-scoped revoke. It matters most on
    `seat_posture_v` / `lease_lane_v`: both are simple views and therefore AUTOMATICALLY
    UPDATABLE, so an INSERT on the view reaches the base table with no direct table privilege
    at all. Panel run panel-gate-slice1b-20260726T143935Z-bfbc65 demonstrated a gate reader
    minting `('seat-bypassed', False)` into `seat_posture` through exactly that path.
    Deny-proof: tests/arb_memory/test_gate_grants.py::
    test_gate_reader_cannot_mint_through_an_ambient_public_grant.

    Named residual that remains even so: this helper is one-shot. A `GRANT ... TO PUBLIC`
    issued AFTER it runs survives untouched; it defends the state at apply time, not for all
    time.
    """
    assert_gate_role_isolation(conn, role)
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    views = [
        sql.Identifier(schema, "claim_admissibility_v"),
        sql.Identifier(schema, "seat_posture_v"),
        sql.Identifier(schema, "lease_lane_v"),
    ]
    tables = [
        sql.Identifier(schema, name)
        for name in ("claims", "attestations", "seat_posture", "lease_lanes")
    ]

    # Ambient ACLs first: only the consumer writes these, and nobody reads them via PUBLIC.
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(sql.SQL(", ").join(tables))
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(sql.SQL(", ").join(views))
    )
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident))
    # REVOKE ALL then GRANT SELECT, rather than enumerating the write verbs to revoke.
    # Enumeration is the fragility the r2 panel demonstrated on the PUBLIC revokes: narrowing
    # `ALL` to `INSERT` left the suite green while UPDATE through an auto-updatable view
    # stayed open. Same reasoning applies to a pre-existing direct grant to the role, so the
    # views are now revoked exactly like the tables.
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(", ").join(views), role_ident)
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(", ").join(views), role_ident)
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(", ").join(tables), role_ident)
    )


def assert_gate_lane_writer_isolation(conn, role: str) -> None:
    """Refuse unless `role` can hold the bound-function trust story.

    Same membership/ownership rails as assert_gate_role_isolation, extended to
    the three lane-writer functions and the binding table. NOINHERIT is not
    accepted as isolation (see assert_gate_role_isolation docstring).
    """
    memberships = [
        row[0]
        for row in conn.execute(
            "SELECT g.rolname FROM pg_auth_members m "
            "JOIN pg_roles r ON r.oid = m.member "
            "JOIN pg_roles g ON g.oid = m.roleid "
            "WHERE r.rolname = %s",
            (role,),
        ).fetchall()
    ]
    if memberships:
        raise GateRoleNotIsolated(
            f"{role} is a member of {memberships}; membership authorises SET ROLE "
            f"regardless of NOINHERIT, so the bound-function trust story is not "
            f"establishable. Provision a dedicated role with no memberships."
        )

    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    owned = [
        row[0]
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE r.rolname = %s AND n.nspname = %s AND c.relname = ANY(%s)",
            (role, schema, list(GATE_LANE_WRITER_RELATIONS)),
        ).fetchall()
    ]
    if owned:
        raise GateRoleNotIsolated(
            f"{role} owns gate relation(s) {owned}; an owner can GRANT itself "
            f"write access back after any revoke. The lane writer must own none."
        )

    owned_fns = [
        row[0]
        for row in conn.execute(
            "SELECT p.proname FROM pg_proc p "
            "JOIN pg_roles r ON r.oid = p.proowner "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE r.rolname = %s AND n.nspname = %s AND p.proname = ANY(%s)",
            (role, schema, list(GATE_LANE_WRITER_FUNCTIONS)),
        ).fetchall()
    ]
    if owned_fns:
        raise GateRoleNotIsolated(
            f"{role} owns lane-writer function(s) {owned_fns}; the runtime role "
            f"must not own SECURITY DEFINER functions."
        )


def apply_gate_lane_writer_grants(
    conn,
    role: str,
    *,
    consumer_id: str,
    lane: str,
) -> None:
    """Grant schema USAGE + EXECUTE on the three bound functions only.

    Function bodies are NOT created here — they come from checked-in
    ``schema.sql``. This helper revokes ambient relation/function access,
    grants the closed privilege set, and UPSERTs the owner-only binding row.
    Role creation and secrets are deliberately NOT here.
    """
    if role == FORBIDDEN_SHARED_LANE_WRITER_ROLE:
        raise GateRoleNotIsolated(
            f"{role} is the forbidden shared lane-writer role; provision a "
            f"per-seat login instead"
        )
    if not consumer_id or not consumer_id.strip():
        raise ValueError("consumer_id must be nonblank")
    if lane not in ("gated", "exempt"):
        raise ValueError(f"lane must be gated|exempt, got {lane!r}")

    assert_gate_lane_writer_isolation(conn, role)
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)

    relations = [
        sql.Identifier(schema, name) for name in GATE_LANE_WRITER_RELATIONS
    ]
    functions = [
        sql.Identifier(schema, name) for name in GATE_LANE_WRITER_FUNCTIONS
    ]

    # Ambient ACLs first: revoke from PUBLIC and the named role.
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(
            sql.SQL(", ").join(relations)
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM {}").format(
            sql.SQL(", ").join(relations), role_ident
        )
    )
    for fn in functions:
        conn.execute(
            sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(fn)
        )
        # arm/retire take text; list takes none — REVOKE ALL ON FUNCTION name
        # requires the full signature. Use regprocedure via current_schema.
    # Explicit signatures so REVOKE/GRANT hit the right overloads.
    sig_map = {
        "arm_lease_lane": sql.SQL("{}(text)").format(
            sql.Identifier(schema, "arm_lease_lane")
        ),
        "retire_lease_lane": sql.SQL("{}(text)").format(
            sql.Identifier(schema, "retire_lease_lane")
        ),
        "list_lease_lanes": sql.SQL("{}()").format(
            sql.Identifier(schema, "list_lease_lanes")
        ),
    }
    for sig in sig_map.values():
        conn.execute(
            sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sig)
        )
        conn.execute(
            sql.SQL("REVOKE ALL ON FUNCTION {} FROM {}").format(sig, role_ident)
        )

    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            schema_ident, role_ident
        )
    )
    for sig in sig_map.values():
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                sig, role_ident
            )
        )

    # Owner-only binding: UPSERT for this role. No GRANT on the binding table.
    conn.execute(
        sql.SQL(
            "INSERT INTO {} (db_role, consumer_id, lane) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (db_role) DO UPDATE "
            "SET consumer_id = EXCLUDED.consumer_id, lane = EXCLUDED.lane"
        ).format(sql.Identifier(schema, "lane_writer_bindings")),
        (role, consumer_id, lane),
    )
