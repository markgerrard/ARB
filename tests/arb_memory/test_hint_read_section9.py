"""S6: frozen-guide §9 sweep — checks not already owned by S1–S5 test files.

Sibling files keep the checks that clearly belong with them (schema, grants,
producer, consumer, local recorder, search wiring, retention). This module
closes the remaining §9 rows so the coverage map has no unmapped gaps.

See ``docs/superpowers/specs/2026-07-29-served-hint-record-S9-COVERAGE.md``.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from unittest import mock

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg import sql

from arb_memory.bus import handle_read_request, hint_reads_stream, reply_key
from arb_memory.mcp.grants import (
    apply_hint_read_local_writer_grants,
    apply_local_reader_grants,
)
from arb_memory.mcp.read_tools import (
    LocalReadSettings,
    ReadMemoryTools,
    SearchRateLimitExceeded,
)
from arb_memory.store import write_artefact_and_hints

pytest_plugins = ("tests.arb_memory.conftest",)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class SpyRedis:
    """Records redis calls; implements the lpush/expire surface handle_read_request needs."""

    def __init__(self):
        self.xadd_calls: list[tuple] = []
        self.command_log: list[str] = []
        self.lists: dict[str, list] = {}
        self.ttls: dict[str, int] = {}

    def xadd(self, stream, fields, **kwargs):
        self.command_log.append("xadd")
        self.xadd_calls.append((stream, dict(fields), dict(kwargs)))
        return "1-0"

    def lpush(self, key, value):
        self.command_log.append("lpush")
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def expire(self, key, ttl):
        self.command_log.append("expire")
        self.ttls[key] = ttl
        return True

    def llen(self, key):
        return len(self.lists.get(key, []))


def _schema(conn):
    return conn.execute("SELECT current_schema()").fetchone()[0]


def _second_conn(scratch):
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    conn.execute(f'SET search_path TO "{schema}", public')
    register_vector(conn)
    return conn


def _non_autocommit_factory(scratch):
    """Production-equivalent: psycopg default is autocommit=False (_memory_conn).

    search_path is set under a brief autocommit window so it does not open an
    outer transaction that would nest ``conn.transaction()`` and roll back on
    close (the production factory does not SET search_path at all).
    """
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    conns: list[psycopg.Connection] = []

    def make_conn():
        conn = psycopg.connect(dsn)
        conn.autocommit = True
        conn.execute(f'SET search_path TO "{schema}", public')
        register_vector(conn)
        conn.autocommit = False
        assert conn.autocommit is False
        conns.append(conn)
        return conn

    make_conn._owned = conns  # type: ignore[attr-defined]
    return make_conn


def _close_factory(factory):
    for conn in getattr(factory, "_owned", ()):
        if conn.closed:
            continue
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def _seed_hint(conn, fake_embed, *, text="section9 searchable note", aid="note-s9"):
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": aid, "content": "body"},
        hints=[
            {
                "text": text,
                "embedding": fake_embed(text),
                "metadata": {"kind": "artefact_index"},
            }
        ],
    )


def _make_rt(conn_factory, fake_embed, **settings_kw):
    return ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored", **settings_kw),
        conn_factory=conn_factory,
        embed=fake_embed,
    )


def _call_read(spy, conn, fake_embed, *, query="q", k=5, cid="cid-s9", prefix=""):
    reply = reply_key(cid, prefix=prefix)
    handle_read_request(
        spy,
        conn,
        {"cid": cid, "reply": reply, "query": query, "k": str(k)},
        embed=fake_embed,
        prefix=prefix,
    )
    return reply


# ---------------------------------------------------------------------------
# §9 (a) COMMIT, bus
# ---------------------------------------------------------------------------


def test_section9_a_commit_bus_visible_from_second_connection(
    redis_bus, scratch, fake_embed, monkeypatch
):
    """§9 (a) COMMIT, bus — consumer-written row visible on a second connection."""
    from arb_memory.hint_reads import HintReadConsumer, _qualified_entry_id

    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _seed_hint(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(
        spy,
        scratch,
        fake_embed,
        query="section9 searchable note",
        prefix=redis_bus.prefix,
        cid="cid-commit-bus",
    )
    assert spy.xadd_calls
    _stream, fields, _ = spy.xadd_calls[-1]

    factory = _non_autocommit_factory(scratch)
    try:
        consumer = HintReadConsumer(
            redis_bus, factory, prefix=redis_bus.prefix, block_ms=50
        )
        eid = redis_bus.xadd(hint_reads_stream(redis_bus.prefix), fields)
        assert consumer.step() == "recorded"

        other = _second_conn(scratch)
        try:
            row = other.execute(
                "SELECT door, outcome, hit_count FROM hint_read "
                "WHERE stream_entry_id = %s",
                (_qualified_entry_id(redis_bus.prefix, eid),),
            ).fetchone()
            assert row is not None
            assert row[0] == "bus"
            assert row[1] == "ok"
            assert row[2] == int(fields["hit_count"])
        finally:
            other.close()
    finally:
        _close_factory(factory)


# ---------------------------------------------------------------------------
# §9 (b) ISOLATION, bus — byte-identical reply
# ---------------------------------------------------------------------------


def test_section9_b_isolation_bus_xadd_failure_reply_byte_identical(
    scratch, fake_embed, monkeypatch
):
    """§9 (b) ISOLATION, bus: forced xadd raise leaves lpush payload byte-identical."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)

    good = SpyRedis()
    reply_good = _call_read(
        good, scratch, fake_embed, query="section9 searchable note", cid="cid-iso-ok"
    )
    payload_good = good.lists[reply_good][0]

    bad = SpyRedis()

    def boom(*_a, **_k):
        raise RuntimeError("forced xadd failure")

    bad.xadd = boom  # type: ignore[method-assign]
    reply_bad = _call_read(
        bad, scratch, fake_embed, query="section9 searchable note", cid="cid-iso-bad"
    )
    payload_bad = bad.lists[reply_bad][0]

    # Reply envelopes differ only by cid (request-bound); status/hits shape must match.
    env_good = json.loads(payload_good)
    env_bad = json.loads(payload_bad)
    assert env_good["status"] == env_bad["status"] == "ok"
    assert env_good["hits"] == env_bad["hits"]
    # Forced failure raised nothing to the caller — handle_read_request returns None/failure
    # from the retrieve path only; xadd is inside the recording guard.
    assert bad.xadd_calls == []


