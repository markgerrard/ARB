"""Unit tests for PsycopgClaimResolver — no real DSN, no Postgres.

Every connection is an explicit fake. These tests own mapping contracts, defaults,
connection discard/reconnect, close idempotence, and the readiness identity/capability
probe *shape*. Live mutation-kills for the privilege half live in
tests/arb_memory/test_claim_resolver.py.
"""

from __future__ import annotations

from typing import Any

import pytest
import psycopg

from agent_redis_bridge import claim_gate
from agent_redis_bridge.claim_resolver import PsycopgClaimResolver


class FakeCursor:
    def __init__(self, rows: list[Any], error: Exception | None = None):
        self._rows = list(rows)
        self._error = error
        self.statements: list[Any] = []
        self.params: list[Any] = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        self.params.append(params)
        if self._error is not None:
            raise self._error
        return self

    def fetchone(self):
        if self._error is not None:
            raise self._error
        if not self._rows:
            return None
        return self._rows.pop(0)


class FakeConnection:
    def __init__(
        self,
        rows: list[Any] | None = None,
        *,
        error: Exception | None = None,
        current_user: str = "arb_gate_reader",
        select_privileges: dict[str, bool] | None = None,
        view_table_privileges: dict[str, bool] | None = None,
        view_column_privileges: dict[str, bool] | None = None,
        base_table_write_privilege: bool = False,
        base_table_column_privileges: dict[str, bool] | None = None,
        connect_kwargs: dict[str, Any] | None = None,
    ):
        self.rows = list(rows or [])
        self.error = error
        self.current_user = current_user
        self.select_privileges = select_privileges
        self.view_table_privileges = view_table_privileges
        self.view_column_privileges = view_column_privileges
        self.base_table_write_privilege = base_table_write_privilege
        self.base_table_column_privileges = base_table_column_privileges
        self.connect_kwargs = connect_kwargs or {}
        self.closed = False
        self.statements: list[str] = []
        self.params: list[Any] = []
        self._cursor = FakeCursor(self.rows, error=error)

    def execute(self, statement, params=None):
        if self.closed:
            raise psycopg.OperationalError("connection closed")
        text = _as_text(statement)
        self.statements.append(text)
        self.params.append(params)

        # Identity probe
        if "current_user" in text:
            return _RowResult({"current_user": self.current_user})

        if "has_table_privilege" in text:
            rel = params[0] if isinstance(params, (list, tuple)) and params else ""
            if isinstance(params, (list, tuple)) and len(params) > 1:
                priv = str(params[1])
            elif "'SELECT'" in text or '"SELECT"' in text:
                priv = "SELECT"
            else:
                priv = ""
            rel_name = str(rel).split(".")[-1].strip('"')
            if priv.upper() == "SELECT":
                ok = True
                if self.select_privileges is not None:
                    ok = self.select_privileges.get(rel_name, False)
                return _RowResult({"has_table_privilege": ok})
            # non-SELECT relation-level on views or base tables
            if rel_name in {
                "claim_admissibility_v",
                "seat_posture_v",
                "lease_lane_v",
            }:
                has = False
                if self.view_table_privileges is not None:
                    has = self.view_table_privileges.get(rel_name, False)
                return _RowResult({"has_table_privilege": has})
            # base tables
            return _RowResult({"has_table_privilege": self.base_table_write_privilege})

        if "has_any_column_privilege" in text:
            rel = params[0] if isinstance(params, (list, tuple)) and params else ""
            rel_name = str(rel).split(".")[-1].strip('"')
            has = False
            if rel_name in {
                "claim_admissibility_v",
                "seat_posture_v",
                "lease_lane_v",
            }:
                if self.view_column_privileges is not None:
                    has = self.view_column_privileges.get(rel_name, False)
            elif self.base_table_column_privileges is not None:
                has = self.base_table_column_privileges.get(rel_name, False)
            return _RowResult({"has_any_column_privilege": has})

        # LIMIT 0 view probes — empty result is fine
        if "LIMIT 0" in text.upper() or "limit 0" in text:
            return _RowResult(None)

        # Lookup path: raise on real SELECT-from-view queries when configured
        if self.error is not None:
            raise self.error
        if not self.rows:
            return _RowResult(None)
        return _RowResult(self.rows.pop(0))

    def close(self):
        self.closed = True


