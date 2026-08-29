"""Live-Postgres integration for PsycopgClaimResolver.

Requires ARB_MEMORY_DSN. A skip is not a pass — export the owner DSN first.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN"
)

from agent_redis_bridge import claim_gate  # noqa: E402
from agent_redis_bridge.claim_resolver import (  # noqa: E402
    PG_TABLE_PRIVILEGES,
    PRIVILEGE_COVERAGE,
    PsycopgClaimResolver,
)
from agent_redis_bridge.envelope import Envelope  # noqa: E402
from arb_memory.mcp.grants import (  # noqa: E402
    GATE_READER_ROLE,
    apply_gate_reader_grants,
)

# One real relation + column per kind for isolating GRANT forms. Column form
# privileges (INSERT/UPDATE/REFERENCES) need a real column; relation-only
# privileges (DELETE/TRUNCATE/TRIGGER/MAINTAIN) ignore the column name.
_KIND_GRANT_TARGET: dict[str, tuple[str, str]] = {
    "view": ("seat_posture_v", "requires_claim_ref"),
    "base_table": ("seat_posture", "requires_claim_ref"),
}


def _probed_live_kill_cases() -> list[tuple[str, str, str]]:
    """(kind, priv, form) derived from PRIVILEGE_COVERAGE.

    form is one of:
      - ``relation`` — GRANT priv ON relation (no column list)
      - ``column`` — GRANT priv (col) ON relation (column-form only)
      - ``select-required`` — REVOKE SELECT (positive arm; must refuse when missing)

    Relation+column tags yield both relation and column cases so deleting either
    half of the tag makes at least one case RED. Relation-only PG privileges
    (DELETE/TRUNCATE/TRIGGER/MAINTAIN) get the relation case only — never a
    silent skip of a missing column form.
    """
    cases: list[tuple[str, str, str]] = []
    for (kind, priv), tag in sorted(PRIVILEGE_COVERAGE.items()):
        if not tag.startswith("probed-by-"):
            continue
        if tag.endswith("select-required"):
            cases.append((kind, priv, "select-required"))
            continue
        # Closed tags: channel presence is structural, not free-form prose.
        if "relation" in tag:
            cases.append((kind, priv, "relation"))
        if "column" in tag:
            cases.append((kind, priv, "column"))
    return cases


def _expected_refuse_match(kind: str, priv: str, form: str, rel_name: str) -> str:
    """Exact message fragment assert_ready must raise for this kill form."""
    if form == "select-required":
        return rf"missing {priv} privilege on view {rel_name}"
    if kind == "view" and form == "relation":
        return rf"non-SELECT privilege {priv} on view {rel_name}"
    if kind == "view" and form == "column":
        return rf"non-SELECT column privilege {priv} on view {rel_name}"
    if kind == "base_table" and form == "relation":
        return rf"write privilege {priv} on base table {rel_name}"
    if kind == "base_table" and form == "column":
        return rf"write column privilege {priv} on base table {rel_name}"
    raise AssertionError(f"unhandled kill form: {(kind, priv, form)}")


def _role_name(suffix: str) -> str:
    return f"{GATE_READER_ROLE}_{suffix}_{uuid.uuid4().hex[:8]}"


# Loud skip reason so a cluster without CREATEROLE cannot silently green the
# live mutation-kill fleet (P2). pytest -rs will print this on every skip.
_CREATEROLE_SECURITY_SKIP = (
    "SECURITY_SKIP: CREATEROLE required — live mutation-kill of gate readiness "
    "is INERT on this credential (cannot create roles)"
)


def _ensure_role(conn, role: str, *, attrs: str = "LOGIN") -> bool:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists:
        return True
    try:
        conn.execute(sql.SQL("CREATE ROLE {} " + attrs).format(sql.Identifier(role)))
        return True
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return False


def _require_role(conn, role: str, *, attrs: str = "LOGIN") -> None:
    if not _ensure_role(conn, role, attrs=attrs):
        pytest.skip(_CREATEROLE_SECURITY_SKIP)


def _drop_role(conn, role: str) -> None:
    conn.execute("RESET ROLE")
    conn.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role)))
    conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _schema(conn) -> str:
    return conn.execute("SELECT current_schema()").fetchone()[0]


def _reader_connect_factory(owner_dsn: str, schema: str, role: str):
    """Open a separate autocommit connection on the scratch schema, then SET ROLE."""

    def connect(dsn, **kwargs):
        # Ignore the resolver's dsn string; use the owner DSN + SET ROLE so we never need
        # a real password for the temporary role. The resolver still believes it is
        # connecting as the reader because assert_ready checks current_user after SET ROLE.
        conn = psycopg.connect(
            owner_dsn,
            autocommit=True,
            row_factory=kwargs.get("row_factory", dict_row),
        )
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return conn

    return connect


def _owner_connect_factory(owner_dsn: str, schema: str):
    def connect(dsn, **kwargs):
        conn = psycopg.connect(
            owner_dsn,
            autocommit=True,
            row_factory=kwargs.get("row_factory", dict_row),
        )
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        return conn

    return connect


def _seed_gate_rows(scratch) -> None:
    scratch.execute(
        "INSERT INTO seat_posture (seat_id, requires_claim_ref) VALUES "
        "('required-seat', true), ('open-seat', false)"
    )
    scratch.execute(
        "INSERT INTO lease_lanes (lease_id, lane, armed_by) VALUES "
        "('gated-lease', 'gated', 'consumer'), "
        "('exempt-lease', 'exempt', 'consumer')"
    )
    # confirmed + attested (wire provenance)
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance) VALUES "
        "('confirmed-attested', 'f-ca', 'confirmed', 'codex-a', 'gpt', 'wire')"
    )
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author) "
        "VALUES ('art-rerun', 1, 'rerun log', 'h', 'harness', 'harness-runner') "
        "ON CONFLICT DO NOTHING"
    )
    scratch.execute(
        "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, family_provenance, "
        "restatement, mechanism, falsifier, falsifier_kind, rerun_artefact_id, "
        "rerun_artefact_version) VALUES "
        "('confirmed-attested', 'claude-b', 'claude', 'wire', "
        "'the door is unwired', 'audit.py:13-20 never called', "
        "'a passing wire test', 'command', 'art-rerun', 1)"
    )
    # confirmed, no attestation
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance) VALUES "
        "('confirmed-unattested', 'f-cu', 'confirmed', 'codex-a', 'gpt', 'wire')"
    )


def _envelope(payload: dict) -> Envelope:
    return Envelope(
        id="e-1",
        sender="claude-x",
        branch="dev",
        recipient="codex-x",
        kind="request",
        sent_at="2026-07-26T00:00:00+00:00",
        payload=payload,
    )


def _base_table_write_predicate(conn, role: str, schema: str) -> bool:
    """True if role has any write privilege on any of the four base tables."""
    for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
        rel = f'"{schema}".{table}'
        for priv in (
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        ):
            if conn.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0]:
                return True
    return False


def _relation_view_non_select(conn, role: str, schema: str) -> bool:
    """True if role has any relation-level non-SELECT privilege on any gate view."""
    for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
        rel = f'"{schema}".{view}'
        for priv in (
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        ):
            if conn.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0]:
                return True
    return False


# ---------------------------------------------------------------------------
# Positive path + gate integration
# ---------------------------------------------------------------------------


def test_live_resolver_maps_views_and_unknown_lease_is_none(scratch):
    role = _role_name("live")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        _seed_gate_rows(scratch)

        resolver = PsycopgClaimResolver(
            "unused-reader-dsn",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            resolver.assert_ready()

            assert resolver.seat_requires_claim_ref("missing-seat") is True
            assert resolver.seat_requires_claim_ref("required-seat") is True
            assert resolver.seat_requires_claim_ref("open-seat") is False

            assert resolver.lease_lane("missing-lease") is None
            assert resolver.lease_lane("gated-lease") == "gated"
            assert resolver.lease_lane("exempt-lease") == "exempt"

            assert resolver.claim("missing-claim") is None
            assert resolver.claim("confirmed-attested") == claim_gate.ClaimFacts(
                True, True, "wire"
            )
            assert resolver.claim("confirmed-unattested") == claim_gate.ClaimFacts(
                True, False, "none"
            )

            # Unknown lease + gate: silence is gated, not outage.
            outcome = claim_gate.evaluate(
                _envelope({"task": "t", "worktree_lease": "not-written-by-1d"}),
                seat_id="required-seat",
                resolver=resolver,
            ).outcome
            assert outcome is not None
            assert outcome.code == claim_gate.MISSING_CLAIM_REF

            presented = claim_gate.evaluate(
                _envelope(
                    {
                        "task": "t",
                        "worktree_lease": "not-written-by-1d",
                        "lane": "exempt",
                    }
                ),
                seat_id="required-seat",
                resolver=resolver,
            ).outcome
            assert presented is not None
            assert presented.code == claim_gate.LANE_NOT_ARMED_EXEMPT
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_refuses_owner_even_when_expected_role_matches(scratch):
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    owner_name = scratch.execute("SELECT current_user").fetchone()[0]

    resolver = PsycopgClaimResolver(
        "unused",
        expected_role=owner_name,
        schema=schema,
        connect=_owner_connect_factory(owner_dsn, schema),
    )
    try:
        # Owner fails the capability proof. Relation-level view non-SELECT is checked
        # before base-table writes, so either named refusal is a correct refuse; identity
        # match alone must never admit.
        with pytest.raises(
            claim_gate.StoreUnreachable,
            match=r"(write privilege \w+ on base table |non-SELECT privilege \w+ on view )",
        ):
            resolver.assert_ready()
    finally:
        resolver.close()


def test_readiness_rejects_view_dml_drift_while_base_table_predicate_is_clean(scratch):
    """Combined relation-level INSERT/UPDATE/DELETE on auto-updatable views.

    Kept for coverage of the common drift shape. The isolating kill for the
    relation-level loop is ``test_readiness_rejects_relation_only_view_delete_drift``
    (DELETE has no column form, so the column probe cannot carry the kill).
    """
    role = _role_name("viewdml")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        # Grant relation-level DML on the auto-updatable views only — no base-table DML.
        for view in ("seat_posture_v", "lease_lane_v"):
            scratch.execute(
                sql.SQL("GRANT INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier(schema, view), sql.Identifier(role)
                )
            )

        # Setup specificity: base tables still clean under has_table_privilege.
        assert _base_table_write_predicate(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT privilege \w+ on view ",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_relation_only_view_delete_drift(scratch):
    """P1-2 isolating kill: GRANT DELETE has no column form.

    Relation-level INSERT/UPDATE also set has_any_column_privilege, so a live
    test that grants those still raises via the column probe when the relation
    loop is deleted. DELETE (and TRUNCATE/MAINTAIN/TRIGGER) are relation-only —
    killing the relation loop must make this test go RED.
    """
    role = _role_name("viewdel")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT DELETE ON {} TO {}").format(
                sql.Identifier(schema, "seat_posture_v"), sql.Identifier(role)
            )
        )

        rel = f'"{schema}".seat_posture_v'
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, rel)
        ).fetchone()[0] is True
        # Column form does not exist for DELETE — both column probes stay false.
        for priv in ("INSERT", "UPDATE", "REFERENCES"):
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0] is False
        assert _base_table_write_predicate(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT privilege DELETE on view seat_posture_v",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_relation_only_view_maintain_drift(scratch):
    """P1-2: MAINTAIN is relation-only (PostgreSQL ≥17); owns its own kill arm."""
    role = _role_name("viewmnt")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT MAINTAIN ON {} TO {}").format(
                sql.Identifier(schema, "lease_lane_v"), sql.Identifier(role)
            )
        )

        rel = f'"{schema}".lease_lane_v'
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'MAINTAIN')", (role, rel)
        ).fetchone()[0] is True
        for priv in ("INSERT", "UPDATE", "REFERENCES"):
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0] is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT privilege MAINTAIN on view lease_lane_v",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


@pytest.mark.parametrize(
    ("view_name", "column_name"),
    [
        ("seat_posture_v", "requires_claim_ref"),
        ("lease_lane_v", "lane"),
    ],
)
def test_readiness_rejects_column_only_view_update_drift(
    scratch, view_name, column_name
):
    role = _role_name(f"colupd_{column_name[:6]}")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        # Column-level UPDATE only — no relation-level view DML, no base-table privilege.
        scratch.execute(
            sql.SQL("GRANT UPDATE ({}) ON {} TO {}").format(
                sql.Identifier(column_name),
                sql.Identifier(schema, view_name),
                sql.Identifier(role),
            )
        )

        rel = f'"{schema}".{view_name}'
        # Setup specificity for the column half.
        assert scratch.execute(
            "SELECT has_any_column_privilege(%s, %s, 'UPDATE')", (role, rel)
        ).fetchone()[0] is True
        assert _relation_view_non_select(scratch, role, schema) is False
        assert _base_table_write_predicate(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT column privilege UPDATE on view ",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


@pytest.mark.parametrize(
    ("view_name", "column_name"),
    [
        ("seat_posture_v", "requires_claim_ref"),
        ("lease_lane_v", "lane"),
    ],
)
def test_readiness_rejects_column_only_view_insert_drift(
    scratch, view_name, column_name
):
    """Column-INSERT arm of has_any_column_privilege owns its own kill."""
    role = _role_name(f"colins_{column_name[:6]}")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT INSERT ({}) ON {} TO {}").format(
                sql.Identifier(column_name),
                sql.Identifier(schema, view_name),
                sql.Identifier(role),
            )
        )

        rel = f'"{schema}".{view_name}'
        assert scratch.execute(
            "SELECT has_any_column_privilege(%s, %s, 'INSERT')", (role, rel)
        ).fetchone()[0] is True
        assert _relation_view_non_select(scratch, role, schema) is False
        assert _base_table_write_predicate(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT column privilege INSERT on view ",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


@pytest.mark.parametrize(
    ("view_name", "column_name"),
    [
        ("seat_posture_v", "requires_claim_ref"),
        ("lease_lane_v", "lane"),
    ],
)
def test_readiness_rejects_column_only_view_references_drift(
    scratch, view_name, column_name
):
    """P1-2: REFERENCES is the third column privilege form; owns its own kill arm."""
    role = _role_name(f"colref_{column_name[:6]}")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT REFERENCES ({}) ON {} TO {}").format(
                sql.Identifier(column_name),
                sql.Identifier(schema, view_name),
                sql.Identifier(role),
            )
        )

        rel = f'"{schema}".{view_name}'
        assert scratch.execute(
            "SELECT has_any_column_privilege(%s, %s, 'REFERENCES')", (role, rel)
        ).fetchone()[0] is True
        assert _relation_view_non_select(scratch, role, schema) is False
        assert _base_table_write_predicate(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"non-SELECT column privilege REFERENCES on view ",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_column_only_base_table_update_drift(scratch):
    """P1-1: column UPDATE on a base table must fail readiness (fleet-wide gate disable).

    has_table_privilege alone is blind to GRANT UPDATE (col). An unqualified
    UPDATE needs no SELECT, so a drifted reader can flip seat_posture for every
    seat while assert_ready still certifies least-privilege.
    """
    role = _role_name("basetbl_upd")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT UPDATE ({}) ON {} TO {}").format(
                sql.Identifier("requires_claim_ref"),
                sql.Identifier(schema, "seat_posture"),
                sql.Identifier(role),
            )
        )

        rel = f'"{schema}".seat_posture'
        # Setup specificity: table-level write clean, column-level UPDATE present.
        assert _base_table_write_predicate(scratch, role, schema) is False
        assert scratch.execute(
            "SELECT has_any_column_privilege(%s, %s, 'UPDATE')", (role, rel)
        ).fetchone()[0] is True
        assert _relation_view_non_select(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"write column privilege UPDATE on base table seat_posture",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_relation_only_base_table_delete_drift(scratch):
    """P1-B isolating kill: GRANT DELETE on a base table has no column form.

    INSERT/UPDATE/REFERENCES relation grants also set has_any_column_privilege,
    so a live test that grants those still raises via the column probe when the
    base-table relation loop is deleted. DELETE (and TRUNCATE/TRIGGER/MAINTAIN)
    are relation-only — killing the relation loop must make this test go RED.
    The prior unit arm used FakeConnection(base_table_write_privilege=True,
    base_table_column_privileges=None), a state unreachable on real PG17.
    """
    role = _role_name("basedel")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT DELETE ON {} TO {}").format(
                sql.Identifier(schema, "claims"), sql.Identifier(role)
            )
        )

        rel = f'"{schema}".claims'
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, rel)
        ).fetchone()[0] is True
        # Column form does not exist for DELETE — column probes stay false.
        for priv in ("INSERT", "UPDATE", "REFERENCES"):
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0] is False
        assert _relation_view_non_select(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"write privilege DELETE on base table claims",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_relation_only_base_table_trigger_drift(scratch):
    """P1-B: TRIGGER is the admission-relevant relation-only base-table arm.

    An INSTEAD OF / row trigger is an escalation surface; DELETE/TRUNCATE fail
    closed (deleted rows → refuse) and are availability/integrity, not admission.
    """
    role = _role_name("basetrig")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT TRIGGER ON {} TO {}").format(
                sql.Identifier(schema, "seat_posture"), sql.Identifier(role)
            )
        )

        rel = f'"{schema}".seat_posture'
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'TRIGGER')", (role, rel)
        ).fetchone()[0] is True
        for priv in ("INSERT", "UPDATE", "REFERENCES"):
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0] is False
        assert _relation_view_non_select(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"write privilege TRIGGER on base table seat_posture",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_readiness_rejects_relation_only_base_table_maintain_drift(scratch):
    """Coverage-driven: MAINTAIN on base tables must match the view arm (no asymmetry)."""
    role = _role_name("basemnt")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        scratch.execute(
            sql.SQL("GRANT MAINTAIN ON {} TO {}").format(
                sql.Identifier(schema, "lease_lanes"), sql.Identifier(role)
            )
        )

        rel = f'"{schema}".lease_lanes'
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'MAINTAIN')", (role, rel)
        ).fetchone()[0] is True
        for priv in ("INSERT", "UPDATE", "REFERENCES"):
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)", (role, rel, priv)
            ).fetchone()[0] is False
        assert _relation_view_non_select(scratch, role, schema) is False

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=r"write privilege MAINTAIN on base table lease_lanes",
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


# ---------------------------------------------------------------------------
# Structural live kill — derived from PRIVILEGE_COVERAGE (the class-ender)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "priv", "form"),
    _probed_live_kill_cases(),
    ids=[f"{k}-{p}-{f}" for k, p, f in _probed_live_kill_cases()],
)
def test_readiness_refuses_every_probed_coverage_pair(scratch, kind, priv, form):
    """LIVE kill per coverage pair: the kill is the artefact, not the tag.

    Generated from PRIVILEGE_COVERAGE so a new probed pair automatically demands
    a live refuse, and retagging a pair exempt (or dropping a channel half)
    makes the corresponding case vanish from this parametrize *and* either
    import-fail (unknown tag / column-form invariant) or RED here when the
    channel is still claimed but the runtime loop no longer probes it.

    Forms:
      relation        — GRANT priv ON rel; refuse naming that priv+rel
      column          — GRANT priv (col) ON rel only; kills +column half deletion
      select-required — REVOKE priv; refuse missing priv on the view
    Relation-only PG privileges never get a column case (not a silent skip).
    """
    rel_name, col_name = _KIND_GRANT_TARGET[kind]
    role = _role_name(f"cov_{kind[:1]}_{priv[:3].lower()}_{form[:3]}")
    _require_role(scratch, role)
    owner_dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    try:
        apply_gate_reader_grants(scratch, role)
        rel_ident = sql.Identifier(schema, rel_name)
        role_ident = sql.Identifier(role)

        if form == "select-required":
            scratch.execute(
                sql.SQL("REVOKE {} ON {} FROM {}").format(
                    sql.SQL(priv), rel_ident, role_ident
                )
            )
        elif form == "column":
            # Column-form-only: relation-level has_table_privilege stays false.
            scratch.execute(
                sql.SQL("GRANT {} ({}) ON {} TO {}").format(
                    sql.SQL(priv),
                    sql.Identifier(col_name),
                    rel_ident,
                    role_ident,
                )
            )
            rel_q = f'"{schema}".{rel_name}'
            assert scratch.execute(
                "SELECT has_any_column_privilege(%s, %s, %s)",
                (role, rel_q, priv),
            ).fetchone()[0] is True
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (role, rel_q, priv),
            ).fetchone()[0] is False
        elif form == "relation":
            scratch.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(priv), rel_ident, role_ident
                )
            )
            rel_q = f'"{schema}".{rel_name}'
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (role, rel_q, priv),
            ).fetchone()[0] is True
        else:
            raise AssertionError(f"unknown form {form!r}")

        resolver = PsycopgClaimResolver(
            "unused",
            expected_role=role,
            schema=schema,
            connect=_reader_connect_factory(owner_dsn, schema, role),
        )
        try:
            with pytest.raises(
                claim_gate.StoreUnreachable,
                match=_expected_refuse_match(kind, priv, form, rel_name),
            ):
                resolver.assert_ready()
        finally:
            resolver.close()
    finally:
        _drop_role(scratch, role)


def test_pg_table_privileges_matches_server_aclexplode(scratch):
    """PG_TABLE_PRIVILEGES is anchored to the connected server, not a twin literal.

    CREATE TEMP TABLE + GRANT ALL + aclexplode(relacl). Goes RED the day the
    cluster gains a table privilege verb that the constant has not grown.
    """
    role = scratch.execute("SELECT current_user").fetchone()[0]
    scratch.execute("CREATE TEMP TABLE _pg_priv_anchor (id int)")
    scratch.execute(
        sql.SQL("GRANT ALL ON _pg_priv_anchor TO {}").format(sql.Identifier(role))
    )
    rows = scratch.execute(
        """
        SELECT privilege_type
        FROM aclexplode(
          (SELECT relacl FROM pg_class WHERE oid = '_pg_priv_anchor'::regclass)
        )
        WHERE grantee = (SELECT oid FROM pg_roles WHERE rolname = current_user)
        """
    ).fetchall()
    server_privs = {row[0] for row in rows}
    assert server_privs == set(PG_TABLE_PRIVILEGES), (
        f"server aclexplode privileges {sorted(server_privs)} != "
        f"PG_TABLE_PRIVILEGES {sorted(PG_TABLE_PRIVILEGES)}"
    )
