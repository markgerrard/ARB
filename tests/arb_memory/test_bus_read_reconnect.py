"""ReadLoop must survive a reaped Postgres connection.

Production incident 2026-07-28 (arb-prod `deploy-memory-1`, up 7 days, 0 restarts): DO Managed
Postgres reaped the read lane's idle connection. Recall is the LOWEST-traffic lane by design
(arb-memory-architecture.md: "one read per panel, not per seat"), so its connection sits idle
longest and is reaped first — while `WriteLoop` and `FetchConsumer`, which hold the factory and
rebuild, stayed healthy. `MemoryConsumer` handed `ReadLoop` a materialised `conn_factory()`
(bus.py), so it had nothing to reconnect *with*: every subsequent read raised
`OperationalError("the connection is lost")` for seven days.

It was invisible at three layers: `handle_read_request` logged nothing, the error became a reply
envelope, and `memory_query` mapped that to `None` — the documented "fall back to grep" signal, so
every caller read a dead lane as an ordinary cache miss.

These tests fail against the pre-fix code. `test_readloop_refreshes_conn_after_transient_failure`
is the regression proper; `test_memory_consumer_gives_readloop_a_factory` is the structural guard
that would catch a revert to `ReadLoop(redis, conn_factory(), ...)`.
"""

import concurrent.futures
import time

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

import psycopg

from arb_memory import bus
from arb_memory.bus import MemoryConsumer, ReadLoop, WriteLoop, memory_query, memory_write


def _wait_until(fn, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def _reaped(conn_factory):
    """A REAL psycopg connection that has been closed — what a reaped connection actually is.

    A hand-rolled fake is not faithful here: `store.retrieve` calls `register_vector()` before it
    ever issues SQL, and that rejects a non-psycopg object with `TypeError`, which
    `classify_infra_error` rightly does NOT treat as transient. Using a genuinely closed
    connection reproduces the production error class (psycopg `InterfaceError`/`OperationalError`),
    which is the whole point of the test.
    """
    conn = conn_factory()
    conn.close()
    return conn


def test_readloop_refreshes_conn_after_transient_failure(redis_bus, conn_factory, fake_embed):
    """A reaped connection must not wedge the lane: the NEXT read has to succeed.

    Fails pre-fix: ReadLoop holds one connection with no factory, so the second read raises
    the same OperationalError and `memory_query` returns None forever.
    """
    write_loop = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    memory_write(
        redis_bus,
        artefact={"artefact_id": "reconnect.md", "content": "c"},
        hints=[{"text": "reconnect target hint"}],
        prefix=redis_bus.prefix,
    )
    entry_id, intent = write_loop.read_one()
    write_loop.handle_and_ack(entry_id, intent)

    reaped = _reaped(conn_factory)
    handed_out = []

    def factory():
        # First call yields the already-reaped connection (the production state at boot+7d);
        # every later call yields a real one, which is what reconnecting must produce.
        conn = reaped if not handed_out else conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rc")

    # Read 1 — hits the reaped connection. The seat must still be unblocked (a miss, not a hang).
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            memory_query, redis_bus, "reconnect target hint", 1, timeout_s=2.0, prefix=redis_bus.prefix
        )
        _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") >= 1)
        loop.step()
        assert future.result(timeout=2.0) is None, "a failed read must still reply, not hang"

    assert len(handed_out) >= 2, "ReadLoop did not rebuild its connection after a transient failure"

    # Read 2 — on the refreshed connection. This is the assertion the production bug violates.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            memory_query, redis_bus, "reconnect target hint", 1, timeout_s=3.0, prefix=redis_bus.prefix
        )
        _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") >= 2)
        loop.step()
        hits = future.result(timeout=3.0)

    assert hits is not None, "read lane stayed wedged after a transient failure — the 7-day outage"
    assert any("reconnect target hint" in h["hint"]["text"] for h in hits)


def test_readloop_logs_transient_failure(redis_bus, conn_factory, fake_embed, caplog):
    """The outage produced ZERO log lines in 7 days. Silence is the defect."""
    loop = ReadLoop(
        redis_bus,
        lambda: _reaped(conn_factory),
        embed=fake_embed,
        prefix=redis_bus.prefix,
        consumer="rl",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memory_query, redis_bus, "anything", 1, timeout_s=2.0, prefix=redis_bus.prefix)
        _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") >= 1)
        with caplog.at_level("ERROR", logger=bus.__name__):
            loop.step()
        assert future.result(timeout=2.0) is None

    # Assert on the RECORD, not merely that some record exists. `caplog.records` counts records
    # from ANY logger, so one stray WARNING from redis/psycopg inside `loop.step()` would make a
    # bare `assert caplog.records` green forever (round 3, opus5 F5).
    assert any(r.name == bus.__name__ and "read request" in r.getMessage() for r in caplog.records), (
        "a failing read must be logged BY arb_memory.bus; silence is how this hid for 7 days. "
        f"saw: {[(r.name, r.getMessage()[:60]) for r in caplog.records]}"
    )