class _RowResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def _as_text(statement) -> str:
    if isinstance(statement, str):
        return statement
    # psycopg.sql.Composable
    try:
        return statement.as_string(None)
    except Exception:
        return str(statement)


class Factory:
    calls = 0
    _queue: list[FakeConnection] = []
    last_kwargs: dict[str, Any] = {}

    def __init__(self, *conns: FakeConnection):
        Factory.calls = 0
        Factory._queue = list(conns)
        Factory.last_kwargs = {}

    def __call__(self, dsn, **kwargs):
        Factory.calls += 1
        Factory.last_kwargs = dict(kwargs)
        if not Factory._queue:
            raise AssertionError(f"unexpected connect #{Factory.calls} to {dsn!r}")
        conn = Factory._queue.pop(0)
        conn.connect_kwargs = dict(kwargs)
        return conn


def resolver_with_rows(rows, **kwargs):
    conn = FakeConnection(rows=rows)
    return PsycopgClaimResolver("reader-dsn", connect=Factory(conn), **kwargs)


def resolver_with_role(
    role: str,
    *,
    expected_role: str | None = None,
    base_table_write_privilege: bool = False,
    base_table_column_privileges: dict[str, bool] | None = None,
    view_table_privileges: dict[str, bool] | None = None,
    view_column_privileges: dict[str, bool] | None = None,
    select_privileges: dict[str, bool] | None = None,
):
    conn = FakeConnection(
        current_user=role,
        base_table_write_privilege=base_table_write_privilege,
        base_table_column_privileges=base_table_column_privileges,
        view_table_privileges=view_table_privileges,
        view_column_privileges=view_column_privileges,
        select_privileges=select_privileges,
    )
    # Attach probe bookkeeping on the resolver after assert_ready via the conn
    kwargs = {}
    if expected_role is not None:
        kwargs["expected_role"] = expected_role
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(conn), **kwargs)
    # Expose the connection for test assertions about what was probed
    resolver._test_conn = conn
    return resolver


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------


def test_missing_posture_defaults_to_requiring_a_claim():
    resolver = resolver_with_rows([None])
    assert resolver.seat_requires_claim_ref("unconfigured-seat") is True


def test_posture_accepts_only_exact_boolean_values():
    assert resolver_with_rows([{"requires_claim_ref": True}]).seat_requires_claim_ref("s") is True
    assert resolver_with_rows([{"requires_claim_ref": False}]).seat_requires_claim_ref("s") is False


@pytest.mark.parametrize("value", [None, 0, "", []])
def test_malformed_falsey_posture_is_store_unreachable_not_an_admit(value):
    resolver = resolver_with_rows([{"requires_claim_ref": value}])
    with pytest.raises(claim_gate.StoreUnreachable, match="requires_claim_ref"):
        resolver.seat_requires_claim_ref("s")


def test_missing_posture_key_is_store_unreachable():
    resolver = resolver_with_rows([{}])
    with pytest.raises(claim_gate.StoreUnreachable):
        resolver.seat_requires_claim_ref("s")


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


def test_unknown_lease_is_none_not_store_unreachable():
    assert resolver_with_rows([None]).lease_lane("not-written-by-1d") is None


def test_valid_lanes_map_exactly():
    assert resolver_with_rows([{"lane": "gated"}]).lease_lane("l") == "gated"
    assert resolver_with_rows([{"lane": "exempt"}]).lease_lane("l") == "exempt"


def test_invalid_present_lane_is_store_unreachable():
    resolver = resolver_with_rows([{"lane": "probe"}])
    with pytest.raises(claim_gate.StoreUnreachable, match="lane"):
        resolver.lease_lane("l")


def test_missing_lane_key_is_store_unreachable():
    resolver = resolver_with_rows([{}])
    with pytest.raises(claim_gate.StoreUnreachable):
        resolver.lease_lane("l")


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def test_claim_maps_view_columns_without_recomputing():
    found = resolver_with_rows([{
        "confirmed_now": True,
        "attested": False,
        "decorrelation_provenance": "degraded",
    }]).claim("c-1")
    assert found == claim_gate.ClaimFacts(
        confirmed_now=True,
        attested=False,
        decorrelation_provenance="degraded",
    )


def test_missing_claim_is_none():
    assert resolver_with_rows([None]).claim("nope") is None


