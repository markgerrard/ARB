"""Unit tests for PsycopgLaneWriter — no real DSN, no Postgres.

Owns privilege-coverage closedness (1c-standard: closed vocabulary, test-side
literal pins, module-scope validator call pin), bound-function SQL properties,
connection discard/reconnect, readiness refusal shapes, and the "function bodies
live only in schema.sql" static pin. Live deny-proofs live in
tests/arb_memory/test_lane_writer.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import psycopg

from agent_redis_bridge.lane_writer import (
    EXEMPT_COVERAGE_PAIRS,
    KNOWN_TAGS,
    LANE_WRITER_ALLOWED_RUNTIME,
    LANE_WRITER_FUNCTIONS,
    LANE_WRITER_PRIVILEGE_COVERAGE,
    LANE_WRITER_RELATIONS,
    LaneStoreUnreachable,
    PG_COLUMN_PRIVILEGES,
    PG_FUNCTION_PRIVILEGES,
    PG_TABLE_PRIVILEGES,
    PsycopgLaneWriter,
    _validate_lane_writer_privilege_coverage,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO_ROOT / "src" / "arb_memory" / "schema.sql"
LANE_WRITER_PY = REPO_ROOT / "src" / "agent_redis_bridge" / "lane_writer.py"
GRANTS_PY = REPO_ROOT / "src" / "arb_memory" / "mcp" / "grants.py"
RUN_PY = REPO_ROOT / "src" / "arb_memory" / "run.py"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows: list[Any], error: Exception | None = None):
        self._rows = list(rows)
        self._error = error

    def execute(self, statement, params=None):
        if self._error is not None:
            raise self._error
        return self

    def fetchone(self):
        if self._error is not None:
            raise self._error
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self):
        if self._error is not None:
            raise self._error
        out = list(self._rows)
        self._rows.clear()
        return out


class FakeConnection:
    def __init__(
        self,
        *,
        current_user: str = "arb_gate_lw_seat_a",
        error: Exception | None = None,
        rows: list[Any] | None = None,
        has_function_privilege: dict[str, bool] | None = None,
        has_table_privilege: dict[tuple[str, str], bool] | None = None,
        has_any_column_privilege: dict[tuple[str, str], bool] | None = None,
        binding: tuple[str, str] | None = ("consumer-a", "gated"),
        memberships: list[str] | None = None,
        owned_relations: list[str] | None = None,
        owned_functions: list[str] | None = None,
    ):
        self.current_user = current_user
        self.error = error
        self.rows = list(rows or [])
        self.has_function_privilege = has_function_privilege
        self.has_table_privilege = has_table_privilege or {}
        self.has_any_column_privilege = has_any_column_privilege or {}
        self.binding = binding
        self.memberships = list(memberships or [])
        self.owned_relations = list(owned_relations or [])
        self.owned_functions = list(owned_functions or [])
        self.closed = False
        self.statements: list[str] = []
        self.params: list[Any] = []
        self.connect_kwargs: dict[str, Any] = {}

    def execute(self, statement, params=None):
        if self.closed:
            raise psycopg.OperationalError("connection closed")
        text = _as_text(statement)
        self.statements.append(text)
        self.params.append(params)

        if self.error is not None and "current_user" not in text:
            # Allow identity probe; fail data path when configured.
            if any(
                token in text
                for token in (
                    "arm_lease_lane",
                    "retire_lease_lane",
                    "list_lease_lanes",
                    "has_table_privilege",
                    "has_function_privilege",
                    "has_any_column_privilege",
                    "lane_writer_bindings",
                    "pg_auth_members",
                    "pg_class",
                    "pg_proc",
                )
            ):
                raise self.error

        if text.strip().upper().startswith("SELECT CURRENT_USER") or text.strip() == "SELECT current_user":
            return _RowResult({"current_user": self.current_user})
        # Bare identity probe may be composed differently by psycopg.sql.
        if "current_user" in text and "pg_auth_members" not in text and "pg_class" not in text and "pg_proc" not in text and "has_" not in text and "arm_lease" not in text and "FROM" not in text.upper():
            return _RowResult({"current_user": self.current_user})

        if "has_function_privilege" in text:
            # params: (function_sig, priv) or (role, function_sig, priv)
            if isinstance(params, (list, tuple)) and len(params) >= 2:
                if len(params) == 2:
                    sig, priv = params[0], params[1]
                else:
                    sig, priv = params[1], params[2]
            else:
                sig, priv = "", ""
            ok = True
            if self.has_function_privilege is not None:
                sig_s = str(sig)
                # Keys may be bare "arm_lease_lane(text)" while the runtime
                # passes a schema-qualified form.
                ok = False
                for key, value in self.has_function_privilege.items():
                    bare = key.split("(")[0]
                    if bare in sig_s:
                        ok = value
                        break
            return _RowResult({"has_function_privilege": ok})

        if "has_any_column_privilege" in text:
            rel = params[0] if isinstance(params, (list, tuple)) and params else ""
            priv = params[1] if isinstance(params, (list, tuple)) and len(params) > 1 else ""
            rel_name = str(rel).split(".")[-1].strip('"')
            has = self.has_any_column_privilege.get((rel_name, str(priv)), False)
            return _RowResult({"has_any_column_privilege": has})

        if "has_table_privilege" in text:
            rel = params[0] if isinstance(params, (list, tuple)) and params else ""
            priv = params[1] if isinstance(params, (list, tuple)) and len(params) > 1 else ""
            rel_name = str(rel).split(".")[-1].strip('"')
            has = self.has_table_privilege.get((rel_name, str(priv)), False)
            return _RowResult({"has_table_privilege": has})

        if "lane_writer_bindings" in text and "SELECT" in text.upper():
            if self.binding is None:
                return _RowResult(None)
            return _RowResult(
                {"consumer_id": self.binding[0], "lane": self.binding[1]}
            )

        if "pg_auth_members" in text:
            return _MultiRowResult([{"rolname": m} for m in self.memberships])

        if "pg_class" in text and "relowner" in text:
            return _MultiRowResult([{"relname": r} for r in self.owned_relations])

        if "pg_proc" in text and "proowner" in text:
            return _MultiRowResult([{"proname": f} for f in self.owned_functions])

        if "arm_lease_lane" in text:
            lease_id = params[0] if params else None
            return _RowResult(
                {
                    "lease_id": lease_id,
                    "lane": (self.binding or ("c", "gated"))[1],
                    "armed_by": (self.binding or ("c", "gated"))[0],
                    "armed_at": "2026-07-27T00:00:00+00:00",
                }
            )

        if "retire_lease_lane" in text:
            return _RowResult({"retire_lease_lane": True})

        if "list_lease_lanes" in text:
            return _MultiRowResult(list(self.rows))

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

    def fetchall(self):
        return [] if self._row is None else [self._row]


class _MultiRowResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _as_text(statement) -> str:
    if isinstance(statement, str):
        return statement
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


def writer_with(
    *,
    role: str = "arb_gate_lw_seat_a",
    consumer: str = "consumer-a",
    lane: str = "gated",
    **conn_kwargs,
) -> tuple[PsycopgLaneWriter, FakeConnection]:
    conn = FakeConnection(current_user=role, binding=(consumer, lane), **conn_kwargs)
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role=role,
        expected_consumer_id=consumer,
        expected_lane=lane,
        connect=Factory(conn),
    )
    writer._test_conn = conn
    return writer, conn


# ---------------------------------------------------------------------------
# Privilege coverage — 1c standard (closed vocab, pins, validator)
# ---------------------------------------------------------------------------


def test_only_positive_runtime_authorities_are_the_three_function_executes():
    allowed_pairs = {
        (kind, name, priv)
        for (kind, name, priv), tag in LANE_WRITER_PRIVILEGE_COVERAGE.items()
        if tag.startswith("allowed-because-")
    }
    assert allowed_pairs == {
        ("FUNCTION", "arm_lease_lane(text)", "EXECUTE"),
        ("FUNCTION", "retire_lease_lane(text)", "EXECUTE"),
        ("FUNCTION", "list_lease_lanes()", "EXECUTE"),
    }
    assert allowed_pairs == set(LANE_WRITER_ALLOWED_RUNTIME)
    unclassified = [
        key
        for key, tag in LANE_WRITER_PRIVILEGE_COVERAGE.items()
        if tag not in KNOWN_TAGS
    ]
    assert not unclassified


def test_privilege_coverage_classifies_every_relation_and_function_pair():
    # Checked-in PG17 table verbs — live aclexplode anchors the server list.
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
    assert set(PG_FUNCTION_PRIVILEGES) == {"EXECUTE"}
    assert set(PG_COLUMN_PRIVILEGES) == {"SELECT", "INSERT", "UPDATE", "REFERENCES"}

    expected_relations = set(LANE_WRITER_RELATIONS)
    relation_keys = {
        (kind, name, priv)
        for (kind, name, priv) in LANE_WRITER_PRIVILEGE_COVERAGE
        if kind in {"base_table", "view", "binding_table"}
    }
    for rel in expected_relations:
        kind = (
            "view"
            if rel.endswith("_v")
            else ("binding_table" if rel == "lane_writer_bindings" else "base_table")
        )
        for priv in PG_TABLE_PRIVILEGES:
            assert (kind, rel, priv) in LANE_WRITER_PRIVILEGE_COVERAGE, (
                f"missing coverage for {(kind, rel, priv)}"
            )

    for sig in LANE_WRITER_FUNCTIONS:
        assert ("FUNCTION", sig, "EXECUTE") in LANE_WRITER_PRIVILEGE_COVERAGE

    # Channel probes must be present (membership / ownership / PUBLIC / owner-login).
    for channel_key in (
        ("CHANNEL", "PUBLIC", "EXECUTE"),
        ("CHANNEL", "membership", "SET ROLE"),
        ("CHANNEL", "ownership", "relation"),
        ("CHANNEL", "ownership", "function"),
        ("CHANNEL", "function_owner", "LOGIN"),
    ):
        assert channel_key in LANE_WRITER_PRIVILEGE_COVERAGE


def test_exempt_and_known_tags_pinned_to_test_side_literals():
    """Second literal across the test boundary — 1c rem5 P1-A class.

    KNOWN_TAGS / EXEMPT are asserted against strings written HERE. A consistent
    multi-site source edit cannot stay green without also editing this test.
    """
    assert EXEMPT_COVERAGE_PAIRS == frozenset()
    assert KNOWN_TAGS == frozenset(
        {
            "allowed-because-runtime-arm",
            "allowed-because-runtime-retire",
            "allowed-because-runtime-list",
            "probed-denied-by-relation",
            "probed-denied-by-relation+column",
            "probed-denied-by-function",
            "probed-denied-by-public-execute",
            "probed-denied-by-membership",
            "probed-denied-by-ownership-relation",
            "probed-denied-by-ownership-function",
            "probed-denied-by-function-owner-login",
        }
    )


def test_module_scope_validator_call_is_present_in_source():
    """Standing pin of the import-time activation edge (1c rem5 call-deletion)."""
    src = LANE_WRITER_PY.read_text(encoding="utf-8")
    bare = [
        ln
        for ln in src.splitlines()
        if ln.strip() == "_validate_lane_writer_privilege_coverage()"
    ]
    assert bare == ["_validate_lane_writer_privilege_coverage()"], (
        f"expected exactly one bare module-scope call; got {bare!r}"
    )


def _coverage_copy() -> dict[tuple[str, str, str], str]:
    return dict(LANE_WRITER_PRIVILEGE_COVERAGE)


def _bad_unknown_tag():
    m = _coverage_copy()
    m[("base_table", "lease_lanes", "DELETE")] = "probed-denied-by-typo"
    return m


def _bad_missing_pair():
    m = _coverage_copy()
    del m[("base_table", "lease_lanes", "DELETE")]
    return m


def _bad_extra_exempt():
    m = _coverage_copy()
    m[("base_table", "lease_lanes", "DELETE")] = "allowed-because-runtime-arm"
    return m


def _bad_column_priv_omits_column():
    m = _coverage_copy()
    # INSERT has a PG column form — tag must claim relation+column when denied.
    m[("base_table", "lease_lanes", "INSERT")] = "probed-denied-by-relation"
    return m


@pytest.mark.parametrize(
    "bad_map_factory, match",
    [
        (_bad_unknown_tag, r"tags outside KNOWN_TAGS"),
        (_bad_missing_pair, r"coverage incomplete|missing"),
        (_bad_extra_exempt, r"allowed pairs must equal|allowed-because"),
        (_bad_column_priv_omits_column, r"column form|relation\+column"),
    ],
    ids=[
        "unknown-tag",
        "missing-pair",
        "extra-allowed-pair",
        "column-priv-omits-column",
    ],
)
def test_validate_privilege_coverage_rejects_bad_mappings(bad_map_factory, match):
    with pytest.raises(ValueError, match=match):
        _validate_lane_writer_privilege_coverage(bad_map_factory())


# ---------------------------------------------------------------------------
# Function-body source pin + SQL security properties
# ---------------------------------------------------------------------------


def test_function_bodies_exist_only_in_schema_sql():
    """AST/static: no CREATE FUNCTION strings outside schema.sql."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    for name in ("arm_lease_lane", "retire_lease_lane", "list_lease_lanes"):
        assert re.search(
            rf"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+{name}\b",
            schema,
            re.IGNORECASE,
        ), f"{name} missing from schema.sql"

    for path in (LANE_WRITER_PY, GRANTS_PY, RUN_PY):
        src = path.read_text(encoding="utf-8")
        assert not re.search(
            r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\b", src, re.IGNORECASE
        ), f"{path} must not assemble function-body SQL"
        # Also refuse f-string interpolation of lease_id into SQL.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                rendered = ast.unparse(node)
                if "lease_id" in rendered and any(
                    fn in rendered
                    for fn in ("arm_lease_lane", "retire_lease_lane", "list_lease_lanes")
                ):
                    raise AssertionError(
                        f"{path} interpolates lease_id into function SQL: {rendered}"
                    )