def test_memory_consumer_gives_readloop_a_factory(redis_bus, conn_factory, fake_embed):
    """Structural guard: MemoryConsumer must hand ReadLoop a CALLABLE, not a connection.

    Fails pre-fix, where the call site was `ReadLoop(redis, conn_factory(), ...)`. This is the
    check that would catch a revert — the bug was one pair of parentheses.
    """
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    assert callable(consumer.read_loop.conn_factory), (
        "ReadLoop was handed a materialised connection; it cannot reconnect when the DB reaps it"
    )


# --- Round-2 remediation: panel-readloop-20260728T055550Z-a1ff27 -------------------
# All four seats returned needs-changes/P1 on the first fix. Three findings, each with a
# test below that FAILS against that fix:
#   asdk-opus5  the lane sits idle-in-transaction, so the real reap raises 25P03
#               (IdleInTransactionSessionTimeout), which is NOT an OperationalError and
#               classifies as "poison" -> _refresh_conn returned early -> still wedged.
#               Verified live on arb-prod: one backend idle-in-transaction for 3h32m,
#               server idle_in_transaction_session_timeout = 1d.
#   agy/grok/codex  the bare-connection back-compat wrap silently no-ops the refresh.
#   grok/codex      the callable() guard is false-green: the wrap makes it callable even
#               on the pre-fix materialised-connection shape.

import psycopg.errors


def test_read_connection_returns_to_idle_between_reads(redis_bus, conn_factory, fake_embed):
    """The lane must not sit INTRANS. This is the ROOT cause, not the reap.

    ReadLoop was the only lane holding an open transaction while idle: WriteLoop uses
    `with conn.transaction()` and returns to IDLE; FetchConsumer opens and closes per entry.
    An INTRANS-idle connection is what the database reaps, and it pins a transaction snapshot
    (VACUUM/bloat exposure on `hints`) even when nothing reaps it.

    Round 2 (P1, opus5/codex/grok): this test was a FALSE-GREEN. `_new_conn` rolled back the
    factory's `SET search_path`, so the read died `UndefinedTable: relation "hints" does not
    exist` -- and because the only assertion was `status == IDLE`, it passed anyway. The
    connection was IDLE because the query FAILED under autocommit, not because a successful
    SELECT returned to IDLE. It now asserts the read actually served hits, which anchors the
    IDLE assertion to a working lane.
    """
    # PRODUCTION-SHAPE connection, built here on purpose. The `conn_factory` fixture sets
    # `conn.autocommit = True` (conftest.py:135), so a connection from it can never be left
    # INTRANS -- this test would pass against the unfixed code and prove nothing. Production
    # `_memory_conn()` (run.py:18-21) is a bare `psycopg.connect(DSN)`: autocommit=False.
    import os

    opened = []

    def prod_shape_factory():
        conn = psycopg.connect(os.environ["ARB_MEMORY_DSN"])  # non-autocommit, like production
        conn.execute(f'SET search_path TO "{_scratch_schema(conn_factory)}", public')
        from pgvector.psycopg import register_vector

        register_vector(conn)
        opened.append(conn)
        return conn

    # A hint the read must actually FIND. Without a real hit to assert on, the IDLE assertion
    # below is unanchored and passes on a lane that cannot serve a single read.
    write_loop = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    memory_write(
        redis_bus,
        artefact={"artefact_id": "idle-between-reads.md", "content": "c"},
        hints=[{"text": "idle between reads target hint"}],
        prefix=redis_bus.prefix,
    )
    entry_id, intent = write_loop.read_one()
    write_loop.handle_and_ack(entry_id, intent)

    # Pin the PREMISE on the FACTORY'S OWN OUTPUT, before ReadLoop touches it. Asserting on
    # `loop.conn` cannot work: the fix's whole purpose is to flip autocommit, so the old guard
    # (`autocommit is False or transaction_status is not None`) was vacuous -- disjunct 1 is
    # always False post-fix, and `transaction_status` is an enum member that is NEVER None
    # (round 2, P2).
    probe = prod_shape_factory()
    assert probe.autocommit is False, "premise: the production-shape factory must be non-autocommit"
    assert probe.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS, (
        "premise: the factory's session setup must leave an OPEN transaction -- that is the state "
        "_new_conn has to resolve without discarding the setup"
    )

    loop = ReadLoop(redis_bus, prod_shape_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="ri")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                memory_query,
                redis_bus,
                "idle between reads target hint",
                1,
                timeout_s=3.0,
                prefix=redis_bus.prefix,
            )
            _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") >= 1)
            loop.step()
            hits = future.result(timeout=3.0)

        assert hits is not None, (
            "the read must SUCCEED, not merely leave the connection IDLE. Round 2: _new_conn "
            "rolled back the factory's SET search_path, so this read raised UndefinedTable while "
            "the IDLE assertion below still passed."
        )
        assert any("idle between reads target hint" in h["hint"]["text"] for h in hits), (
            "the read returned no matching hint -- the lane is querying the wrong schema"
        )

        status = loop.conn.info.transaction_status
        assert status == psycopg.pq.TransactionStatus.IDLE, (
            f"read connection left in {status!r}; an INTRANS-idle connection is what the server "
            "reaps (25P03) and it pins a transaction snapshot indefinitely. Verified live on "
            "arb-prod: one backend idle-in-transaction for 3h32m, xact_age == time since restart."
        )
    finally:
        for c in opened:
            if not c.closed:
                c.close()