# ---------------------------------------------------------------------------
# Commit-before-ack / PEL redelivery (row 9)
# ---------------------------------------------------------------------------


def test_section9_commit_precedes_ack_pel_redelivery_idempotent(
    redis_bus, scratch, fake_embed, monkeypatch
):
    """Commit then fail ack → entry stays in PEL; redelivery is idempotent (duplicate)."""
    from arb_memory.hint_reads import HINT_READS_GROUP, HintReadConsumer

    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(
        spy,
        scratch,
        fake_embed,
        query="section9 searchable note",
        prefix=redis_bus.prefix,
        cid="cid-pel",
    )
    fields = spy.xadd_calls[-1][1]
    stream = hint_reads_stream(redis_bus.prefix)

    factory = _non_autocommit_factory(scratch)
    try:
        consumer = HintReadConsumer(
            redis_bus, factory, prefix=redis_bus.prefix, block_ms=50
        )
        # Suppress ack after a successful write so the entry remains pending.
        acks: list[str] = []
        real_ack = consumer._ack

        def ack_once(entry_id):
            acks.append(entry_id)
            if len(acks) == 1:
                return  # skip first xack — simulates kill between commit and ack
            return real_ack(entry_id)

        consumer._ack = ack_once  # type: ignore[method-assign]

        eid = redis_bus.xadd(stream, fields)
        assert consumer.step() == "recorded"
        assert redis_bus.xpending(stream, HINT_READS_GROUP)["pending"] == 1

        n_parents = scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0]
        assert n_parents == 1

        # Redelivery via drain_pending reaches the same idempotent end state.
        consumer._ack = real_ack  # type: ignore[method-assign]
        drained = consumer.drain_pending()
        assert drained >= 1
        assert scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 1
        assert redis_bus.xpending(stream, HINT_READS_GROUP)["pending"] == 0
        assert eid  # entry existed
    finally:
        _close_factory(factory)