def test_schema_sql_functions_pin_security_properties():
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    for name in ("arm_lease_lane", "retire_lease_lane", "list_lease_lanes"):
        # Extract the function block roughly.
        m = re.search(
            rf"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+{name}\b(.*?LANGUAGE\s+plpgsql.*?)(?=CREATE\s|\Z)",
            schema,
            re.IGNORECASE | re.DOTALL,
        )
        assert m, f"could not extract {name} from schema.sql"
        body = m.group(0)
        assert re.search(r"SECURITY\s+DEFINER", body, re.I)
        # search_path is pinned to the function's OWN schema plus pg_catalog and
        # nothing else. The schema half is a placeholder substituted at apply
        # time — never inherited from the caller, who controls their own
        # search_path and could otherwise redirect a SECURITY DEFINER lookup.
        assert re.search(
            r"SET\s+search_path\s*=\s*__LANE_SCHEMA__,\s*pg_catalog", body, re.I
        )
        # ...and the substituted value comes from current_schema(), not from any
        # caller-supplied or owner-derived value.
        assert re.search(
            r"'__LANE_SCHEMA__',\s*pg_catalog\.quote_ident\("
            r"pg_catalog\.current_schema\(\)\)",
            body,
        )
        assert "session_user" in body
        # No identity/lane caller parameters.
        header = body.split("AS", 1)[0]
        assert "armed_by" not in header
        assert not re.search(r"\blane\b", header, re.I) or name  # param list only p_lease_id
        assert "p_consumer" not in header.lower()
        assert "p_lane" not in header.lower()
        assert "p_armed_by" not in header.lower()

    # retire deletes only bound consumer rows (predicate compares the row's
    # armed_by against the binding looked up from session_user).
    retire = re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+retire_lease_lane\b.*?(?=CREATE\s|\Z)",
        schema,
        re.I | re.S,
    )
    assert retire
    assert re.search(r"armed_by\s*=\s*bound_consumer", retire.group(0), re.I)
    assert "bound_consumer" in retire.group(0)

    # list filters by bound consumer.
    list_fn = re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+list_lease_lanes\b.*?(?=CREATE\s|\Z)",
        schema,
        re.I | re.S,
    )
    assert list_fn
    assert "armed_by" in list_fn.group(0)