def _scratch_schema(conn_factory):
    """The schema the conftest fixtures installed the schema into."""
    c = conn_factory()
    return c.execute("SELECT current_schema()").fetchone()[0]


def test_readloop_rebuilds_after_idle_in_transaction_reap(redis_bus, conn_factory, fake_embed):
    """The reap class that actually happens in production must trigger a rebuild.

    25P03 is NOT a psycopg.OperationalError, so classify_infra_error calls it "poison". The
    first fix returned early on non-transient and never rebuilt. The rebuild predicate must
    key on the connection being unusable, not on the exception's class.

    Round 2 (P2, opus5): this previously handed the DEAD connection to `__init__`, so `self.conn`
    was already closed before `_refresh_conn` ran -- the rebuild was not attributable to the reap
    at all. The lane now starts HEALTHY and is reaped afterwards, which is the production
    sequence.

    SCOPE, corrected in round 3 (codex, P1): this test reaches the rebuild via the `closed` branch
    of `_conn_unusable`, NOT via the status branch -- verified by gutting the status branch and
    watching this test stay green. That is faithful to the real 25P03 sequence (opus5 measured
    `closed=True, status=UNKNOWN` after the failed use, so `closed` genuinely decides it in
    production), but it means this test does NOT pin the status predicate. What it pins is that a
    POISON-classified exception no longer short-circuits the rebuild, which is the exact ca2cf1ff
    regression. The status branch is pinned separately by
    `test_refresh_rebuilds_on_unusable_status_even_when_not_closed` and
    `test_conn_unusable_treats_aborted_transaction_as_unusable`.
    """
    assert not issubclass(psycopg.errors.IdleInTransactionSessionTimeout, psycopg.OperationalError), (
        "premise changed: 25P03 is now an OperationalError, re-check this test's reason to exist"
    )

    handed_out = []

    def factory():
        conn = conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rr")
    assert len(handed_out) == 1 and not loop.conn.closed, "premise: the lane starts HEALTHY"

    # Now reap it the way the server does: the backend is gone by the time the lane next uses it.
    loop.conn.close()
    before = len(handed_out)
    loop._refresh_conn(
        psycopg.errors.IdleInTransactionSessionTimeout("terminating connection due to idle-in-transaction timeout"),
        "0-0",
    )
    assert len(handed_out) == before + 1, (
        "no rebuild after a 25P03 reap -- the lane stays wedged on the exact failure that "
        "took recall down for 7 days"
    )