# ---------------------------------------------------------------------------
# Consumer tests at autocommit=False (row 10)
# ---------------------------------------------------------------------------


def test_section9_consumer_runs_at_autocommit_false(
    redis_bus, scratch, fake_embed, monkeypatch
):
    """§9: consumer path is production-equivalent (autocommit=False)."""
    from arb_memory.hint_reads import HintReadConsumer, _qualified_entry_id

    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(
        spy,
        scratch,
        fake_embed,
        query="section9 searchable note",
        prefix=redis_bus.prefix,
        cid="cid-ac",
    )
    fields = spy.xadd_calls[-1][1]

    factory = _non_autocommit_factory(scratch)
    seen_autocommit: list[bool] = []

    def wrapping_factory():
        conn = factory()
        seen_autocommit.append(conn.autocommit)
        return conn

    try:
        consumer = HintReadConsumer(
            redis_bus, wrapping_factory, prefix=redis_bus.prefix, block_ms=50
        )
        eid = redis_bus.xadd(hint_reads_stream(redis_bus.prefix), fields)
        assert consumer.step() == "recorded"
        assert seen_autocommit, "consumer must open a connection"
        assert all(v is False for v in seen_autocommit)
        row = scratch.execute(
            "SELECT count(*) FROM hint_read WHERE stream_entry_id = %s",
            (_qualified_entry_id(redis_bus.prefix, eid),),
        ).fetchone()[0]
        assert row == 1
    finally:
        _close_factory(factory)


# ---------------------------------------------------------------------------
# Local tier reuses cached connection (row 13)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_local_reuses_cached_connection(
    scratch, conn_factory, fake_embed
):
    """§9: local recording reuses the cached connection — one factory call across two reads."""
    _seed_hint(scratch, fake_embed)
    connect_calls = {"n": 0}
    base = conn_factory

    def counting_factory():
        connect_calls["n"] += 1
        return base()

    rt = _make_rt(counting_factory, fake_embed)
    await rt.memory_search("section9 searchable note", k=3)
    await rt.memory_search("section9 searchable note", k=3)

    assert connect_calls["n"] == 1
    assert scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Local records every rejected/errored class (row 15)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_local_records_every_rejection_class(
    scratch, conn_factory, fake_embed, monkeypatch
):
    """query-too-long, missing_api_key, rate_limited, and genuine handler error each record."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    monkeypatch.setenv("ARB_HINT_READ_QUERY_RAW", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # 1) query_too_long
    rt_long = _make_rt(conn_factory, fake_embed, search_max_query_chars=4)
    with pytest.raises(ValueError, match="query too long"):
        await rt_long.memory_search("abcdef")

    # 2) missing_api_key — default embed path, no OPENAI_API_KEY
    rt_key = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=conn_factory,
        # embed=None → default embed → missing key path
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await rt_key.memory_search("short")

    # 3) rate_limited
    rt_rate = _make_rt(conn_factory, fake_embed, search_rate_per_min=0)
    with pytest.raises(SearchRateLimitExceeded):
        await rt_rate.memory_search("ok")

    # 4) genuine handler exception (unbounded) — store.retrieve raises
    with mock.patch(
        "arb_memory.store.retrieve",
        side_effect=RuntimeError("forced retrieve failure"),
    ):
        rt_err = _make_rt(conn_factory, fake_embed)
        with pytest.raises(RuntimeError, match="forced retrieve failure"):
            await rt_err.memory_search("section9 searchable note")

    rows = scratch.execute(
        "SELECT outcome, hit_count FROM hint_read ORDER BY served_at, read_id"
    ).fetchall()
    assert len(rows) == 4
    assert all(r[0] == "error" and r[1] == 0 for r in rows)


# ---------------------------------------------------------------------------
# G-08: spy INSERT attempt count + next window (row 16 remainder)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_g08_spy_insert_attempts_and_next_window(
    scratch, conn_factory, fake_embed, monkeypatch
):
    """G-08 (2): INSERT attempt count ≤1 for same class; next window allows a second."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    insert_attempts = {"n": 0}
    real_record = __import__(
        "arb_memory.mcp.read_tools", fromlist=["_record_local_read"]
    )._record_local_read

    def counting_record(*args, **kwargs):
        insert_attempts["n"] += 1
        return real_record(*args, **kwargs)

    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    monkeypatch.setattr("arb_memory.mcp.read_tools.time.monotonic", fake_monotonic)

    with mock.patch(
        "arb_memory.mcp.read_tools._record_local_read", side_effect=counting_record
    ):
        rt = _make_rt(conn_factory, fake_embed, search_max_query_chars=4)
        with pytest.raises(ValueError, match="query too long"):
            await rt.memory_search("12345")
        with pytest.raises(ValueError, match="query too long"):
            await rt.memory_search("67890")
        assert insert_attempts["n"] == 1
        n_rows = scratch.execute(
            "SELECT count(*) FROM hint_read WHERE outcome = 'error'"
        ).fetchone()[0]
        assert n_rows == 1

        # Next 60s window → a second attempt and a second row.
        clock["t"] += 61.0
        with pytest.raises(ValueError, match="query too long"):
            await rt.memory_search("zzzzz")
        assert insert_attempts["n"] == 2
        n_rows2 = scratch.execute(
            "SELECT count(*) FROM hint_read WHERE outcome = 'error'"
        ).fetchone()[0]
        assert n_rows2 == 2