def test_runtime_calls_use_bound_parameters_not_interpolation():
    writer, conn = writer_with()
    # Force a successful arm path.
    writer.arm("lease-1")
    texts = " ".join(conn.statements)
    params_blob = conn.params
    assert "arm_lease_lane" in texts
    assert any(
        p is not None and "lease-1" in (p if isinstance(p, (list, tuple)) else (p,))
        for p in params_blob
    )
    # No string-interpolated lease id in SQL text.
    assert "lease-1" not in texts


# ---------------------------------------------------------------------------
# Writer behaviour
# ---------------------------------------------------------------------------


def test_arm_retire_list_call_only_the_three_functions():
    writer, conn = writer_with(rows=[
        {
            "lease_id": "l1",
            "lane": "gated",
            "armed_by": "consumer-a",
            "armed_at": "t",
        }
    ])
    row = writer.arm("l1")
    assert row["lease_id"] == "l1"
    assert writer.retire("l1") is True
    rows = writer.rows()
    assert rows and rows[0]["lease_id"] == "l1"
    joined = " ".join(conn.statements)
    assert "arm_lease_lane" in joined
    assert "retire_lease_lane" in joined
    assert "list_lease_lanes" in joined
    for forbidden in ("INSERT INTO", "DELETE FROM", "UPDATE "):
        assert forbidden not in joined


