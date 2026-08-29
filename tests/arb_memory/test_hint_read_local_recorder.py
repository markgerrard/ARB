"""S3: local hint_read recorder + D-2 query columns (TDD).

Calls `_record_local_read` / `_query_columns` directly — call-site wiring into
memory_search is a later increment.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from unittest import mock

import psycopg
import pytest

from arb_memory.mcp import read_tools
from arb_memory.mcp.read_tools import _query_columns, _record_local_read

pytest_plugins = ("tests.arb_memory.conftest",)


def _hit(hint_id: int, *, vector_distance=0.1, lexical_rank=0.5, withheld=False):
    """store.retrieve shape: metrics live under 'hint'; withheld is outer (H-02)."""
    return {
        "hint": {
            "id": hint_id,
            "vector_distance": vector_distance,
            "lexical_rank": lexical_rank,
            "text": f"hint-{hint_id}",
        },
        "artefact": None,
        "repo_pointer": None,
        "withheld": withheld,
    }


def _parent_count(conn):
    return conn.execute("SELECT count(*) FROM hint_read").fetchone()[0]


def _hit_rows(conn, read_id=None):
    if read_id is None:
        return conn.execute(
            "SELECT rank, hint_id, withheld, vector_distance, lexical_rank "
            "FROM hint_read_hit ORDER BY rank"
        ).fetchall()
    return conn.execute(
        "SELECT rank, hint_id, withheld, vector_distance, lexical_rank "
        "FROM hint_read_hit WHERE read_id = %s ORDER BY rank",
        (read_id,),
    ).fetchall()


def test_record_local_read_happy_path_parent_and_ranked_hits(scratch):
    hits = [_hit(101), _hit(202), _hit(303)]
    _record_local_read(
        scratch,
        door="local",
        outcome="ok",
        hit_count=3,
        query_text="find the thing",
        query_truncated=False,
        k=8,
        hits=hits,
    )

    parents = scratch.execute(
        "SELECT door, outcome, k, hit_count, query_truncated FROM hint_read"
    ).fetchall()
    assert len(parents) == 1
    assert parents[0] == ("local", "ok", 8, 3, False)

    rows = _hit_rows(scratch)
    assert [r[0] for r in rows] == [1, 2, 3]
    assert [r[1] for r in rows] == [101, 202, 303]


def test_record_local_read_requires_autocommit(scratch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    conn = psycopg.connect(dsn)
    try:
        conn.autocommit = False
        conn.execute(f'SET search_path TO "{schema}", public')
        with pytest.raises(RuntimeError, match="autocommit") as excinfo:
            _record_local_read(
                conn,
                door="local",
                outcome="ok",
                hit_count=0,
                query_text="q",
                query_truncated=False,
                k=1,
                hits=(),
            )
        assert "autocommit" in str(excinfo.value).lower()
        assert _parent_count(scratch) == 0
    finally:
        conn.rollback()
        conn.close()


def test_record_local_read_rejects_autocommit_connection_already_in_a_transaction(scratch):
    """K-04/J-12: `autocommit` is a PROXY; the real clause-1 precondition is IDLE.

    `conn.transaction()` issues a real COMMIT only as the OUTERMOST block. On a connection
    that is autocommit **and** already INTRANS it degrades to a SAVEPOINT, whose release
    commits nothing — so the old guard admitted a connection on which the recorder reported
    success and wrote nothing durable. Verified against PostgreSQL 17.7: guard passes,
    status INTRANS, row count after the block = 0.

    Guard on the status itself, so a connection that cannot COMMIT is refused at the door and
    `memory_search`'s recording guard turns it into a logged miss (G-01 / H-05).
    """
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute("BEGIN")  # autocommit stays True; the SERVER is now mid-transaction
        # Premises: without these the test would pass for the wrong reason.
        assert conn.autocommit is True, "premise: the autocommit-only guard still passes"
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS

        with pytest.raises(RuntimeError) as excinfo:
            _record_local_read(
                conn,
                door="local",
                outcome="ok",
                hit_count=1,
                query_text="q",
                query_truncated=False,
                k=1,
                hits=[_hit(7)],
            )
        assert "idle" in str(excinfo.value).lower()
    finally:
        conn.rollback()
        conn.close()

    # Parity with test_record_local_read_requires_autocommit: the guard fired before any
    # INSERT, so no parent row exists on an independent connection.
    assert _parent_count(scratch) == 0


def test_record_local_read_rejects_connection_with_unreadable_status():
    """Q8 follow-up: an UNKNOWN precondition is not a satisfied one — this guard fails CLOSED.

    The `conn_factory` seam admits non-psycopg objects (`ReadMemoryTools.__init__` stores the
    caller's factory verbatim). One that is `autocommit=True` but exposes no `.info` yields
    `status is None`, which says "cannot tell whether conn.transaction() would COMMIT" — not
    "it would". An earlier revision skipped the check in that case; that reintroduced the exact
    assume-instead-of-assert defect the guard exists to remove, and a probe confirmed such a
    connection ran the recorder to completion.

    Deliberately DB-free: it pins the guard, not the write path, so it runs without Postgres.
    """

    class StatusBlindConn:
        autocommit = True  # passes guard 1, so guard 2 is what decides

        def __init__(self):
            self.calls = []

        def transaction(self):
            self.calls.append("transaction")
            raise AssertionError("guard must reject before opening a transaction")

        def execute(self, *args, **kwargs):
            self.calls.append("execute")
            raise AssertionError("guard must reject before executing")

    conn = StatusBlindConn()
    # Premises: without these the test could pass for the wrong reason.
    assert conn.autocommit is True, "premise: guard 1 passes, so guard 2 is under test"
    assert not hasattr(conn, "info"), "premise: the status is genuinely unreadable"

    with pytest.raises(RuntimeError) as excinfo:
        _record_local_read(
            conn,
            door="local",
            outcome="ok",
            hit_count=0,
            query_text="q",
            query_truncated=False,
            k=1,
            hits=(),
        )
    assert "idle" in str(excinfo.value).lower()
    assert conn.calls == [], "guard must reject before touching the connection"


def test_record_local_read_atomicity_rolls_back_parent_on_second_hit_failure(scratch):
    """G-06: failure on the second hint_read_hit INSERT must roll back the parent too."""
    hits = [_hit(11), _hit(22)]
    real_execute = scratch.execute
    hit_inserts = {"n": 0}

    def execute_spy(query, params=None, **kwargs):
        q = query if isinstance(query, str) else str(query)
        if "INSERT INTO hint_read_hit" in q:
            hit_inserts["n"] += 1
            if hit_inserts["n"] == 2:
                raise psycopg.errors.RaiseException("forced second hit failure")
        if params is None:
            return real_execute(query, **kwargs)
        return real_execute(query, params, **kwargs)

    with mock.patch.object(scratch, "execute", side_effect=execute_spy):
        with pytest.raises(psycopg.errors.RaiseException, match="forced second hit failure"):
            _record_local_read(
                scratch,
                door="local",
                outcome="ok",
                hit_count=2,
                query_text="atomic",
                query_truncated=False,
                k=2,
                hits=hits,
            )

    assert hit_inserts["n"] == 2
    assert _parent_count(scratch) == 0
    assert scratch.execute("SELECT count(*) FROM hint_read_hit").fetchone()[0] == 0


def test_record_local_read_door_follows_argument(scratch):
    """H-07: door is a bound parameter, not hardcoded 'local'."""
    for door in ("local", "bus"):
        _record_local_read(
            scratch,
            door=door,
            outcome="ok",
            hit_count=0,
            query_text=f"door-{door}",
            query_truncated=False,
            k=1,
            hits=(),
        )
    doors = {
        r[0]
        for r in scratch.execute("SELECT door FROM hint_read").fetchall()
    }
    assert doors == {"local", "bus"}


def test_record_local_read_reads_nested_hint_fields(scratch):
    """H-02: hint_id / vector_distance / lexical_rank from hit['hint']; withheld from outer."""
    hits = [
        _hit(42, vector_distance=0.25, lexical_rank=0.9, withheld=True),
        _hit(99, vector_distance=0.5, lexical_rank=0.1, withheld=False),
    ]
    _record_local_read(
        scratch,
        door="local",
        outcome="ok",
        hit_count=2,
        query_text="nested",
        query_truncated=False,
        k=2,
        hits=hits,
    )
    rows = _hit_rows(scratch)
    assert rows[0][0] == 1
    assert rows[0][1] == 42
    assert rows[0][2] is True
    assert float(rows[0][3]) == pytest.approx(0.25)
    assert float(rows[0][4]) == pytest.approx(0.9)
    assert rows[1][1] == 99
    assert rows[1][2] is False


def test_query_columns_hmac_when_key_set(monkeypatch):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "secret-key")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    query = "capped query text"
    expected = hmac.new(
        b"secret-key", query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    query_hmac, query_text_to_store = _query_columns(query)
    assert query_hmac == expected
    assert query_text_to_store is None


def test_query_columns_null_hmac_when_key_unset_still_records(scratch, monkeypatch):
    """D-2: key unset → query_hmac NULL; recording still succeeds (§7 dominates)."""
    monkeypatch.delenv("ARB_HINT_READ_QUERY_KEY", raising=False)
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    # reset once-flag so this path is free to log
    read_tools._query_key_missing_logged = False

    _record_local_read(
        scratch,
        door="local",
        outcome="ok",
        hit_count=0,
        query_text="no-key-query",
        query_truncated=False,
        k=3,
        hits=(),
    )
    row = scratch.execute(
        "SELECT query_hmac, query_text FROM hint_read"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_query_columns_raw_opt_in_stores_text(scratch, monkeypatch):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "k")
    monkeypatch.setenv("ARB_HINT_READ_QUERY_RAW", "1")
    query = "raw-visible"
    _record_local_read(
        scratch,
        door="local",
        outcome="ok",
        hit_count=0,
        query_text=query,
        query_truncated=True,
        k=1,
        hits=(),
    )
    row = scratch.execute(
        "SELECT query_hmac, query_text, query_truncated FROM hint_read"
    ).fetchone()
    assert row[0] == hmac.new(b"k", query.encode("utf-8"), hashlib.sha256).hexdigest()
    assert row[1] == query
    assert row[2] is True


def test_query_columns_raw_off_stores_null_text(monkeypatch):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "k")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _, text = _query_columns("hidden")
    assert text is None


def test_query_columns_empty_key_same_as_unset(monkeypatch):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    read_tools._query_key_missing_logged = False
    query_hmac, text = _query_columns("q")
    assert query_hmac is None
    assert text is None