@pytest.mark.parametrize(
    "row",
    [
        {"attested": True, "decorrelation_provenance": "wire"},  # missing confirmed_now
        {"confirmed_now": True, "decorrelation_provenance": "wire"},  # missing attested
        {"confirmed_now": True, "attested": True},  # missing provenance
        {"confirmed_now": 1, "attested": True, "decorrelation_provenance": "wire"},
        {"confirmed_now": True, "attested": 0, "decorrelation_provenance": "wire"},
        {"confirmed_now": True, "attested": True, "decorrelation_provenance": "vibes"},
    ],
)
def test_malformed_claim_row_is_store_unreachable(row):
    resolver = resolver_with_rows([row])
    with pytest.raises(claim_gate.StoreUnreachable):
        resolver.claim("c-1")


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_query_failure_is_named_store_unreachable_and_next_call_reconnects():
    first = FakeConnection(error=psycopg.OperationalError("connection dropped"))
    second = FakeConnection(rows=[{"requires_claim_ref": False}])
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(first, second))

    with pytest.raises(claim_gate.StoreUnreachable, match="connection dropped"):
        resolver.seat_requires_claim_ref("s")
    assert first.closed
    assert resolver.seat_requires_claim_ref("s") is False
    assert Factory.calls == 2


def test_contract_failure_discards_and_reconnects_on_next_call():
    first = FakeConnection(rows=[{"requires_claim_ref": 0}])
    second = FakeConnection(rows=[{"requires_claim_ref": False}])
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(first, second))

    with pytest.raises(claim_gate.StoreUnreachable, match="requires_claim_ref"):
        resolver.seat_requires_claim_ref("s")
    assert first.closed
    assert resolver.seat_requires_claim_ref("s") is False
    assert Factory.calls == 2


def test_close_is_idempotent():
    conn = FakeConnection(rows=[{"requires_claim_ref": False}])
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(conn))
    assert resolver.seat_requires_claim_ref("s") is False
    resolver.close()
    assert conn.closed
    resolver.close()  # must not raise


def test_lookup_sql_selects_only_published_view_columns():
    conn = FakeConnection(rows=[
        {"requires_claim_ref": True},
        {"lane": "gated"},
        {
            "confirmed_now": True,
            "attested": False,
            "decorrelation_provenance": "none",
        },
    ])
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(conn))
    resolver.seat_requires_claim_ref("s")
    resolver.lease_lane("l")
    resolver.claim("c")

    texts = " ".join(conn.statements)
    assert "seat_posture_v" in texts
    assert "lease_lane_v" in texts
    assert "claim_admissibility_v" in texts
    assert "requires_claim_ref" in texts
    assert "confirmed_now" in texts
    assert "attested" in texts
    assert "decorrelation_provenance" in texts
    # Never raw tables or a status predicate on the lookup path
    for forbidden in (
        " FROM claims ",
        " FROM seat_posture ",
        " FROM lease_lanes ",
        " FROM attestations ",
        "status =",
        "review_by",
    ):
        assert forbidden not in texts, f"lookup SQL leaked {forbidden!r}: {texts}"


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_checks_identity_and_all_three_views():
    resolver = resolver_with_role("arb_gate_reader")
    resolver.assert_ready()
    conn = resolver._test_conn
    texts = " ".join(conn.statements)
    # Relation names for privilege probes are bind params, not SQL literals.
    param_blob = " ".join(
        str(p) for p in conn.params if p is not None
    )
    probed = texts + " " + param_blob
    for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
        assert view in probed, f"readiness never probed {view}"
    assert "has_table_privilege" in texts
    assert "has_any_column_privilege" in texts
    for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
        assert table in probed, f"readiness never probed base table {table}"


def test_readiness_refuses_owner_even_when_expected_role_matches_owner():
    resolver = resolver_with_role(
        "arb_memory",
        expected_role="arb_memory",
        base_table_write_privilege=True,
    )
    with pytest.raises(claim_gate.StoreUnreachable, match="write privilege"):
        resolver.assert_ready()


def test_readiness_refuses_relation_level_view_dml():
    resolver = resolver_with_role(
        "arb_gate_reader",
        view_table_privileges={"seat_posture_v": True},
    )
    with pytest.raises(
        claim_gate.StoreUnreachable, match=r"non-SELECT privilege \w+ on view "
    ):
        resolver.assert_ready()


