"""PostgreSQL-backed per-seat lane writer (Slice 1d-i).

Owns one dedicated, autocommit connection authenticated as a per-seat login
role that holds EXECUTE only on three owner-controlled SECURITY DEFINER
functions. Identity (armed_by) and lane are database-bound via
``lane_writer_bindings`` keyed by ``session_user``; the runtime role never
receives direct ``lease_lanes`` DML.

Function bodies live only in ``src/arb_memory/schema.sql``. This module calls
them with bound parameters and never assembles function-body SQL.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Privilege coverage — DATA, not hand-maintained probe lists (1c pattern).
#
# Tags are closed at import (KNOWN_TAGS). Allowed runtime authorities are
# pinned to LANE_WRITER_ALLOWED_RUNTIME. The live artefact is the parametrized
# kill in tests/arb_memory/test_lane_writer.py that injects each denied arm
# and requires assert_ready() to refuse by name.
# ---------------------------------------------------------------------------

PG_TABLE_PRIVILEGES: tuple[str, ...] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)

PG_COLUMN_PRIVILEGES: frozenset[str] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "REFERENCES"}
)

PG_FUNCTION_PRIVILEGES: tuple[str, ...] = ("EXECUTE",)

LANE_WRITER_FUNCTIONS: tuple[str, ...] = (
    "arm_lease_lane(text)",
    "retire_lease_lane(text)",
    "list_lease_lanes()",
)

# Gate relations the writer must not hold any privilege on, plus the
# owner-only binding table. Views end in _v.
LANE_WRITER_RELATIONS: tuple[str, ...] = (
    "claims",
    "attestations",
    "seat_posture",
    "lease_lanes",
    "lane_writer_bindings",
    "claim_admissibility_v",
    "seat_posture_v",
    "lease_lane_v",
)

KNOWN_TAGS: frozenset[str] = frozenset(
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

# No exempt privilege pairs: every non-allowed arm is probed-denied.
EXEMPT_COVERAGE_PAIRS: frozenset[tuple[str, str, str]] = frozenset()

LANE_WRITER_ALLOWED_RUNTIME: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("FUNCTION", "arm_lease_lane(text)", "EXECUTE"),
        ("FUNCTION", "retire_lease_lane(text)", "EXECUTE"),
        ("FUNCTION", "list_lease_lanes()", "EXECUTE"),
    }
)

_VALID_LANES = frozenset({"gated", "exempt"})


def _relation_kind(name: str) -> str:
    if name.endswith("_v"):
        return "view"
    if name == "lane_writer_bindings":
        return "binding_table"
    return "base_table"


def _build_default_coverage() -> dict[tuple[str, str, str], str]:
    coverage: dict[tuple[str, str, str], str] = {
        ("FUNCTION", "arm_lease_lane(text)", "EXECUTE"): "allowed-because-runtime-arm",
        ("FUNCTION", "retire_lease_lane(text)", "EXECUTE"): (
            "allowed-because-runtime-retire"
        ),
        ("FUNCTION", "list_lease_lanes()", "EXECUTE"): "allowed-because-runtime-list",
        ("CHANNEL", "PUBLIC", "EXECUTE"): "probed-denied-by-public-execute",
        ("CHANNEL", "membership", "SET ROLE"): "probed-denied-by-membership",
        ("CHANNEL", "ownership", "relation"): "probed-denied-by-ownership-relation",
        ("CHANNEL", "ownership", "function"): "probed-denied-by-ownership-function",
        ("CHANNEL", "function_owner", "LOGIN"): "probed-denied-by-function-owner-login",
    }
    for rel in LANE_WRITER_RELATIONS:
        kind = _relation_kind(rel)
        for priv in PG_TABLE_PRIVILEGES:
            if priv in PG_COLUMN_PRIVILEGES:
                coverage[(kind, rel, priv)] = "probed-denied-by-relation+column"
            else:
                coverage[(kind, rel, priv)] = "probed-denied-by-relation"
    # Non-allowlisted functions (none today beyond the three) would land as
    # probed-denied-by-function. The three allowed EXECUTE arms are set above.
    for sig in LANE_WRITER_FUNCTIONS:
        key = ("FUNCTION", sig, "EXECUTE")
        if key not in coverage:
            coverage[key] = "probed-denied-by-function"
    return coverage


LANE_WRITER_PRIVILEGE_COVERAGE: dict[tuple[str, str, str], str] = (
    _build_default_coverage()
)


def _validate_lane_writer_privilege_coverage(
    coverage: dict[tuple[str, str, str], str] | None = None,
) -> None:
    """Closedness of the lane-writer coverage map. Fail loud, not green-by-skip."""
    coverage = (
        LANE_WRITER_PRIVILEGE_COVERAGE if coverage is None else coverage
    )

    unknown = sorted({tag for tag in coverage.values() if tag not in KNOWN_TAGS})
    if unknown:
        raise ValueError(
            f"LANE_WRITER_PRIVILEGE_COVERAGE has tags outside KNOWN_TAGS: {unknown}"
        )

    # Required relation × privilege cartesian product.
    expected_rel: set[tuple[str, str, str]] = set()
    for rel in LANE_WRITER_RELATIONS:
        kind = _relation_kind(rel)
        for priv in PG_TABLE_PRIVILEGES:
            expected_rel.add((kind, rel, priv))
    present_rel = {
        k for k in coverage if k[0] in {"base_table", "view", "binding_table"}
    }
    missing = sorted(expected_rel - present_rel)
    if missing:
        raise ValueError(
            f"LANE_WRITER_PRIVILEGE_COVERAGE coverage incomplete; missing={missing}"
        )

    # Function EXECUTE arms.
    for sig in LANE_WRITER_FUNCTIONS:
        if ("FUNCTION", sig, "EXECUTE") not in coverage:
            raise ValueError(
                f"LANE_WRITER_PRIVILEGE_COVERAGE missing function EXECUTE for {sig}"
            )

    # Required channel probes.
    for channel_key in (
        ("CHANNEL", "PUBLIC", "EXECUTE"),
        ("CHANNEL", "membership", "SET ROLE"),
        ("CHANNEL", "ownership", "relation"),
        ("CHANNEL", "ownership", "function"),
        ("CHANNEL", "function_owner", "LOGIN"),
    ):
        if channel_key not in coverage:
            raise ValueError(
                f"LANE_WRITER_PRIVILEGE_COVERAGE missing channel probe {channel_key}"
            )

    allowed = frozenset(
        key
        for key, tag in coverage.items()
        if tag.startswith("allowed-because-")
    )
    if allowed != LANE_WRITER_ALLOWED_RUNTIME:
        raise ValueError(
            f"allowed pairs must equal LANE_WRITER_ALLOWED_RUNTIME; "
            f"got={sorted(allowed)} expected={sorted(LANE_WRITER_ALLOWED_RUNTIME)}"
        )

    # Column-form invariant for denied relation privileges.
    for (kind, name, priv), tag in coverage.items():
        if kind not in {"base_table", "view", "binding_table"}:
            continue
        if tag.startswith("allowed-because-"):
            continue
        if priv in PG_COLUMN_PRIVILEGES:
            if "relation" not in tag or "column" not in tag:
                raise ValueError(
                    f"{(kind, name, priv)} has a PG column form; tag must claim "
                    f"relation+column, got {tag!r}"
                )
        else:
            if "column" in tag:
                raise ValueError(
                    f"{(kind, name, priv)} has no PG column form; tag must not "
                    f"claim column, got {tag!r}"
                )


_validate_lane_writer_privilege_coverage()


# Inbox-thread bound: hung/dead-idle store must surface as LaneStoreUnreachable.
_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_MS = 5000
_KEEPALIVES = 1
_KEEPALIVES_IDLE_S = 10
_KEEPALIVES_INTERVAL_S = 5
_KEEPALIVES_COUNT = 3
_TCP_USER_TIMEOUT_MS = 15000


class LaneStoreUnreachable(RuntimeError):
    """Lane writer store is unreachable or fails closed readiness."""


class PsycopgLaneWriter:
    """Daemon-scoped caller of the three bound lease-lane functions."""

    def __init__(
        self,
        dsn: str,
        *,
        expected_role: str,
        expected_consumer_id: str,
        expected_lane: str,
        schema: str = "public",
        connect: Callable = psycopg.connect,
    ) -> None:
        if expected_lane not in _VALID_LANES:
            raise ValueError(
                f"expected_lane must be gated|exempt, got {expected_lane!r}"
            )
        if not expected_role:
            raise ValueError("expected_role must be nonblank")
        if not expected_consumer_id:
            raise ValueError("expected_consumer_id must be nonblank")
        self._dsn = dsn
        self.expected_role = expected_role
        self.expected_consumer_id = expected_consumer_id
        self.expected_lane = expected_lane
        self.schema = schema
        self._connect_fn = connect
        self._conn: Any = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect_fn(
                self._dsn,
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=_CONNECT_TIMEOUT_S,
                keepalives=_KEEPALIVES,
                keepalives_idle=_KEEPALIVES_IDLE_S,
                keepalives_interval=_KEEPALIVES_INTERVAL_S,
                keepalives_count=_KEEPALIVES_COUNT,
                tcp_user_timeout=_TCP_USER_TIMEOUT_MS,
                options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
            )
        return self._conn

    def _discard(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._discard()

    def _run(self, statement, params=(), *, fetch: str = "one"):
        with self._lock:
            try:
                cur = self._connect().execute(statement, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
            except psycopg.Error as exc:
                self._discard()
                raise LaneStoreUnreachable(str(exc)) from exc

    def arm(self, lease_id: str) -> dict[str, Any]:
        row = self._run(
            sql.SQL("SELECT * FROM {}(%s)").format(
                sql.Identifier(self.schema, "arm_lease_lane")
            ),
            (lease_id,),
            fetch="one",
        )
        if row is None:
            raise LaneStoreUnreachable("arm_lease_lane returned no row")
        return dict(row)

    def retire(self, lease_id: str) -> bool:
        row = self._run(
            sql.SQL("SELECT {}(%s) AS retire_lease_lane").format(
                sql.Identifier(self.schema, "retire_lease_lane")
            ),
            (lease_id,),
            fetch="one",
        )
        if row is None:
            raise LaneStoreUnreachable("retire_lease_lane returned no row")
        value = row["retire_lease_lane"] if isinstance(row, dict) else row[0]
        return bool(value)

    def rows(self) -> list[dict[str, Any]]:
        result = self._run(
            sql.SQL("SELECT * FROM {}()").format(
                sql.Identifier(self.schema, "list_lease_lanes")
            ),
            (),
            fetch="all",
        )
        return [dict(r) for r in (result or [])]

    @staticmethod
    def _quote_ident(ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def _relname(self, name: str) -> str:
        return f"{self._quote_ident(self.schema)}.{self._quote_ident(name)}"

    def _fnname(self, sig: str) -> str:
        # sig is like "arm_lease_lane(text)"; qualify the bare name.
        bare = sig.split("(", 1)[0]
        args = sig[len(bare) :]
        return f"{self._quote_ident(self.schema)}.{self._quote_ident(bare)}{args}"

    def assert_ready(self) -> None:
        """Prove identity, binding, three EXECUTEs, and no denied privilege arm.

        Failures route through ``LaneStoreUnreachable`` so startup refuses
        registration.
        """
        with self._lock:
            try:
                conn = self._connect()
                row = conn.execute("SELECT current_user").fetchone()
                if isinstance(row, dict):
                    current = row.get("current_user")
                    if current is None and len(row) == 1:
                        current = next(iter(row.values()))
                else:
                    current = row[0]
                if current != self.expected_role:
                    raise LaneStoreUnreachable(
                        f"expected role {self.expected_role!r}, "
                        f"current_user={current!r}"
                    )

                # Isolation channels first (membership / ownership) so those
                # kills name the structural path rather than a downstream ACL.
                memberships = conn.execute(
                    "SELECT g.rolname FROM pg_catalog.pg_auth_members m "
                    "JOIN pg_catalog.pg_roles r ON r.oid = m.member "
                    "JOIN pg_catalog.pg_roles g ON g.oid = m.roleid "
                    "WHERE r.rolname = current_user"
                ).fetchall()
                if memberships:
                    names = [
                        m["rolname"] if isinstance(m, dict) else m[0]
                        for m in memberships
                    ]
                    raise LaneStoreUnreachable(
                        f"lane writer is a member of {names}; membership "
                        f"authorises SET ROLE"
                    )

                owned_rels = conn.execute(
                    "SELECT c.relname FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_roles r ON r.oid = c.relowner "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE r.rolname = current_user AND n.nspname = %s "
                    "AND c.relname = ANY(%s)",
                    (self.schema, list(LANE_WRITER_RELATIONS)),
                ).fetchall()
                if owned_rels:
                    names = [
                        r["relname"] if isinstance(r, dict) else r[0]
                        for r in owned_rels
                    ]
                    raise LaneStoreUnreachable(
                        f"lane writer owns relation(s) {names}"
                    )

                owned_fns = conn.execute(
                    "SELECT p.proname FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_roles r ON r.oid = p.proowner "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE r.rolname = current_user AND n.nspname = %s "
                    "AND p.proname = ANY(%s)",
                    (
                        self.schema,
                        [
                            "arm_lease_lane",
                            "retire_lease_lane",
                            "list_lease_lanes",
                        ],
                    ),
                ).fetchall()
                if owned_fns:
                    names = [
                        f["proname"] if isinstance(f, dict) else f[0]
                        for f in owned_fns
                    ]
                    raise LaneStoreUnreachable(
                        f"lane writer owns function(s) {names}"
                    )

                # Positive EXECUTE arms derived from allowed coverage.
                for kind, name, priv in sorted(LANE_WRITER_ALLOWED_RUNTIME):
                    if kind != "FUNCTION":
                        continue
                    fn = self._fnname(name)
                    has = conn.execute(
                        "SELECT has_function_privilege(%s, %s) "
                        "AS has_function_privilege",
                        (fn, priv),
                    ).fetchone()
                    ok = (
                        has["has_function_privilege"]
                        if isinstance(has, dict)
                        else has[0]
                    )
                    if not ok:
                        raise LaneStoreUnreachable(
                            f"missing {priv} privilege on function {name}"
                        )

                # Denied relation / column privileges from coverage.
                for (kind, name, priv), tag in sorted(
                    LANE_WRITER_PRIVILEGE_COVERAGE.items()
                ):
                    if kind not in {"base_table", "view", "binding_table"}:
                        continue
                    if not tag.startswith("probed-denied-by-"):
                        continue
                    rel = self._relname(name)
                    if "relation" in tag:
                        has = conn.execute(
                            "SELECT has_table_privilege(%s, %s) "
                            "AS has_table_privilege",
                            (rel, priv),
                        ).fetchone()
                        flag = (
                            has["has_table_privilege"]
                            if isinstance(has, dict)
                            else has[0]
                        )
                        if flag:
                            raise LaneStoreUnreachable(
                                f"forbidden privilege {priv} on {kind} {name}"
                            )
                    if "column" in tag and priv in PG_COLUMN_PRIVILEGES:
                        has = conn.execute(
                            "SELECT has_any_column_privilege(%s, %s) "
                            "AS has_any_column_privilege",
                            (rel, priv),
                        ).fetchone()
                        flag = (
                            has["has_any_column_privilege"]
                            if isinstance(has, dict)
                            else has[0]
                        )
                        if flag:
                            raise LaneStoreUnreachable(
                                f"forbidden column privilege {priv} on "
                                f"{kind} {name}"
                            )

                # Binding-safe function probe: arm+retire a throwaway lease so
                # session_user-bound consumer_id/lane are proved through the
                # SECURITY DEFINER path (runtime has no SELECT on the binding
                # table — that is itself a denied coverage arm).
                probe_id = f"__lane_ready__{uuid.uuid4().hex}"
                arm_row = conn.execute(
                    sql.SQL("SELECT * FROM {}(%s)").format(
                        sql.Identifier(self.schema, "arm_lease_lane")
                    ),
                    (probe_id,),
                ).fetchone()
                if arm_row is None:
                    raise LaneStoreUnreachable(
                        f"binding probe arm returned no row for "
                        f"session_user={current!r}"
                    )
                if isinstance(arm_row, dict):
                    bound_consumer = arm_row["armed_by"]
                    bound_lane = arm_row["lane"]
                else:
                    # lease_id, lane, armed_by, armed_at
                    bound_lane = arm_row[1]
                    bound_consumer = arm_row[2]
                try:
                    if bound_consumer != self.expected_consumer_id:
                        raise LaneStoreUnreachable(
                            f"binding consumer_id mismatch: "
                            f"expected {self.expected_consumer_id!r}, "
                            f"got {bound_consumer!r}"
                        )
                    if bound_lane != self.expected_lane:
                        raise LaneStoreUnreachable(
                            f"binding lane mismatch: "
                            f"expected {self.expected_lane!r}, "
                            f"got {bound_lane!r}"
                        )
                finally:
                    conn.execute(
                        sql.SQL("SELECT {}(%s)").format(
                            sql.Identifier(self.schema, "retire_lease_lane")
                        ),
                        (probe_id,),
                    )

                # List probe (empty ok).
                conn.execute(
                    sql.SQL("SELECT 1 FROM {}() LIMIT 0").format(
                        sql.Identifier(self.schema, "list_lease_lanes")
                    )
                )
            except LaneStoreUnreachable:
                self._discard()
                raise
            except psycopg.Error as exc:
                self._discard()
                raise LaneStoreUnreachable(str(exc)) from exc
            except Exception as exc:
                self._discard()
                raise LaneStoreUnreachable(str(exc)) from exc