# --- Round-3 remediation: panel-readloop-r2-20260728T085541Z-ba19b8 -----------------
# 3/4 seats needs-changes/P1 on 98ca6482. The core fix was CONFIRMED working end-to-end
# against a real server-side 25P03 reap, but two production defects composed into a rebuilt
# version of the original outage:
#   opus5   `_new_conn` rolled back a factory-opened transaction, discarding the factory's
#           `SET search_path` (a plain SET inside a transaction block is transactional). The
#           lane then queried the wrong schema; the root-cause test passed on a failed read.
#   agy     `_conn_unusable` recognised only UNKNOWN, missing INERROR -- an aborted transaction
#           raises InFailedSqlTransaction on every further statement, so the lane wedges.
#   agy/    `_new_conn` swallowed an autocommit failure and RETURNED the outage-shaped
#   opus5   connection. That is how INERROR becomes reachable: fail-open installs a
#           non-autocommit lane, the next failed read aborts its transaction, and the
#           unusable-probe declines to rebuild. Permanent wedge, original shape.


def test_new_conn_preserves_factory_session_setup(redis_bus, conn_factory, fake_embed):
    """A factory-opened transaction is COMMITTED, not rolled back.

    Fails against 98ca6482, whose `conn.rollback()` reverted the factory's `SET search_path` to
    `"$user", public`. Verified standalone: after the rollback the lane queried the wrong schema
    and every read raised UndefinedTable.
    """
    import os

    opened = []

    def factory():
        conn = psycopg.connect(os.environ["ARB_MEMORY_DSN"])  # non-autocommit, like production
        conn.execute('SET search_path TO "arb_readloop_marker", public')
        opened.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rp")
    try:
        assert loop.conn.autocommit is True, "the lane must end up in autocommit"
        search_path = loop.conn.execute("SHOW search_path").fetchone()[0]
        assert "arb_readloop_marker" in search_path, (
            f"the factory's session setup was DISCARDED (search_path={search_path!r}). Rolling "
            "back a factory-opened transaction silently reverts its SETs, and the lane then "
            "queries the wrong schema."
        )
    finally:
        for c in opened:
            if not c.closed:
                c.close()


def test_conn_unusable_treats_aborted_transaction_as_unusable(redis_bus, conn_factory, fake_embed):
    """An INERROR connection cannot serve reads, so the lane MUST rebuild.

    Fails against 98ca6482, which recognised only UNKNOWN. Every further statement on an aborted
    transaction raises InFailedSqlTransaction until rollback, so declining to rebuild wedges the
    lane permanently -- the original outage shape, reached via `_new_conn`'s old fail-open.
    """
    loop = ReadLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="ru")
    aborted = conn_factory()
    aborted.autocommit = False
    with pytest.raises(psycopg.Error):
        aborted.execute("SELECT * FROM definitely_no_such_table_arb")
    assert aborted.info.transaction_status == psycopg.pq.TransactionStatus.INERROR, (
        "premise: the connection must be in an aborted transaction"
    )
    assert not aborted.closed, "premise: INERROR is invisible to a closed-check"

    loop.conn = aborted
    try:
        assert loop._conn_unusable() is True, (
            "an aborted transaction was reported usable; the lane would keep it and every "
            "subsequent read would raise InFailedSqlTransaction forever"
        )
    finally:
        aborted.close()


def test_new_conn_raises_rather_than_returning_the_outage_shape(redis_bus, conn_factory, fake_embed):
    """If autocommit cannot be set, RAISE -- never hand back the outage-shaped connection.

    Fails against 98ca6482, whose `except Exception: return conn` silently reinstated a
    non-autocommit read lane. `psycopg` really does refuse the assignment on an open transaction
    (`can't change 'autocommit' now: connection in transaction status INTRANS`), so this is a
    reachable path, not a hypothetical.
    """

    class RefusesAutocommit:
        """A connection whose autocommit setter refuses, as psycopg's does when INTRANS."""

        closed = False

        def __init__(self, inner):
            self._inner = inner

        @property
        def info(self):
            return self._inner.info

        @property
        def autocommit(self):
            return False

        @autocommit.setter
        def autocommit(self, value):
            raise psycopg.ProgrammingError("can't change 'autocommit' now")

        def commit(self):
            self._inner.commit()

        def rollback(self):
            self._inner.rollback()

        def close(self):
            self._inner.close()

    inner = conn_factory()
    try:
        with pytest.raises(psycopg.ProgrammingError):
            ReadLoop(
                redis_bus,
                lambda: RefusesAutocommit(inner),
                embed=fake_embed,
                prefix=redis_bus.prefix,
                consumer="rs",
            )
        # The socket must be CLOSED, not merely abandoned. Round 3 (opus5 F2, mutation B): removing
        # the close leaked one connection per refused refresh and the whole suite stayed green, so
        # the line commented "do not leak the socket we are refusing to use" had no test at all.
        assert inner.closed, (
            "the connection we refused to configure was left open -- one leaked socket per "
            "refused refresh, and the read lane refreshes on every failed read"
        )
    finally:
        if not inner.closed:
            inner.close()