def test_readiness_refuses_column_level_view_privilege():
    resolver = resolver_with_role(
        "arb_gate_reader",
        view_column_privileges={"lease_lane_v": True},
    )
    with pytest.raises(
        claim_gate.StoreUnreachable, match=r"non-SELECT column privilege \w+ on view "
    ):
        resolver.assert_ready()


def test_readiness_refuses_column_level_base_table_privilege():
    """P1-1 unit arm: base-table column write is refused, not only relation writes."""
    resolver = resolver_with_role(
        "arb_gate_reader",
        base_table_column_privileges={"seat_posture": True},
    )
    with pytest.raises(
        claim_gate.StoreUnreachable,
        match=r"write column privilege \w+ on base table seat_posture",
    ):
        resolver.assert_ready()


def test_connect_forces_timeouts():
    """P1-3 / P1-C: hung or dead-idle store must not stall the inbox lock forever.

    connect_timeout + statement_timeout bound "server answers but is slow".
    libpq keepalives + tcp_user_timeout bound "socket looks ESTABLISHED but
    never answers" (failover / NAT drop / partition) — the case statement_timeout
    cannot see because it is server-side.
    """
    conn = FakeConnection()
    factory = Factory(conn)
    resolver = PsycopgClaimResolver("reader-dsn", connect=factory)
    resolver.seat_requires_claim_ref("s")  # triggers connect
    assert factory.last_kwargs.get("connect_timeout") == 5
    options = factory.last_kwargs.get("options", "")
    assert "statement_timeout=5000" in options
    assert factory.last_kwargs.get("keepalives") == 1
    assert factory.last_kwargs.get("keepalives_idle") == 10
    assert factory.last_kwargs.get("keepalives_interval") == 5
    assert factory.last_kwargs.get("keepalives_count") == 3
    assert factory.last_kwargs.get("tcp_user_timeout") == 15000


# ---------------------------------------------------------------------------
# Privilege coverage — kill the defect CLASS, not the next instance
# ---------------------------------------------------------------------------


def test_privilege_coverage_classifies_every_pg_table_privilege_pair():
    """Any unclassified (relation-kind, privilege) pair is a RED.

    Closed vocabulary + pinned exempt set + full cartesian product. Misspelled
    tags refuse module load (KNOWN_TAGS); this test re-asserts the same
    invariants against the loaded module so a regression is visible in pytest.
    """
    from agent_redis_bridge.claim_resolver import (
        EXEMPT_COVERAGE_PAIRS,
        KNOWN_TAGS,
        PG_COLUMN_PRIVILEGES,
        PG_TABLE_PRIVILEGES,
        PRIVILEGE_COVERAGE,
        RELATION_KINDS,
    )

    # Checked-in PostgreSQL 17 list — the live aclexplode test is the server
    # anchor; this only keeps the unit suite self-contained without DSN.
    assert set(PG_TABLE_PRIVILEGES) == {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    }
    assert set(RELATION_KINDS) == {"view", "base_table"}

    expected = {(kind, priv) for kind in RELATION_KINDS for priv in PG_TABLE_PRIVILEGES}
    missing = sorted(expected - set(PRIVILEGE_COVERAGE))
    extra = sorted(set(PRIVILEGE_COVERAGE) - expected)
    assert not missing, (
        f"unclassified (relation-kind, privilege) pairs — every PG table "
        f"privilege on every relation kind must be in KNOWN_TAGS: {missing}"
    )
    assert not extra, f"coverage has keys outside the cartesian product: {extra}"

    bad_tags = [
        (key, tag)
        for key, tag in PRIVILEGE_COVERAGE.items()
        if tag not in KNOWN_TAGS
    ]
    assert not bad_tags, f"coverage tags outside closed KNOWN_TAGS: {bad_tags}"

    actual_exempt = {
        pair
        for pair, tag in PRIVILEGE_COVERAGE.items()
        if tag.startswith("exempt-because-")
    }
    assert actual_exempt == set(EXEMPT_COVERAGE_PAIRS)

    # Column-form invariant (structural): non-SELECT privs with a PG column form
    # must claim relation+column; relation-only PG privs must not claim column.
    for (kind, priv), tag in PRIVILEGE_COVERAGE.items():
        if tag.startswith("exempt-because-") or priv == "SELECT":
            continue
        if priv in PG_COLUMN_PRIVILEGES:
            assert "relation" in tag and "column" in tag, (
                f"{(kind, priv)} has PG column form but tag={tag!r}"
            )
        else:
            assert "column" not in tag and "relation" in tag, (
                f"{(kind, priv)} has no PG column form but tag={tag!r}"
            )


