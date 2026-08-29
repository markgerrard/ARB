"""PostgreSQL-backed claim resolver for the bus-side gate (Slice 1c).

Owns one dedicated, autocommit `arb_gate_reader` connection and maps the three
Slice 1b views (`claim_admissibility_v`, `seat_posture_v`, `lease_lane_v`)
straight onto Slice 1a's resolver contract. No Python recomputation of
admissibility predicates; missing rows fail closed as documented in the design
spec; present-row contract violations become ``StoreUnreachable``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent_redis_bridge import claim_gate
from arb_memory.mcp.grants import GATE_READER_ROLE, GATE_RELATIONS

# Relation inventory is one source (grants.GATE_RELATIONS). Views end in _v;
# everything else is a base table the reader must not write. Addition of a
# Slice 1d gate table to GATE_RELATIONS automatically enters readiness probes.
GATE_VIEWS: tuple[str, ...] = tuple(r for r in GATE_RELATIONS if r.endswith("_v"))
BASE_TABLES: tuple[str, ...] = tuple(
    r for r in GATE_RELATIONS if not r.endswith("_v")
)

# ---------------------------------------------------------------------------
# Privilege coverage — DATA, not hand-maintained probe lists.
#
# Prior instances of "a security predicate with a sub-clause whose deletion no
# test detects" all shared one root: probe tuples (then free-form coverage tags)
# edited by hand without a closed machine vocabulary. The map below is the
# classification; the LIVE artefact is the parametrized kill in
# tests/arb_memory/test_claim_resolver.py that GRANTs each probed pair and
# requires assert_ready() to refuse. Tags are closed at import (KNOWN_TAGS);
# the exempt set is pinned to a literal (EXEMPT_COVERAGE_PAIRS).
# ---------------------------------------------------------------------------

# PostgreSQL 17 GRANT ON TABLE verbs (https://www.postgresql.org/docs/17/sql-grant.html).
# has_table_privilege accepts exactly these for ordinary tables/views. The live
# test test_pg_table_privileges_matches_server_aclexplode anchors this set to
# the connected server (not only to a second checked-in literal).
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

# Privileges that have a column form in PostgreSQL (GRANT priv (col) ON …).
# DELETE/TRUNCATE/TRIGGER/MAINTAIN are relation-only.
PG_COLUMN_PRIVILEGES: frozenset[str] = frozenset(
    {"SELECT", "INSERT", "UPDATE", "REFERENCES"}
)

RELATION_KINDS: tuple[str, ...] = ("view", "base_table")

# Closed tag vocabulary. A misspelled or free-form tag refuses module load —
# the same failure mode as a missing (kind, priv) pair. Free-form
# exempt-because-<prose> is not accepted; every exemption string is enumerated.
KNOWN_TAGS: frozenset[str] = frozenset(
    {
        "probed-by-view-select-required",
        "probed-by-view-relation",
        "probed-by-view-relation+column",
        "probed-by-base-relation",
        "probed-by-base-relation+column",
        "exempt-because-select-alone-is-not-a-mint-or-escalation-path",
    }
)

# Pinned exempt set. Retagging a probed pair to any exempt tag without also
# editing this literal is an import-time failure (or a unit-test RED).
EXEMPT_COVERAGE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("base_table", "SELECT"),
    }
)

# Column privileges exist only for SELECT/INSERT/UPDATE/REFERENCES (PG docs);
# DELETE/TRUNCATE/TRIGGER/MAINTAIN have no column form and are relation-only.
PRIVILEGE_COVERAGE: dict[tuple[str, str], str] = {
    ("view", "SELECT"): "probed-by-view-select-required",
    ("view", "INSERT"): "probed-by-view-relation+column",
    ("view", "UPDATE"): "probed-by-view-relation+column",
    ("view", "DELETE"): "probed-by-view-relation",
    ("view", "TRUNCATE"): "probed-by-view-relation",
    ("view", "REFERENCES"): "probed-by-view-relation+column",
    ("view", "TRIGGER"): "probed-by-view-relation",
    ("view", "MAINTAIN"): "probed-by-view-relation",
    # SELECT on base tables is revoked by apply_gate_reader_grants but is not a
    # mint or escalation path for the gate's admission decision (views remain
    # the authority). Named exemption — not a silent hole.
    ("base_table", "SELECT"): (
        "exempt-because-select-alone-is-not-a-mint-or-escalation-path"
    ),
    ("base_table", "INSERT"): "probed-by-base-relation+column",
    ("base_table", "UPDATE"): "probed-by-base-relation+column",
    ("base_table", "DELETE"): "probed-by-base-relation",
    ("base_table", "TRUNCATE"): "probed-by-base-relation",
    ("base_table", "REFERENCES"): "probed-by-base-relation+column",
    ("base_table", "TRIGGER"): "probed-by-base-relation",
    ("base_table", "MAINTAIN"): "probed-by-base-relation",
}


def _validate_privilege_coverage(
    coverage: dict[tuple[str, str], str] | None = None,
) -> None:
    """Closedness of a coverage map. Fail loud, not green-by-skip.

    ``coverage`` defaults to the module global ``PRIVILEGE_COVERAGE`` so the
    import-time call stays a one-liner. Unit tests pass a deliberate bad map
    to kill each branch without mutating the module global (eighth-instance
    guard: the validator body itself is exercised, not only the good table).
    """
    coverage = PRIVILEGE_COVERAGE if coverage is None else coverage
    expected = {(kind, priv) for kind in RELATION_KINDS for priv in PG_TABLE_PRIVILEGES}
    keys = set(coverage)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        raise ValueError(
            f"PRIVILEGE_COVERAGE must be the full cartesian product of "
            f"RELATION_KINDS × PG_TABLE_PRIVILEGES; missing={missing} extra={extra}"
        )
    unknown = sorted(
        {tag for tag in coverage.values() if tag not in KNOWN_TAGS}
    )
    if unknown:
        raise ValueError(
            f"PRIVILEGE_COVERAGE has tags outside KNOWN_TAGS (closed vocabulary): "
            f"{unknown}"
        )
    actual_exempt = frozenset(
        pair
        for pair, tag in coverage.items()
        if tag.startswith("exempt-because-")
    )
    if actual_exempt != EXEMPT_COVERAGE_PAIRS:
        raise ValueError(
            f"exempt pairs must equal EXEMPT_COVERAGE_PAIRS literal; "
            f"got={sorted(actual_exempt)} expected={sorted(EXEMPT_COVERAGE_PAIRS)}"
        )
    for (kind, priv), tag in coverage.items():
        if tag.startswith("exempt-because-"):
            continue
        if not tag.startswith("probed-by-"):
            raise ValueError(f"tag for {(kind, priv)} is neither probed nor exempt: {tag!r}")
        # Column-form invariant: non-SELECT privileges that have a PG column form
        # must claim both relation and column channels; relation-only PG privs
        # must not claim column.
        if priv == "SELECT":
            if tag != "probed-by-view-select-required":
                raise ValueError(
                    f"SELECT coverage for {kind} must be view-select-required or exempt, "
                    f"got {tag!r}"
                )
            continue
        if priv in PG_COLUMN_PRIVILEGES:
            if "relation" not in tag or "column" not in tag:
                raise ValueError(
                    f"{(kind, priv)} has a PG column form; tag must claim "
                    f"relation+column, got {tag!r}"
                )
        else:
            if "column" in tag:
                raise ValueError(
                    f"{(kind, priv)} has no PG column form; tag must not claim "
                    f"column, got {tag!r}"
                )
            if "relation" not in tag:
                raise ValueError(
                    f"{(kind, priv)} must claim relation channel, got {tag!r}"
                )


_validate_privilege_coverage()


def _privs_probed_via(kind: str, channel: str) -> tuple[str, ...]:
    """Privileges for ``kind`` whose coverage tag includes ``channel``.

    ``channel`` is one of ``"relation"``, ``"column"``, ``"select-required"``.
    Relation+column tags contain both substrings and appear in both loops.
    Only ``probed-by-*`` tags participate (exempt tags are skipped).
    """
    out: list[str] = []
    for priv in PG_TABLE_PRIVILEGES:
        tag = PRIVILEGE_COVERAGE[(kind, priv)]
        if not tag.startswith("probed-by-"):
            continue
        if channel == "select-required" and tag.endswith("select-required"):
            out.append(priv)
        elif channel in ("relation", "column") and channel in tag:
            out.append(priv)
    return tuple(out)


# Derived probe loops — never hand-edit these; edit PRIVILEGE_COVERAGE.
_VIEW_SELECT_REQUIRED = _privs_probed_via("view", "select-required")
_VIEW_RELATION_NON_SELECT = _privs_probed_via("view", "relation")
_VIEW_COLUMN_NON_SELECT = _privs_probed_via("view", "column")
_BASE_TABLE_WRITES = _privs_probed_via("base_table", "relation")
_BASE_TABLE_COLUMN_WRITES = _privs_probed_via("base_table", "column")

_VALID_LANES = frozenset({"gated", "exempt"})
_VALID_PROVENANCE = frozenset({"none", "degraded", "wire"})

# Inbox-thread bound: a hung or dead-idle store must surface as store_unreachable,
# not stall consumer_heartbeat refresh (VIS-2 class; claim_resolver holds _lock
# across I/O).
#
# Two distinct failure modes, two mechanisms:
#   * connect_timeout + statement_timeout — server answers TCP but is slow /
#     connect stalls. statement_timeout is SERVER-side: it cannot bound a socket
#     that looks ESTABLISHED while recv() blocks forever (failover, NAT drop,
#     partition). That was the round-1 residual on P1-3.
#   * libpq keepalives + tcp_user_timeout — CLIENT-side deadline for dead-idle
#     connections (the case the server never sees). Precedents: visibility.py
#     socket_timeout=5; redis clients in bridge.py.
#
# Darwin caveat: tcp_user_timeout is a no-op on macOS libpq builds that lack
# TCP_USER_TIMEOUT support. On darwin, keepalives alone bound dead-idle
# (~ keepalives_idle + keepalives_interval * keepalives_count ≈ 25s). A
# partition with a query already in flight remains unbounded on darwin; the
# 15s client-side hard deadline holds on Linux where TCP_USER_TIMEOUT is wired.
_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_MS = 5000
_KEEPALIVES = 1
_KEEPALIVES_IDLE_S = 10
_KEEPALIVES_INTERVAL_S = 5
_KEEPALIVES_COUNT = 3
_TCP_USER_TIMEOUT_MS = 15000


class PsycopgClaimResolver:
    """Daemon-scoped reader for the three gate views.

    Connection access is serialised under ``_lock`` so a later result-delivery
    caller can share the same resolver without racing the inbox thread.
    """

    def __init__(
        self,
        dsn: str,
        *,
        expected_role: str = GATE_READER_ROLE,
        schema: str = "public",
        connect: Callable = psycopg.connect,
    ) -> None:
        self._dsn = dsn
        self.expected_role = expected_role
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

    def _read(self, statement, params=(), *, project):
        with self._lock:
            try:
                row = self._connect().execute(statement, params).fetchone()
            except psycopg.Error as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(str(exc)) from exc
            if row is None:
                return None
            try:
                return project(row)
            except (KeyError, TypeError, ValueError) as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(
                    f"authority row violates its published contract: {exc}"
                ) from exc

    @staticmethod
    def _validated_posture(row: dict) -> bool:
        if "requires_claim_ref" not in row:
            raise KeyError("requires_claim_ref")
        value = row["requires_claim_ref"]
        if type(value) is not bool:
            raise TypeError(
                f"requires_claim_ref must be bool, got {type(value).__name__}"
            )
        return value

    @staticmethod
    def _validated_lane(row: dict) -> str:
        if "lane" not in row:
            raise KeyError("lane")
        lane = row["lane"]
        if lane not in _VALID_LANES:
            raise ValueError(f"lane must be gated|exempt, got {lane!r}")
        return lane

    @staticmethod
    def _validated_claim_facts(row: dict) -> claim_gate.ClaimFacts:
        for key in ("confirmed_now", "attested", "decorrelation_provenance"):
            if key not in row:
                raise KeyError(key)
        confirmed = row["confirmed_now"]
        attested = row["attested"]
        provenance = row["decorrelation_provenance"]
        if type(confirmed) is not bool:
            raise TypeError(
                f"confirmed_now must be bool, got {type(confirmed).__name__}"
            )
        if type(attested) is not bool:
            raise TypeError(
                f"attested must be bool, got {type(attested).__name__}"
            )
        if provenance not in _VALID_PROVENANCE:
            raise ValueError(
                f"decorrelation_provenance must be none|degraded|wire, "
                f"got {provenance!r}"
            )
        return claim_gate.ClaimFacts(
            confirmed_now=confirmed,
            attested=attested,
            decorrelation_provenance=provenance,
        )

    def seat_requires_claim_ref(self, seat_id: str) -> bool:
        value = self._read(
            sql.SQL("SELECT requires_claim_ref FROM {} WHERE seat_id = %s").format(
                sql.Identifier(self.schema, "seat_posture_v")
            ),
            (seat_id,),
            project=self._validated_posture,
        )
        return True if value is None else value

    def lease_lane(self, lease_id: str) -> str | None:
        return self._read(
            sql.SQL("SELECT lane FROM {} WHERE lease_id = %s").format(
                sql.Identifier(self.schema, "lease_lane_v")
            ),
            (lease_id,),
            project=self._validated_lane,
        )

    def claim(self, claim_ref: str) -> claim_gate.ClaimFacts | None:
        return self._read(
            sql.SQL(
                "SELECT confirmed_now, attested, decorrelation_provenance "
                "FROM {} WHERE claim_id = %s"
            ).format(sql.Identifier(self.schema, "claim_admissibility_v")),
            (claim_ref,),
            project=self._validated_claim_facts,
        )

    @staticmethod
    def _quote_ident(ident: str) -> str:
        """Double-quote a PostgreSQL identifier (escape embedded quotes)."""
        return '"' + ident.replace('"', '""') + '"'

    def _relname(self, name: str) -> str:
        """Qualified relation name for privilege probes (schema-aware).

        Always quote both parts so a mixed-case schema matches the relation
        that ``sql.Identifier`` selects on the data path. An unquoted name is
        folded to lowercase by PostgreSQL and can evaluate capability against
        a different relation than the one read.
        """
        return f"{self._quote_ident(self.schema)}.{self._quote_ident(name)}"

    def assert_ready(self) -> None:
        """Prove identity + SELECT + no non-SELECT view privilege + no base writes.

        Failures route through ``StoreUnreachable`` so startup refuses registration.
        """
        with self._lock:
            try:
                conn = self._connect()
                row = conn.execute("SELECT current_user").fetchone()
                # dict_row or tuple depending on connect factory
                if isinstance(row, dict):
                    current = row.get("current_user")
                    if current is None and len(row) == 1:
                        current = next(iter(row.values()))
                else:
                    current = row[0]
                if current != self.expected_role:
                    raise claim_gate.StoreUnreachable(
                        f"expected role {self.expected_role!r}, current_user={current!r}"
                    )

                for view in GATE_VIEWS:
                    view_ident = sql.Identifier(self.schema, view)
                    rel = self._relname(view)
                    # Positive SELECT arm is derived from PRIVILEGE_COVERAGE
                    # (probed-by-view-select-required). Checked before LIMIT 0 so
                    # a missing grant surfaces the named readiness message rather
                    # than a generic permission-denied from the probe query.
                    for priv in _VIEW_SELECT_REQUIRED:
                        has_select = conn.execute(
                            "SELECT has_table_privilege(%s, %s) AS has_table_privilege",
                            (rel, priv),
                        ).fetchone()
                        select_ok = (
                            has_select["has_table_privilege"]
                            if isinstance(has_select, dict)
                            else has_select[0]
                        )
                        if not select_ok:
                            raise claim_gate.StoreUnreachable(
                                f"missing {priv} privilege on view {view}"
                            )

                    # Prove the view is queryable (SELECT of published columns, LIMIT 0).
                    if view == "claim_admissibility_v":
                        cols = sql.SQL(
                            "confirmed_now, attested, decorrelation_provenance"
                        )
                    elif view == "seat_posture_v":
                        cols = sql.SQL("requires_claim_ref")
                    else:
                        cols = sql.SQL("lane")
                    conn.execute(
                        sql.SQL("SELECT {} FROM {} LIMIT 0").format(cols, view_ident)
                    )

                    for priv in _VIEW_RELATION_NON_SELECT:
                        has = conn.execute(
                            "SELECT has_table_privilege(%s, %s) AS has_table_privilege",
                            (rel, priv),
                        ).fetchone()
                        flag = (
                            has["has_table_privilege"]
                            if isinstance(has, dict)
                            else has[0]
                        )
                        if flag:
                            raise claim_gate.StoreUnreachable(
                                f"non-SELECT privilege {priv} on view {view}"
                            )

                    for priv in _VIEW_COLUMN_NON_SELECT:
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
                            raise claim_gate.StoreUnreachable(
                                f"non-SELECT column privilege {priv} on view {view}"
                            )

                for table in BASE_TABLES:
                    rel = self._relname(table)
                    for priv in _BASE_TABLE_WRITES:
                        has = conn.execute(
                            "SELECT has_table_privilege(%s, %s) AS has_table_privilege",
                            (rel, priv),
                        ).fetchone()
                        flag = (
                            has["has_table_privilege"]
                            if isinstance(has, dict)
                            else has[0]
                        )
                        if flag:
                            raise claim_gate.StoreUnreachable(
                                f"write privilege {priv} on base table {table}"
                            )
                    # Column-level writes are invisible to has_table_privilege;
                    # probe them the same way views are probed (P1-1).
                    for priv in _BASE_TABLE_COLUMN_WRITES:
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
                            raise claim_gate.StoreUnreachable(
                                f"write column privilege {priv} on base table {table}"
                            )
            except claim_gate.StoreUnreachable:
                self._discard()
                raise
            except psycopg.Error as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(str(exc)) from exc
            except Exception as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(str(exc)) from exc