def test_readloop_refuses_a_bare_connection(redis_bus, conn_factory, fake_embed):
    """Back-compat wrapping a bare connection was a silent no-reconnect trap (3 seats).

    conn_factory() returned the same dead object, so `old_conn is not new_conn` was False:
    nothing created, nothing closed, nothing logged -- the original bug's failure class.
    Refuse it loudly instead.
    """
    # The regex must be WIDER than the word "callable". Round 4 (opus5 F1): deleting the entire
    # guard left all 64 read tests green, because with the guard gone `_new_conn` calls the bare
    # connection and CPython raises "'Connection' object is not callable" -- which also matches
    # /callable/. That was the THIRD iteration of this same false-green (round 2's `callable()`
    # assertion, round 3's lambda-wrap). Match text only OUR message contains.
    with pytest.raises(TypeError, match="requires a callable conn_factory"):
        ReadLoop(redis_bus, conn_factory(), embed=fake_embed, prefix=redis_bus.prefix, consumer="rb")


def test_memory_consumer_passes_the_factory_itself(redis_bus, conn_factory, fake_embed):
    """Identity, not callability -- the previous guard was false-green.

    `callable(...)` passed even on the pre-fix `ReadLoop(redis, conn_factory(), ...)` shape,
    because the back-compat wrap made a bare connection callable. Reverting the guarded line
    left the test green (reproduced).
    """
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    assert consumer.read_loop.conn_factory is conn_factory, (
        "MemoryConsumer must hand ReadLoop the factory object itself"
    )


# --- Round-4 remediation: panel-readloop-r3-20260728T092857Z-a1e9d5 -----------------
# codex returned two P1s against f0638d05, both reproduced by the orchestrator before
# remediation:
#   1. The commit-failure fallback rolled back (discarding the factory's session setup) and
#      then RETURNED the connection as usable -- the same wrong-schema defect round 3 removed,
#      reached through a narrower door. Reproduced: search_path reverted to `"$user", public`
#      while the lane came up healthy (autocommit=True, closed=False).
#   2. `test_readloop_rebuilds_after_idle_in_transaction_reap` reached the rebuild via `closed`,
#      so the STATUS branch of `_conn_unusable` was never exercised by the test named for it.
#      Reproduced: gutting the status branch left that test green.


class _CommitFails:
    """A factory connection whose commit() raises but whose rollback() would succeed.

    Models a factory that performs deferred setup work failing at commit time.
    """

    def __init__(self, inner):
        self._inner = inner
        self.rollback_calls = 0

    @property
    def closed(self):
        return self._inner.closed

    @property
    def info(self):
        return self._inner.info

    def _get_autocommit(self):
        return self._inner.autocommit

    def _set_autocommit(self, value):
        self._inner.autocommit = value

    autocommit = property(_get_autocommit, _set_autocommit)

    def commit(self):
        raise psycopg.ProgrammingError("deferred factory setup failed at commit")

    def rollback(self):
        self.rollback_calls += 1
        self._inner.rollback()

    def close(self):
        self._inner.close()

    def execute(self, *args, **kwargs):
        return self._inner.execute(*args, **kwargs)


def test_new_conn_refuses_a_connection_whose_setup_cannot_be_committed(redis_bus, conn_factory, fake_embed):
    """A commit failure must CLOSE and RAISE, never install a setup-discarded connection.

    Fails against f0638d05, whose `except: rollback()` fallback continued and returned a live
    connection with the factory's `SET search_path` silently reverted -- reproduced, lane healthy,
    wrong schema. Rolling back and carrying on reinstates the round-2 defect.
    """
    import os

    inner = psycopg.connect(os.environ["ARB_MEMORY_DSN"])  # non-autocommit
    inner.execute('SET search_path TO "arb_commitfail_marker", public')
    wrapped = _CommitFails(inner)
    try:
        with pytest.raises(psycopg.Error):
            ReadLoop(redis_bus, lambda: wrapped, embed=fake_embed, prefix=redis_bus.prefix, consumer="rc2")
        assert wrapped.rollback_calls == 0, (
            "the lane rolled back after a failed commit; that discards the factory's session "
            "setup, and continuing afterwards installs a live wrong-schema connection"
        )
        assert inner.closed, "the connection we refused to use must be closed, not leaked"
    finally:
        if not inner.closed:
            inner.close()