def test_privilege_coverage_drives_the_runtime_probe_tuples():
    """Probe loops are derived from coverage via the production helper.

    Uses ``_privs_probed_via`` (the same gate production applies, including
    ``startswith("probed-by-")``) rather than re-implementing substring rules
    in the test — a divergent test oracle was cold-Opus P1-β.
    """
    from agent_redis_bridge.claim_resolver import (
        _BASE_TABLE_COLUMN_WRITES,
        _BASE_TABLE_WRITES,
        _VIEW_COLUMN_NON_SELECT,
        _VIEW_RELATION_NON_SELECT,
        _VIEW_SELECT_REQUIRED,
        _privs_probed_via,
    )

    assert _VIEW_SELECT_REQUIRED == _privs_probed_via("view", "select-required")
    assert _VIEW_SELECT_REQUIRED == ("SELECT",)
    assert _VIEW_RELATION_NON_SELECT == _privs_probed_via("view", "relation")
    assert _VIEW_COLUMN_NON_SELECT == _privs_probed_via("view", "column")
    assert _BASE_TABLE_WRITES == _privs_probed_via("base_table", "relation")
    assert _BASE_TABLE_COLUMN_WRITES == _privs_probed_via("base_table", "column")

    # Admission-relevant relation-only arms must be present (no column form).
    for priv in ("DELETE", "TRUNCATE", "TRIGGER"):
        assert priv in _VIEW_RELATION_NON_SELECT
        assert priv in _BASE_TABLE_WRITES
    assert "MAINTAIN" in _VIEW_RELATION_NON_SELECT
    assert "MAINTAIN" in _BASE_TABLE_WRITES  # closes the view/base asymmetry


def _coverage_copy() -> dict[tuple[str, str], str]:
    """Fresh mutable copy of the production coverage map for unit mutation."""
    from agent_redis_bridge.claim_resolver import PRIVILEGE_COVERAGE

    return dict(PRIVILEGE_COVERAGE)


def _bad_unknown_tag() -> dict[tuple[str, str], str]:
    m = _coverage_copy()
    m[("view", "INSERT")] = "probed-by-typo-not-in-vocabulary"
    return m


def _bad_missing_pair() -> dict[tuple[str, str], str]:
    m = _coverage_copy()
    del m[("view", "TRIGGER")]
    return m


def _bad_extra_exempt_pair() -> dict[tuple[str, str], str]:
    """A second exempt pair without updating EXEMPT_COVERAGE_PAIRS."""
    m = _coverage_copy()
    m[("view", "INSERT")] = (
        "exempt-because-select-alone-is-not-a-mint-or-escalation-path"
    )
    return m


def _bad_column_priv_omits_column() -> dict[tuple[str, str], str]:
    """INSERT is +column-capable; strip the column claim from its tag."""
    m = _coverage_copy()
    m[("view", "INSERT")] = "probed-by-view-relation"
    return m


@pytest.mark.parametrize(
    "bad_map_factory, match",
    [
        (_bad_unknown_tag, r"tags outside KNOWN_TAGS"),
        (_bad_missing_pair, r"full cartesian product"),
        (_bad_extra_exempt_pair, r"exempt pairs must equal EXEMPT_COVERAGE_PAIRS"),
        (_bad_column_priv_omits_column, r"has a PG column form; tag must claim"),
    ],
    ids=[
        "unknown-tag",
        "missing-pair",
        "extra-exempt-pair",
        "column-priv-omits-column",
    ],
)
def test_validate_privilege_coverage_rejects_bad_mappings(bad_map_factory, match):
    """Direct unit kill of the import-time validator — guards the guard.

    Eighth-instance shape: the module-scope call protects PRIVILEGE_COVERAGE,
    but nothing called the validator with a bad map. These cases pass the
    coverage mapping as a parameter so each ValueError is RED-demonstrable by
    weakening the corresponding branch in ``_validate_privilege_coverage``.
    """
    from agent_redis_bridge.claim_resolver import _validate_privilege_coverage

    with pytest.raises(ValueError, match=match):
        _validate_privilege_coverage(bad_map_factory())