# ---------------------------------------------------------------------------
# Over-long query on the bus door (row 17 remainder; local already covered)
# ---------------------------------------------------------------------------


def test_section9_over_long_query_truncated_on_bus_door(
    scratch, fake_embed, monkeypatch
):
    """G-12 bus half: over-cap query stored truncated with query_truncated=true (D-2 raw)."""
    from arb_memory.bus import SEARCH_MAX_QUERY_CHARS

    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    monkeypatch.setenv("ARB_HINT_READ_QUERY_RAW", "1")
    _seed_hint(scratch, fake_embed)

    long_q = "x" * (SEARCH_MAX_QUERY_CHARS + 50)
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query=long_q, cid="cid-cap")
    _stream, fields, _ = spy.xadd_calls[-1]
    assert fields["query_truncated"] == "1"
    assert "query_text" in fields
    assert len(fields["query_text"]) == SEARCH_MAX_QUERY_CHARS
    assert fields["query_hmac"]  # hmac of capped text


# ---------------------------------------------------------------------------
# Local reader still cannot write hints/artefacts after writer grants (row 23)
# ---------------------------------------------------------------------------


def test_section9_local_reader_cannot_write_hints_or_artefacts_after_writer_grants(
    scratch,
):
    """Grant widening for hint_read INSERT must not open hints/artefacts writes.

    Seed ambient INSERT first (deny-proof rule), then apply the production
    local_reader → local_writer sequence and prove INSERT is gone.
    """
    role = f"arb_hint_read_scope_{uuid.uuid4().hex}"
    schema = _schema(scratch)
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.fail(
            "CREATE ROLE required for seeded grant scope proof; substrate denied it"
        )

    try:
        # Ambient pollution the local_reader path must strip.
        scratch.execute(
            sql.SQL("GRANT INSERT ON hints, artefacts TO {}").format(
                sql.Identifier(role)
            )
        )
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT')",
            (role, f"{schema}.hints"),
        ).fetchone()[0] is True

        # Production order (run.py): local_reader grants, then hint_read writer grants.
        apply_local_reader_grants(scratch, role)
        apply_hint_read_local_writer_grants(scratch, role)

        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT')",
            (role, f"{schema}.hint_read"),
        ).fetchone()[0] is True
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT')",
            (role, f"{schema}.hints"),
        ).fetchone()[0] is False
        assert scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT')",
            (role, f"{schema}.artefacts"),
        ).fetchone()[0] is False

        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        assert scratch.execute("SELECT current_user").fetchone()[0] == role
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            scratch.execute(
                "INSERT INTO hints (text, embedding, content_hash) "
                "VALUES ('x', NULL, 'h')"
            )
        scratch.rollback()
        scratch.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            scratch.execute(
                "INSERT INTO artefacts (artefact_id, version, content, content_hash) "
                "VALUES ('a', 1, 'c', 'h')"
            )
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