class _UnusableStatus:
    """Not closed, but reporting a status on which no further read can succeed."""

    closed = False

    def __init__(self, status):
        self._status = status

    @property
    def info(self):
        class _Info:
            transaction_status = self._status

        return _Info()


def test_refresh_rebuilds_on_unusable_status_even_when_not_closed(redis_bus, conn_factory, fake_embed):
    """The STATUS branch of `_conn_unusable` must be able to drive a rebuild on its own.

    Round 3 (codex, P1): the test named for the 25P03 reap reached the rebuild through `closed`,
    so gutting the status branch left it green -- the status predicate had no test of its own for
    the not-closed case. This pins it: `closed=False` plus an unusable status must still rebuild.
    """
    handed_out = []

    def factory():
        conn = conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rst")
    before = len(handed_out)

    loop.conn = _UnusableStatus(psycopg.pq.TransactionStatus.UNKNOWN)
    assert loop.conn.closed is False, "premise: this must be decided by STATUS, not by closed"
    assert loop._conn_unusable() is True, "an UNKNOWN-status connection must report unusable"

    # A poison-classified exception, so the rebuild can only come from the status branch.
    loop._refresh_conn(psycopg.errors.IdleInTransactionSessionTimeout("reaped"), "0-0")
    assert len(handed_out) == before + 1, (
        "no rebuild from the status branch alone -- the predicate is dead code for every "
        "not-yet-closed unusable connection"
    )


class _StatusRaises:
    """Not closed, but reading its transaction_status raises — a non-psycopg connection object."""

    closed = False

    @property
    def info(self):
        raise RuntimeError("connection status is unreadable")


def test_conn_unusable_fails_closed_when_status_is_unreadable(redis_bus, conn_factory, fake_embed):
    """An unreadable status must report UNUSABLE, not usable.

    Round 3 (opus5 F2, mutation A): reverting this to `return False` is the VERBATIM round-2
    fail-open defect, and the entire suite stayed green — the branch had no test. Failing open here
    leaves the lane holding a connection it refuses to rebuild.
    """
    loop = ReadLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rur")
    loop.conn = _StatusRaises()
    assert loop._conn_unusable() is True, (
        "a connection whose status cannot even be read was reported usable; that is the round-2 "
        "fail-open, which wedged the lane on a connection it declined to rebuild"
    )