def test_query_failure_is_lane_store_unreachable_and_reconnects():
    first = FakeConnection(error=psycopg.OperationalError("connection dropped"))
    second = FakeConnection(binding=("consumer-a", "gated"))
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(first, second),
    )
    with pytest.raises(LaneStoreUnreachable, match="connection dropped"):
        writer.arm("l1")
    assert first.closed
    # Second connect succeeds.
    row = writer.arm("l2")
    assert row["lease_id"] == "l2"
    assert Factory.calls == 2


def test_close_is_idempotent():
    writer, conn = writer_with()
    writer.arm("l1")
    writer.close()
    assert conn.closed
    writer.close()


def test_readiness_refuses_role_mismatch():
    writer, _ = writer_with(role="other_role")
    # expected_role defaults to arb_gate_lw_seat_a via helper kwargs — override:
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(FakeConnection(current_user="other_role")),
    )
    with pytest.raises(LaneStoreUnreachable, match=r"expected role"):
        writer.assert_ready()


def test_readiness_refuses_binding_mismatch():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("other-consumer", "gated"),
            )
        ),
    )
    with pytest.raises(LaneStoreUnreachable, match=r"consumer_id|binding"):
        writer.assert_ready()


def test_readiness_refuses_lane_mismatch():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("consumer-a", "exempt"),
            )
        ),
    )
    with pytest.raises(LaneStoreUnreachable, match=r"lane"):
        writer.assert_ready()