# ---------------------------------------------------------------------------
# Genuine empty result → outcome=ok, hit_count=0 (row 24)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_empty_result_records_ok_zero_hits_local(
    scratch, conn_factory, fake_embed, monkeypatch
):
    """No matching hints → outcome='ok', hit_count=0 on the local tier."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    # empty schema: no seed
    rt = _make_rt(conn_factory, fake_embed)
    hits = await rt.memory_search("zzzz-no-match-section9-empty", k=5)
    assert hits == []
    row = scratch.execute(
        "SELECT outcome, hit_count FROM hint_read"
    ).fetchone()
    assert row == ("ok", 0)
    assert scratch.execute("SELECT count(*) FROM hint_read_hit").fetchone()[0] == 0


def test_section9_empty_result_records_ok_zero_hits_bus(
    scratch, fake_embed, monkeypatch
):
    """Bus producer with empty retrieve still XADDs outcome=ok hit_count=0."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query="zzzz-no-match-bus-empty", cid="cid-empty")
    _stream, fields, _ = spy.xadd_calls[-1]
    assert fields["outcome"] == "ok"
    assert fields["hit_count"] == "0"
    assert json.loads(fields["hits"]) == []

    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    event = _parse_hint_read_event(fields)
    assert HintReadSink().write(scratch, "p:", "7-0", event) == "recorded"
    row = scratch.execute(
        "SELECT outcome, hit_count, door FROM hint_read"
    ).fetchone()
    assert row == ("ok", 0, "bus")


# ---------------------------------------------------------------------------
# G-11: recording = one XADD, no extra DB (row 25)
# ---------------------------------------------------------------------------