def test_new_conn_refuses_a_factory_connection_in_an_aborted_transaction(
    redis_bus, conn_factory, fake_embed, caplog
):
    """An INERROR factory connection must be REFUSED and CLOSED, not served.

    Round 4 (opus5 F4, codex F1, agy F2 — three seats). The previous version of this test asserted
    `loop.conn.autocommit is True`, i.e. it asserted the lane came up USABLE — so it ENSHRINED the
    defect rather than detecting it. opus5 measured what serving that connection means: live,
    autocommit, IDLE, wrong schema; every read raises UndefinedTable which classifies as "poison";
    `_conn_unusable()` is False because the connection is healthy; so the lane NEVER rebuilds.
    Permanent silence, `memory_query` returns None, indistinguishable from a cache miss — the exact
    7-day-outage failure class.

    The log line is still required (it is the only diagnostic), but it is now secondary to refusing.
    """
    import os

    opened = []

    def aborted_factory():
        conn = psycopg.connect(os.environ["ARB_MEMORY_DSN"])  # non-autocommit
        conn.execute('SET search_path TO "arb_aborted_marker", public')
        try:
            conn.execute("SELECT * FROM no_such_table_for_abort")  # -> INERROR, setup doomed
        except psycopg.Error:
            pass
        opened.append(conn)
        return conn

    try:
        with caplog.at_level("ERROR", logger=bus.__name__):
            with pytest.raises(RuntimeError, match="aborted transaction"):
                ReadLoop(redis_bus, aborted_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rab")

        assert opened and opened[0].closed, (
            "the refused connection was left open — one leaked backend per refresh attempt"
        )
        assert any(
            r.name == bus.__name__ and "ABORTED" in r.getMessage() for r in caplog.records
        ), (
            "refusing without logging leaves no diagnostic. COMMIT does not raise on INERROR, so "
            f"the log line is the only signal that setup was lost. saw: "
            f"{[(r.name, r.getMessage()[:70]) for r in caplog.records]}"
        )
    finally:
        for c in opened:
            if not c.closed:
                c.close()


# --- Round-5 remediation: panel-readloop-r4-20260728T101211Z-1151b0 -----------------
# UNANIMOUS needs-changes/P1 from four seats, all agreeing there is NO P0 (the INTRANS/INERROR
# branch is unreachable with production's bare `psycopg.connect`, which returns IDLE). All four
# independently reproduced the author's five mutations as caught -- but EIGHT more survived, found
# by three different seats. The fix was under-pinned, not wrong. Each test below kills a
# specifically-named surviving mutant.


def test_production_shape_idle_connection_becomes_autocommit_and_stays_idle(
    redis_bus, conn_factory, fake_embed
):
    """THE load-bearing line: prod hands over an IDLE non-autocommit conn; the lane must flip it.

    Round 4 (grok, P1, "mutation K2" — reproduced by the orchestrator). Every other prod-shape test
    forces INTRANS by running `SET search_path` inside the factory, and the shared `conn_factory`
    fixture forces `autocommit=True`. So NOTHING exercised the actual production path:
    `run.py:18-21` is a bare `psycopg.connect(DSN)`, which returns autocommit=False in status IDLE,
    and `_new_conn` flips it with a plain `conn.autocommit = True`. Mutating that one line to leave
    autocommit False left 64 read tests green while the lane went back to sitting INTRANS after
    every SELECT — the exact 7-day-outage root cause, with the suite fully green.

    This test uses a SERVER-SIDE search_path (`options="-c search_path=..."`), applied at connection
    startup rather than by a client `SET`, so the connection arrives IDLE exactly as production's
    does while still being able to see the test schema.
    """
    import os

    schema = _scratch_schema(conn_factory)
    opened = []

    def production_shape_factory():
        conn = psycopg.connect(
            os.environ["ARB_MEMORY_DSN"], options=f"-c search_path={schema},public"
        )
        opened.append(conn)
        return conn

    # Premise: this really is production's shape — non-autocommit AND IDLE (no open transaction).
    probe = production_shape_factory()
    assert probe.autocommit is False, "premise: production's factory is non-autocommit"
    assert probe.info.transaction_status == psycopg.pq.TransactionStatus.IDLE, (
        "premise: production's factory opens NO transaction — that is what distinguishes this "
        "test from the INTRANS-forcing ones, and it is the path that had no coverage"
    )

    write_loop = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    memory_write(
        redis_bus,
        artefact={"artefact_id": "prodshape.md", "content": "c"},
        hints=[{"text": "production shape target hint"}],
        prefix=redis_bus.prefix,
    )
    entry_id, intent = write_loop.read_one()
    write_loop.handle_and_ack(entry_id, intent)

    loop = ReadLoop(
        redis_bus, production_shape_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rps"
    )
    try:
        assert loop.conn.autocommit is True, (
            "the lane did not flip production's connection into autocommit — it will sit INTRANS "
            "after the first SELECT and the server will reap it (25P03). This is the outage."
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                memory_query,
                redis_bus,
                "production shape target hint",
                1,
                timeout_s=3.0,
                prefix=redis_bus.prefix,
            )
            _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") >= 1)
            loop.step()
            hits = future.result(timeout=3.0)

        assert hits is not None and any(
            "production shape target hint" in h["hint"]["text"] for h in hits
        ), "the production-shape lane could not serve a read at all"

        status = loop.conn.info.transaction_status
        assert status == psycopg.pq.TransactionStatus.IDLE, (
            f"production-shape lane left in {status!r} after a SUCCESSFUL read. An INTRANS-idle "
            "connection is what the server reaps and what pins a transaction snapshot — the "
            "original incident."
        )
    finally:
        for c in opened:
            if not c.closed:
                c.close()


def test_refresh_rebuilds_on_a_transient_exception_alone(redis_bus, conn_factory, fake_embed):
    """The `transient` disjunct must be able to drive a rebuild by itself.

    Round 4 (agy, P1 — reproduced): mutating `transient = classify_infra_error(exc) == "transient"`
    to `transient = False` left 46 tests green, because EVERY existing test reached the rebuild
    through `_conn_unusable()` (closed socket or bad status). The transient half of the predicate
    had no coverage. Here the connection is deliberately healthy, so only `transient` can fire.
    """
    handed_out = []

    def factory():
        conn = conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rtr")
    before = len(handed_out)
    assert loop._conn_unusable() is False, (
        "premise: the connection must look USABLE, so that a rebuild can only come from the "
        "transient-exception branch"
    )

    loop._refresh_conn(psycopg.OperationalError("connection lost"), "0-0")
    assert len(handed_out) == before + 1, (
        "no rebuild from a transient exception on a healthy-looking connection — the `transient` "
        "half of the rebuild predicate is dead code"
    )


def test_refresh_does_not_kill_the_lane_when_the_factory_is_down(redis_bus, conn_factory, fake_embed):
    """A factory that raises during refresh must NOT escape and kill the read thread.

    Round 4 (opus5 F2, P1): mutations removing the `except` guard in `_refresh_conn`, and one
    nulling `self.conn` before the rebuild, BOTH survived — the whole refresh-failure path was
    untested. Without that guard the exception escapes `step()`, and `run()` catches only
    `redis.RedisError`, so the daemon's read thread dies silently: callers then see `blpop` timeouts
    mapped to None, i.e. "cache miss". That is the 7-day-outage signature exactly.
    """
    calls = []

    def failing_after_first():
        if calls:
            raise psycopg.OperationalError("the connection is closed")
        conn = conn_factory()
        calls.append(conn)
        return conn

    loop = ReadLoop(redis_bus, failing_after_first, embed=fake_embed, prefix=redis_bus.prefix, consumer="rfd")
    loop.conn.close()  # force _conn_unusable() -> True so a refresh is attempted

    # Must NOT raise, and must leave the lane with a connection object to retry from.
    loop._refresh_conn(psycopg.OperationalError("lost"), "1-1")
    assert loop.conn is not None, (
        "the lane was left with no connection after a failed refresh; the next read would raise "
        "AttributeError instead of retrying"
    )

    # And the lane must still be able to rebuild once the database comes back.
    calls.clear()
    loop._refresh_conn(psycopg.OperationalError("lost"), "1-2")
    assert calls, "the lane did not retry the factory after the outage cleared"


def test_refresh_closes_the_superseded_connection(redis_bus, conn_factory, fake_embed):
    """A superseded but still-OPEN connection must be closed exactly once.

    Round 4 (opus5 F3 + codex F3, P1): deleting `old_conn.close()` in `_refresh_conn` left 64 tests
    green. The identical defect class was pinned for `_new_conn` in round 4 (`assert inner.closed`)
    but its sibling here was missed. The INERROR branch of `_conn_unusable` fires on a connection
    with `closed=False`, so the refresh genuinely supersedes a LIVE backend — one leaked PG backend
    per refresh, on the lane that refreshes after every failed read.

    The only existing test touching this path used a stub with no `close()` to observe.
    """
    handed_out = []

    def factory():
        conn = conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rsc")

    # A real, OPEN connection in an aborted transaction: unusable by status, not by `closed`.
    old = conn_factory()
    old.autocommit = False
    with pytest.raises(psycopg.Error):
        old.execute("SELECT * FROM no_such_table_supersede")
    assert old.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
    assert not old.closed, "premise: the superseded connection must be OPEN, or nothing leaks"
    loop.conn = old

    before = len(handed_out)
    loop._refresh_conn(psycopg.errors.IdleInTransactionSessionTimeout("reaped"), "0-0")

    assert len(handed_out) == before + 1, "no rebuild happened, so this test proves nothing"
    assert loop.conn is not old, "the lane kept the superseded connection"
    assert old.closed, (
        "the superseded connection was left OPEN — one leaked Postgres backend per refresh, and "
        "this lane refreshes on every failed read"
    )


def test_non_infra_errors_do_not_rebuild_the_connection(redis_bus, conn_factory, fake_embed):
    """A non-infrastructure error must NOT be treated as transient.

    Round 4 (opus5 F6, P2): mutating the `except TypeError: transient = False` default to `True`
    survived. A TypeError out of `classify_infra_error` means "this is not an infrastructure
    error" — an application-level bug such as a broken `embed`. Treating those as transient would
    rebuild the Postgres connection on every application error, silently.
    """
    handed_out = []

    def factory():
        conn = conn_factory()
        handed_out.append(conn)
        return conn

    loop = ReadLoop(redis_bus, factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="rni")
    before = len(handed_out)
    assert loop._conn_unusable() is False, "premise: the connection is healthy"

    # `classify_infra_error` raises TypeError for a non-exception argument -> not infra -> no rebuild.
    loop._refresh_conn("not an exception at all", "0-0")
    assert len(handed_out) == before, (
        "the lane rebuilt its connection for a non-infrastructure error; an application bug would "
        "churn Postgres connections on every read"
    )