def test_exempt_and_known_tags_pinned_to_test_side_literals():
    """Second literal across the test boundary — rem5 P1-A kill.

    EXEMPT_COVERAGE_PAIRS and KNOWN_TAGS are asserted against strings written
    HERE, not against the same module object imported twice. A consistent
    3-site source edit (retag a probed pair to a new exempt tag, add the tag
    to KNOWN_TAGS, add the pair to EXEMPT_COVERAGE_PAIRS) keeps the import-time
    validator happy and previously left the whole unit suite green; after this
    pin, that edit cannot stay green without also editing THIS test file. That
    boundary is the protection.
    """
    from agent_redis_bridge.claim_resolver import EXEMPT_COVERAGE_PAIRS, KNOWN_TAGS

    assert EXEMPT_COVERAGE_PAIRS == frozenset({("base_table", "SELECT")})
    assert KNOWN_TAGS == frozenset(
        {
            "probed-by-view-select-required",
            "probed-by-view-relation",
            "probed-by-view-relation+column",
            "probed-by-base-relation",
            "probed-by-base-relation+column",
            "exempt-because-select-alone-is-not-a-mint-or-escalation-path",
        }
    )


def test_module_scope_validator_call_is_present_in_source():
    """Standing unit pin of the import-time activation edge.

    Deleting only the bare module-scope ``_validate_privilege_coverage()`` call
    leaves the production map valid: import succeeds, the parametrized unit
    kills still call the function with bad maps, and live probes still use the
    good map — so unit+live stay green without a source pin of the call itself.
    This test is the named standing check that goes RED on call-deletion
    (rem5 acceptance (b)). A retag mutant does NOT depend on this call; unit
    classification tests catch retags with or without it — do not claim the
    opposite (the 27af2b75 mechanism claim was false).
    """
    from pathlib import Path

    import agent_redis_bridge.claim_resolver as cr

    src = Path(cr.__file__).read_text()
    bare = [
        ln for ln in src.splitlines() if ln.strip() == "_validate_privilege_coverage()"
    ]
    assert bare == ["_validate_privilege_coverage()"], (
        f"expected exactly one bare module-scope call; got {bare!r}"
    )


def test_gate_views_and_base_tables_derive_from_gate_relations():
    """Relation inventory is one source — addition to GATE_RELATIONS must land here."""
    from agent_redis_bridge.claim_resolver import BASE_TABLES, GATE_VIEWS
    from arb_memory.mcp.grants import GATE_RELATIONS

    assert GATE_VIEWS == tuple(r for r in GATE_RELATIONS if r.endswith("_v"))
    assert BASE_TABLES == tuple(r for r in GATE_RELATIONS if not r.endswith("_v"))
    assert set(GATE_VIEWS) | set(BASE_TABLES) == set(GATE_RELATIONS)
    assert not (set(GATE_VIEWS) & set(BASE_TABLES))


def test_relname_quotes_schema_and_relation():
    """Privilege probes must target the same identifier the data path uses.

    sql.Identifier quotes; an unquoted f-string of a mixed-case schema would
    fold to lowercase and evaluate capability against a different relation.
    """
    resolver = PsycopgClaimResolver("reader-dsn", schema="GateSchema")
    assert resolver._relname("seat_posture_v") == '"GateSchema"."seat_posture_v"'
    # Embedded double-quotes are escaped per PG identifier rules.
    resolver2 = PsycopgClaimResolver("reader-dsn", schema='a"b')
    assert resolver2._relname("t") == '"a""b"."t"'


def test_readiness_refuses_wrong_role():
    resolver = resolver_with_role("somebody_else", expected_role="arb_gate_reader")
    with pytest.raises(
        claim_gate.StoreUnreachable, match=r"expected role 'arb_gate_reader'"
    ):
        resolver.assert_ready()


def test_readiness_refuses_missing_select_on_a_view():
    resolver = resolver_with_role(
        "arb_gate_reader",
        select_privileges={
            "claim_admissibility_v": True,
            "seat_posture_v": False,
            "lease_lane_v": True,
        },
    )
    with pytest.raises(
        claim_gate.StoreUnreachable,
        match=r"missing SELECT privilege on view seat_posture_v",
    ):
        resolver.assert_ready()
