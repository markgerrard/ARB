import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
from psycopg import sql  # noqa: E402
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

from arb_memory.mcp.grants import (  # noqa: E402
    GATE_READER_ROLE,
    GateRoleNotIsolated,
    apply_gate_reader_grants,
    assert_gate_role_isolation,
)


def _role_name(suffix):
    """Unique per test run. A fixed name is a concurrency hazard: these tests SET ROLE and
    DROP OWNED BY ... CASCADE, so a name surviving a crashed or concurrent run would either
    raise InsufficientPrivilege at SET ROLE (outside any pytest.raises, looking like a
    privilege defect in the code under review) or strip a concurrent run's grants."""
    return f"{GATE_READER_ROLE}_{suffix}_{uuid.uuid4().hex[:8]}"


def _ensure_role(conn, role, *, attrs="LOGIN"):
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists:
        return True
    try:
        conn.execute(sql.SQL("CREATE ROLE {} " + attrs).format(sql.Identifier(role)))
        return True
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return False


def _drop_role(conn, role):
    conn.execute("RESET ROLE")
    conn.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role)))
    conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_gate_reader_can_select_the_three_views_and_nothing_else(scratch):
    role = _role_name("views")
    if not _ensure_role(scratch, role):
        pytest.skip("cannot create roles on this credential")
    try:
        apply_gate_reader_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f'"{schema}".{view}'),
            ).fetchone()[0] is True, f"{view} not readable by the gate reader"

        for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert scratch.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f'"{schema}".{table}', priv),
                ).fetchone()[0] is False, (
                    f"gate reader holds {priv} on base table {table} - only the consumer writes"
                )

        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            for priv in ("INSERT", "UPDATE", "DELETE"):
                assert scratch.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f'"{schema}".{view}', priv),
                ).fetchone()[0] is False, f"gate reader holds {priv} on {view}"
    finally:
        _drop_role(scratch, role)


def test_gate_reader_cannot_mint_through_an_ambient_public_grant(scratch):
    """P1-A deny-proof: `has_table_privilege` on the named role is not the trust story.

    Effective privilege in PostgreSQL is the UNION of direct, PUBLIC and inherited-role ACLs, so
    revoking from the role alone leaves an ambient grant standing. Both small views are simple and
    therefore *automatically updatable*, which means an INSERT on the view reaches the base table
    with no direct table privilege at all. This asserts the write is actually refused when acting
    AS the role -- not merely that the helper did not itself grant it.

    Panel run panel-gate-slice1b-20260726T143935Z-bfbc65 (codex-sol F1, reproduced by the
    orchestrator): before the PUBLIC revoke this inserted `('seat-bypassed', False)` into the base
    `seat_posture` table.
    """
    role = _role_name("ambient")
    if not _ensure_role(scratch, role):
        pytest.skip("cannot create roles on this credential")
    try:
        # Pre-existing rows whose state the reader must not be able to change. UPDATE is the
        # verb that matters most: INSERT only adds a posture for a seat nobody configured,
        # but UPDATE silently un-gates a seat someone DELIBERATELY gated (spec §3), and
        # UPDATE on lease_lane_v is the §5.3 total bypass.
        scratch.execute("INSERT INTO seat_posture (seat_id, requires_claim_ref) "
                        "VALUES ('victim', true)")
        scratch.execute("INSERT INTO lease_lanes (lease_id, lane, armed_by) "
                        "VALUES ('l-victim', 'gated', 'consumer')")
        # Ambient grants of exactly the shape the helper must neutralise. ALL, not INSERT:
        # narrowing the helper's revoke to INSERT-only left the suite green while UPDATE
        # through an auto-updatable view stayed open (r2 panel P1).
        scratch.execute("GRANT ALL ON seat_posture_v TO PUBLIC")
        scratch.execute("GRANT ALL ON lease_lane_v TO PUBLIC")
        scratch.execute("GRANT ALL ON claims TO PUBLIC")
        apply_gate_reader_grants(scratch, role)

        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        try:
            for stmt in (
                "INSERT INTO seat_posture_v (seat_id, requires_claim_ref) "
                "VALUES ('seat-bypassed', false)",
                "UPDATE seat_posture_v SET requires_claim_ref = false WHERE seat_id = 'victim'",
                "DELETE FROM seat_posture_v WHERE seat_id = 'victim'",
                "INSERT INTO lease_lane_v (lease_id, lane) VALUES ('l-bypassed', 'exempt')",
                "UPDATE lease_lane_v SET lane = 'exempt' WHERE lease_id = 'l-victim'",
                "DELETE FROM lease_lane_v WHERE lease_id = 'l-victim'",
                "SELECT 1 FROM claims LIMIT 1",
                "INSERT INTO claims (claim_id, finding_ref, author_seat, author_family, "
                "author_family_provenance) VALUES ('c-x', 'f', 's', 'gpt', 'wire')",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    scratch.execute(stmt)
            # ...while the three views it is supposed to read stay readable.
            for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
                scratch.execute(f"SELECT 1 FROM {view} LIMIT 1")
        finally:
            scratch.execute("RESET ROLE")

        # The raise alone cannot distinguish "refused" from "refused for an unrelated
        # reason". The base-table post-condition is the load-bearing half.
        assert scratch.execute(
            "SELECT seat_id, requires_claim_ref FROM seat_posture ORDER BY seat_id"
        ).fetchall() == [("victim", True)], "gate reader changed posture state"
        assert scratch.execute(
            "SELECT lease_id, lane FROM lease_lanes ORDER BY lease_id"
        ).fetchall() == [("l-victim", "gated")], "gate reader changed lane state"
    finally:
        _drop_role(scratch, role)


def test_noinherit_alone_does_not_establish_isolation(scratch):
    """Deny-proof for a REJECTED REMEDY, not for the code.

    An earlier revision of `apply_gate_reader_grants`'s docstring prescribed provisioning the
    role NOINHERIT. That remedy is WRONG, and this test is what makes the wrongness fail
    loudly instead of sitting in prose: NOINHERIT blocks only AUTOMATIC inheritance, while a
    session authenticated as the role can still `SET ROLE <parent>` and write with the
    parent's privileges. If PostgreSQL ever made NOINHERIT sufficient, this test fails and
    the remedy can be reinstated deliberately rather than by assumption.
    """
    gate = _role_name("ni_gate")
    parent = _role_name("ni_parent")
    if not _ensure_role(scratch, gate, attrs="LOGIN NOINHERIT"):
        pytest.skip("cannot create roles on this credential")
    _ensure_role(scratch, parent, attrs="NOLOGIN")
    try:
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        scratch.execute(sql.SQL("GRANT INSERT ON {} TO {}").format(
            sql.Identifier(schema, "seat_posture"), sql.Identifier(parent)))
        # Both need schema USAGE: without it the relation is invisible and PostgreSQL
        # reports UndefinedTable rather than InsufficientPrivilege, which would make the
        # automatic-path assertion below pass for the wrong reason.
        for r in (parent, gate):
            scratch.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(r)))
        scratch.execute(sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(parent), sql.Identifier(gate)))

        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(gate)))
        try:
            # NOINHERIT does block the automatic path...
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                scratch.execute("INSERT INTO seat_posture (seat_id) VALUES ('auto')")
            # ...and does NOT block this one. That is the whole finding.
            scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(parent)))
            scratch.execute("INSERT INTO seat_posture (seat_id) VALUES ('via-set-role')")
        finally:
            scratch.execute("RESET ROLE")

        assert scratch.execute(
            "SELECT count(*) FROM seat_posture WHERE seat_id = 'via-set-role'"
        ).fetchone()[0] == 1, (
            "SET ROLE through a membership no longer mints - NOINHERIT may now be sufficient; "
            "re-derive the remedy in assert_gate_role_isolation before relying on this"
        )
    finally:
        _drop_role(scratch, gate)
        _drop_role(scratch, parent)