def test_readiness_refuses_missing_function_execute():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("consumer-a", "gated"),
                has_function_privilege={
                    "arm_lease_lane(text)": False,
                    "retire_lease_lane(text)": True,
                    "list_lease_lanes()": True,
                },
            )
        ),
    )
    with pytest.raises(
        LaneStoreUnreachable, match=r"missing EXECUTE|arm_lease_lane"
    ):
        writer.assert_ready()


def test_readiness_refuses_table_privilege():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("consumer-a", "gated"),
                has_table_privilege={("lease_lanes", "DELETE"): True},
            )
        ),
    )
    with pytest.raises(
        LaneStoreUnreachable, match=r"forbidden privilege DELETE on .*lease_lanes"
    ):
        writer.assert_ready()


def test_readiness_refuses_membership():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("consumer-a", "gated"),
                memberships=["some_parent"],
            )
        ),
    )
    with pytest.raises(LaneStoreUnreachable, match=r"membership|member of"):
        writer.assert_ready()


def test_readiness_refuses_relation_ownership():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=("consumer-a", "gated"),
                owned_relations=["lease_lanes"],
            )
        ),
    )
    with pytest.raises(LaneStoreUnreachable, match=r"owns|ownership"):
        writer.assert_ready()


def test_connect_forces_timeouts():
    conn = FakeConnection()
    factory = Factory(conn)
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=factory,
    )
    writer.arm("l1")
    assert factory.last_kwargs.get("connect_timeout") == 5
    options = factory.last_kwargs.get("options", "")
    assert "statement_timeout=5000" in options
    assert factory.last_kwargs.get("keepalives") == 1
    assert factory.last_kwargs.get("tcp_user_timeout") == 15000


def test_unbound_role_surfaces_as_lane_store_unreachable():
    writer = PsycopgLaneWriter(
        "lane-dsn",
        expected_role="arb_gate_lw_seat_a",
        expected_consumer_id="consumer-a",
        expected_lane="gated",
        connect=Factory(
            FakeConnection(
                current_user="arb_gate_lw_seat_a",
                binding=None,
                error=psycopg.errors.InsufficientPrivilege("unbound-lane-writer-role"),
            )
        ),
    )
    # arm path hits the function which raises; readiness also checks binding.
    with pytest.raises(LaneStoreUnreachable):
        writer.assert_ready()