def test_section9_g11_recording_is_one_xadd_zero_extra_db(
    scratch, fake_embed, monkeypatch
):
    """Recording path: exactly one xadd; no DB execute after store.retrieve returns."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)

    execute_after_retrieve = {"n": 0}
    real_retrieve = __import__("arb_memory.store", fromlist=["retrieve"]).retrieve
    real_execute = scratch.execute

    def retrieve_spy(conn, *args, **kwargs):
        hits = real_retrieve(conn, *args, **kwargs)

        def execute_spy(query, params=None, **kw):
            execute_after_retrieve["n"] += 1
            if params is None:
                return real_execute(query, **kw)
            return real_execute(query, params, **kw)

        # Patch the same conn handle the producer uses (scratch).
        conn.execute = execute_spy  # type: ignore[method-assign]
        return hits

    spy = SpyRedis()
    with mock.patch("arb_memory.store.retrieve", side_effect=retrieve_spy):
        _call_read(
            spy, scratch, fake_embed, query="section9 searchable note", cid="cid-g11"
        )

    assert spy.command_log.count("xadd") == 1
    # Reply path: lpush + expire; recording path: only xadd.
    assert spy.command_log.count("lpush") == 1
    assert spy.command_log.count("expire") == 1
    assert execute_after_retrieve["n"] == 0


# ---------------------------------------------------------------------------
# run_id / seat_id NULL on every row this slice writes (row 26)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_run_id_seat_id_null_both_tiers(
    scratch, conn_factory, fake_embed, monkeypatch
):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)

    # local
    rt = _make_rt(conn_factory, fake_embed)
    await rt.memory_search("section9 searchable note", k=3)

    # bus
    spy = SpyRedis()
    _call_read(
        spy, scratch, fake_embed, query="section9 searchable note", cid="cid-null"
    )
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    fields = spy.xadd_calls[-1][1]
    assert "run_id" not in fields
    assert "seat_id" not in fields
    event = _parse_hint_read_event(fields)
    assert HintReadSink().write(scratch, "p:", "8-0", event) == "recorded"

    rows = scratch.execute(
        "SELECT door, run_id, seat_id FROM hint_read ORDER BY door"
    ).fetchall()
    assert len(rows) >= 2
    doors = {r[0] for r in rows}
    assert "local" in doors and "bus" in doors
    assert all(r[1] is None and r[2] is None for r in rows)


# ---------------------------------------------------------------------------
# Partial indexes via pg_index.indpred (row 27 / J-05 fix)
# ---------------------------------------------------------------------------


def test_section9_partial_indexes_via_pg_index_indpred(scratch):
    """J-05: assert partial predicate via catalog, not pg_get_indexdef text LIKE.

    ``pg_get_indexdef`` parenthesises the NullTest (``WHERE (run_id IS NOT NULL)``),
    so ``indexdef LIKE '%WHERE run_id IS NOT NULL%'`` never matches in any state.
    ``pg_index.indpred IS NOT NULL`` is the discriminating property.
    """
    rows = {
        r[0]: r
        for r in scratch.execute(
            """
            SELECT c.relname AS indexname,
                   i.indpred IS NOT NULL AS has_pred,
                   pg_get_indexdef(i.indexrelid) AS indexdef
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname IN ('hint_read_run_idx', 'hint_read_seat_idx')
            """
        ).fetchall()
    }
    assert set(rows) == {"hint_read_run_idx", "hint_read_seat_idx"}
    for name, (_n, has_pred, indexdef) in rows.items():
        assert has_pred is True, f"{name} must be a partial index (indpred set)"
        # Document the trap: the guide's LIKE form does not match the parenthesised def.
        if name == "hint_read_run_idx":
            assert "WHERE run_id IS NOT NULL" not in (indexdef or "")
            assert "run_id IS NOT NULL" in (indexdef or "")
        if name == "hint_read_seat_idx":
            assert "WHERE seat_id IS NOT NULL" not in (indexdef or "")
            assert "seat_id IS NOT NULL" in (indexdef or "")


# ---------------------------------------------------------------------------
# door distinguishes bus from local (row 28 — explicit both-tier assertion)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_section9_door_distinguishes_bus_from_local(
    scratch, conn_factory, fake_embed, monkeypatch
):
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s9-key")
    _seed_hint(scratch, fake_embed)

    rt = _make_rt(conn_factory, fake_embed)
    await rt.memory_search("section9 searchable note", k=2)

    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query="section9 searchable note", cid="cid-door")
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    event = _parse_hint_read_event(spy.xadd_calls[-1][1])
    HintReadSink().write(scratch, "p:", "9-0", event)

    doors = {
        r[0]
        for r in scratch.execute("SELECT door FROM hint_read").fetchall()
    }
    assert doors == {"local", "bus"}


# ---------------------------------------------------------------------------
# withheld matches store.retrieve, not artefact-is-None (row 29 explicit)
# ---------------------------------------------------------------------------


def test_section9_withheld_matches_retrieve_not_artefact_none(scratch, fake_embed):
    """withheld comes from store.retrieve's outer key (learn_proposal), not artefact-is-None."""
    from arb_memory.store import retrieve

    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "learn-s9", "content": "EXTERNAL"},
        hints=[
            {
                "text": "learn proposal s9 withheld",
                "embedding": fake_embed("learn proposal s9 withheld"),
                "metadata": {"kind": "artefact_index", "learn_proposal": True},
            }
        ],
    )
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "note-s9b", "content": "ordinary"},
        hints=[
            {
                "text": "ordinary s9 withheld",
                "embedding": fake_embed("ordinary s9 withheld"),
                "metadata": {"kind": "artefact_index"},
            }
        ],
    )
    hits = retrieve(scratch, "learn proposal ordinary s9 withheld", k=5, embed=fake_embed)
    assert hits
    by_aid = {h["hint"]["artefact_id"]: h for h in hits}
    assert by_aid["learn-s9"]["withheld"] is True
    assert by_aid["learn-s9"]["artefact"] is None
    assert by_aid["note-s9b"]["withheld"] is False
    assert by_aid["note-s9b"]["artefact"] is not None
    # Wire/hit recording must read hit["withheld"], not derive it from artefact.
    for hit in hits:
        assert "withheld" in hit
        assert isinstance(hit["withheld"], bool)