def test_isolation_assertion_refuses_a_role_with_a_membership(scratch):
    """The real remedy: membership is refused outright, regardless of INHERIT/NOINHERIT."""
    gate = _role_name("iso_member")
    parent = _role_name("iso_parent")
    if not _ensure_role(scratch, gate):
        pytest.skip("cannot create roles on this credential")
    _ensure_role(scratch, parent, attrs="NOLOGIN")
    try:
        assert_gate_role_isolation(scratch, gate)  # clean role: no raise
        scratch.execute(sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(parent), sql.Identifier(gate)))
        with pytest.raises(GateRoleNotIsolated) as exc:
            assert_gate_role_isolation(scratch, gate)
        assert parent in str(exc.value)
        # ...and the grant helper must refuse to certify such a role at all.
        with pytest.raises(GateRoleNotIsolated):
            apply_gate_reader_grants(scratch, gate)
    finally:
        _drop_role(scratch, gate)
        _drop_role(scratch, parent)


def test_isolation_assertion_refuses_a_role_owning_a_gate_relation(scratch):
    """Ownership is the second bypass: an owner re-grants itself after any revoke."""
    gate = _role_name("iso_owner")
    if not _ensure_role(scratch, gate):
        pytest.skip("cannot create roles on this credential")
    try:
        assert_gate_role_isolation(scratch, gate)
        scratch.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(
            sql.Identifier("seat_posture"), sql.Identifier(gate)))
        with pytest.raises(GateRoleNotIsolated) as exc:
            assert_gate_role_isolation(scratch, gate)
        assert "seat_posture" in str(exc.value)
        with pytest.raises(GateRoleNotIsolated):
            apply_gate_reader_grants(scratch, gate)
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("ALTER TABLE {} OWNER TO CURRENT_USER").format(
            sql.Identifier("seat_posture")))
        _drop_role(scratch, gate)


def test_gate_reader_cannot_read_artefacts_or_hints(scratch):
    """Scope check: the gate credential is not a general store reader."""
    role = _role_name("scope")
    if not _ensure_role(scratch, role):
        pytest.skip("cannot create roles on this credential")
    try:
        apply_gate_reader_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        for table in ("artefacts", "hints"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f'"{schema}".{table}'),
            ).fetchone()[0] is False, f"gate reader can read {table}"
    finally:
        _drop_role(scratch, role)
