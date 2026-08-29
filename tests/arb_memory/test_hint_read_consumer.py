"""S4b: HintReadConsumer / HintReadSink / deadletter (TDD).

THE critical contract (BUILD-CHARTER DoD item 7): capture the producer's real
xadd bytes and feed them through ``_parse_hint_read_event`` → ``HintReadSink.write``.
No hand-built wire dict for that path — fixture and producer must not be free to drift.
"""
from __future__ import annotations

import json

import pytest

from arb_memory.bus import handle_read_request, hint_reads_stream, reply_key
from arb_memory.store import write_artefact_and_hints

pytest_plugins = ("tests.arb_memory.conftest",)


# ---------------------------------------------------------------------------
# Helpers — shared with producer tests (spy + seed)
# ---------------------------------------------------------------------------


class SpyRedis:
    """Records xadd calls; implements the lpush/expire surface handle_read_request needs."""

    def __init__(self):
        self.xadd_calls: list[tuple] = []
        self.lists: dict[str, list] = {}
        self.ttls: dict[str, int] = {}

    def xadd(self, stream, fields, **kwargs):
        self.xadd_calls.append((stream, dict(fields), dict(kwargs)))
        return "1-0"

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def llen(self, key):
        return len(self.lists.get(key, []))


def _seed_two_hints(conn, fake_embed):
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "learn-s4b", "content": "EXTERNAL BODY"},
        hints=[
            {
                "text": "learn proposal s4b",
                "embedding": fake_embed("learn proposal s4b"),
                "metadata": {"kind": "artefact_index", "learn_proposal": True},
            }
        ],
    )
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "note-s4b", "content": "ordinary note"},
        hints=[
            {
                "text": "ordinary note s4b",
                "embedding": fake_embed("ordinary note s4b"),
                "metadata": {"kind": "artefact_index"},
            }
        ],
    )


def _call_read(
    spy,
    conn,
    fake_embed,
    *,
    query="learn proposal ordinary note s4b",
    k=5,
    cid="cid-s4b",
    prefix="",
):
    reply = reply_key(cid, prefix=prefix)
    handle_read_request(
        spy,
        conn,
        {"cid": cid, "reply": reply, "query": query, "k": str(k)},
        embed=fake_embed,
        prefix=prefix,
    )
    return reply


def _capture_producer_fields(scratch, fake_embed, monkeypatch, *, prefix="", query=None, k=5):
    """Run the real producer; return (prefix, entry_id, fields) from the spy xadd."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s4b-secret")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()
    q = query if query is not None else "learn proposal ordinary note s4b"
    _call_read(spy, scratch, fake_embed, prefix=prefix, query=q, k=k)
    assert spy.xadd_calls, "producer must have XADDed"
    stream, fields, _kwargs = spy.xadd_calls[-1]
    assert stream == hint_reads_stream(prefix)
    entry_id = "1-0"  # SpyRedis returns this; production uses Redis-assigned id
    return prefix, entry_id, fields


def _parent_rows(conn):
    return conn.execute(
        "SELECT read_id, door, outcome, query_hmac, query_text, query_truncated, "
        "k, hit_count, cid, stream_entry_id "
        "FROM hint_read ORDER BY stream_entry_id NULLS LAST, served_at"
    ).fetchall()


def _hit_rows(conn, read_id=None):
    if read_id is None:
        return conn.execute(
            "SELECT read_id, rank, hint_id, withheld, vector_distance, lexical_rank "
            "FROM hint_read_hit ORDER BY read_id, rank"
        ).fetchall()
    return conn.execute(
        "SELECT rank, hint_id, withheld, vector_distance, lexical_rank "
        "FROM hint_read_hit WHERE read_id = %s ORDER BY rank",
        (read_id,),
    ).fetchall()


def _dl_rows(conn):
    return conn.execute(
        "SELECT stream_entry_id, error FROM hint_read_deadletter ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# THE test: producer bytes → parse → sink → DB
# ---------------------------------------------------------------------------


def test_producer_bytes_through_parser_into_sink(scratch, fake_embed, monkeypatch):
    """DoD item 7: captured producer xadd fields feed parse → sink; assert DB rows.

    Does NOT hand-build a wire dict.
    """
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    prefix, entry_id, fields = _capture_producer_fields(scratch, fake_embed, monkeypatch)
    # fields is the exact dict the producer passed to xadd — not reconstructed
    event = _parse_hint_read_event(fields)
    result = HintReadSink().write(scratch, prefix, entry_id, event)
    assert result == "recorded"

    parents = _parent_rows(scratch)
    assert len(parents) == 1
    (
        read_id,
        door,
        outcome,
        query_hmac,
        query_text,
        query_truncated,
        k,
        hit_count,
        cid,
        stream_entry_id,
    ) = parents[0]
    assert door == "bus"
    assert outcome == "ok"
    assert query_hmac is not None  # key was set
    assert query_text is None  # raw not opted in
    assert query_truncated is False
    assert k == 5
    assert hit_count == int(fields["hit_count"])
    assert cid == fields["cid"]
    # H-04: stream_entry_id holds the fully-qualified value, not the bare id
    from arb_memory.hint_reads import _qualified_entry_id

    assert stream_entry_id == _qualified_entry_id(prefix, entry_id)
    assert stream_entry_id != entry_id or prefix == ""  # bare only if prefix empty still has stream path
    assert "/" in stream_entry_id

    hits = _hit_rows(scratch, read_id)
    wire_hits = json.loads(fields["hits"])
    assert len(hits) == len(wire_hits) == hit_count
    assert [r[0] for r in hits] == list(range(1, len(hits) + 1))
    for row, wh in zip(hits, wire_hits):
        rank, hint_id, withheld, vector_distance, lexical_rank = row
        assert hint_id == wh["hint_id"]
        assert withheld is wh["withheld"]
        if wh["vector_distance"] is None:
            assert vector_distance is None
        else:
            assert vector_distance == pytest.approx(wh["vector_distance"])
        if wh["lexical_rank"] is None:
            assert lexical_rank is None
        else:
            assert lexical_rank == pytest.approx(wh["lexical_rank"])


def test_redelivery_is_idempotent(scratch, fake_embed, monkeypatch):
    """Same prefix+entry_id twice → second returns 'duplicate'; one parent; one hit set."""
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    prefix, entry_id, fields = _capture_producer_fields(scratch, fake_embed, monkeypatch)
    event = _parse_hint_read_event(fields)
    sink = HintReadSink()

    assert sink.write(scratch, prefix, entry_id, event) == "recorded"
    assert sink.write(scratch, prefix, entry_id, event) == "duplicate"

    assert scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 1
    hit_count = scratch.execute("SELECT hit_count FROM hint_read").fetchone()[0]
    n_hits = scratch.execute("SELECT count(*) FROM hint_read_hit").fetchone()[0]
    assert n_hits == hit_count
    assert n_hits >= 1
    assert scratch.execute("SELECT count(*) FROM hint_read_deadletter").fetchone()[0] == 0


def test_h04_cross_prefix_distinct_rows(scratch, fake_embed, monkeypatch):
    """Same bare entry_id under two prefixes → two rows; stream_entry_id is qualified."""
    from arb_memory.hint_reads import (
        HintReadSink,
        _parse_hint_read_event,
        _qualified_entry_id,
    )

    # Capture once; reuse the same fields under two prefixes (same bare entry id).
    _prefix, entry_id, fields = _capture_producer_fields(
        scratch, fake_embed, monkeypatch, prefix="a:"
    )
    event = _parse_hint_read_event(fields)
    sink = HintReadSink()

    assert sink.write(scratch, "deploy-a:", entry_id, event) == "recorded"
    assert sink.write(scratch, "deploy-b:", entry_id, event) == "recorded"

    parents = _parent_rows(scratch)
    assert len(parents) == 2
    read_ids = {p[0] for p in parents}
    assert len(read_ids) == 2
    stream_ids = {p[9] for p in parents}
    assert stream_ids == {
        _qualified_entry_id("deploy-a:", entry_id),
        _qualified_entry_id("deploy-b:", entry_id),
    }
    for sid in stream_ids:
        assert sid != entry_id
        assert entry_id in sid

    # hit sets for both parents
    total_hits = scratch.execute("SELECT count(*) FROM hint_read_hit").fetchone()[0]
    expected = int(fields["hit_count"]) * 2
    assert total_hits == expected


def test_h02_nesting_on_bus_tier(scratch, fake_embed, monkeypatch):
    """hint_id/vector_distance/lexical_rank from inner hint; withheld from outer.

    After parse, the event carries nested hits; sink reads that nesting (H-02).
    """
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    prefix, entry_id, fields = _capture_producer_fields(scratch, fake_embed, monkeypatch)
    event = _parse_hint_read_event(fields)

    # Parsed event has nested shape — not flat wire shape
    assert event["hits"], "expected hits"
    for hit in event["hits"]:
        assert "hint" in hit
        assert "id" in hit["hint"]
        assert "withheld" in hit
        assert "hint_id" not in hit  # flat wire key must not remain as the source of truth

    # Force a mutation of nesting: if sink wrongly read flat keys, wrong/NULL data
    result = HintReadSink().write(scratch, prefix, entry_id, event)
    assert result == "recorded"
    wire_hits = json.loads(fields["hits"])
    rows = _hit_rows(scratch)
    # one parent
    read_ids = {r[0] for r in rows}
    assert len(read_ids) == 1
    for row, wh in zip(
        sorted(rows, key=lambda r: r[1]),
        wire_hits,
    ):
        _rid, rank, hint_id, withheld, vd, lr = row
        assert hint_id == wh["hint_id"]
        assert withheld is wh["withheld"]
        assert any(wh["withheld"] is True for wh in wire_hits)
        assert any(wh["withheld"] is False for wh in wire_hits)


def test_hit_count_and_rank_ordering(scratch, fake_embed, monkeypatch):
    """Ranks start at 1 and follow array order; hit_count matches hit rows."""
    from arb_memory.hint_reads import HintReadSink, _parse_hint_read_event

    prefix, entry_id, fields = _capture_producer_fields(scratch, fake_embed, monkeypatch)
    event = _parse_hint_read_event(fields)
    HintReadSink().write(scratch, prefix, entry_id, event)

    hit_count = scratch.execute("SELECT hit_count FROM hint_read").fetchone()[0]
    ranks = [
        r[0]
        for r in scratch.execute(
            "SELECT rank FROM hint_read_hit ORDER BY rank"
        ).fetchall()
    ]
    assert ranks == list(range(1, hit_count + 1))
    assert len(ranks) == hit_count
    # ranks follow the order of the hits array (enumerate start=1), not a re-sort
    wire_hits = json.loads(fields["hits"])
    db_hint_ids = [
        r[0]
        for r in scratch.execute(
            "SELECT hint_id FROM hint_read_hit ORDER BY rank"
        ).fetchall()
    ]
    assert db_hint_ids == [wh["hint_id"] for wh in wire_hits]


def test_deadletter_malformed_and_redelivery_idempotent(scratch):
    """Malformed entry → deadletter row; redelivered same entry does not UNIQUE-error."""
    from arb_memory.hint_reads import (
        _qualified_entry_id,
        deadletter_malformed_hint_read,
    )

    prefix = "p:"
    entry_id = "99-1"
    fields = {"outcome": "ok", "hits": "NOT-JSON{", "k": "1"}
    err = ValueError("hint-read event invalid hits")

    deadletter_malformed_hint_read(scratch, prefix, entry_id, fields, err)
    deadletter_malformed_hint_read(scratch, prefix, entry_id, fields, err)  # redelivery

    rows = _dl_rows(scratch)
    assert len(rows) == 1
    assert rows[0][0] == _qualified_entry_id(prefix, entry_id)
    assert "invalid hits" in (rows[0][1] or "")
    assert scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 0


def test_parse_malformed_hits_raises_value_error():
    """Parse denial: bad hits JSON raises the specific ValueError, not a bare Exception."""
    from arb_memory.hint_reads import _parse_hint_read_event

    with pytest.raises(ValueError, match="hits") as excinfo:
        _parse_hint_read_event(
            {
                "outcome": "ok",
                "k": "1",
                "query_truncated": "0",
                "served_at": "2026-07-29T00:00:00+00:00",
                "hits": "{not-json",
            }
        )
    assert isinstance(excinfo.value, ValueError)


def test_parse_missing_outcome_raises_value_error():
    from arb_memory.hint_reads import _parse_hint_read_event

    with pytest.raises(ValueError, match="outcome"):
        _parse_hint_read_event(
            {
                "k": "1",
                "query_truncated": "0",
                "served_at": "2026-07-29T00:00:00+00:00",
                "hits": "[]",
            }
        )


def test_consumer_malformed_entry_deadletters(redis_bus, conn_factory, scratch):
    """Full consumer path: malformed stream entry → hint_read_deadletter, then acked."""
    from arb_memory.hint_reads import (
        HINT_READS_GROUP,
        HintReadConsumer,
        _qualified_entry_id,
    )

    prefix = redis_bus.prefix
    stream = hint_reads_stream(prefix)
    consumer = HintReadConsumer(redis_bus, conn_factory, prefix=prefix, block_ms=50)
    eid = redis_bus.xadd(
        stream,
        {
            "outcome": "ok",
            "k": "1",
            "query_truncated": "0",
            "served_at": "2026-07-29T00:00:00+00:00",
            "hits": "not-json",
        },
    )
    assert consumer.step() == "dead-lettered"
    conn = conn_factory()
    n = conn.execute(
        "SELECT count(*) FROM hint_read_deadletter WHERE stream_entry_id = %s",
        (_qualified_entry_id(prefix, eid),),
    ).fetchone()[0]
    assert n == 1
    assert conn.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 0
    assert redis_bus.xpending(stream, HINT_READS_GROUP)["pending"] == 0


def test_consumer_happy_path_records(redis_bus, conn_factory, scratch, fake_embed, monkeypatch):
    """Consumer drains a real producer-shaped entry (via parse of captured fields)."""
    from arb_memory.hint_reads import HintReadConsumer, _qualified_entry_id

    prefix_for_fields = redis_bus.prefix
    _p, _eid, fields = _capture_producer_fields(
        scratch, fake_embed, monkeypatch, prefix=prefix_for_fields
    )
    stream = hint_reads_stream(prefix_for_fields)
    # Group is created at "$" — construct consumer first so the subsequent XADD is visible.
    consumer = HintReadConsumer(
        redis_bus, conn_factory, prefix=prefix_for_fields, block_ms=50
    )
    # Put the *captured* producer fields on the real stream (not a hand-built twin).
    eid = redis_bus.xadd(stream, fields)
    result = consumer.step()
    assert result == "recorded"
    conn = conn_factory()
    row = conn.execute(
        "SELECT stream_entry_id, door, hit_count FROM hint_read WHERE stream_entry_id = %s",
        (_qualified_entry_id(prefix_for_fields, eid),),
    ).fetchone()
    assert row is not None
    assert row[1] == "bus"
    assert row[2] == int(fields["hit_count"])
    n_hits = conn.execute(
        "SELECT count(*) FROM hint_read_hit h JOIN hint_read r ON r.read_id = h.read_id "
        "WHERE r.stream_entry_id = %s",
        (_qualified_entry_id(prefix_for_fields, eid),),
    ).fetchone()[0]
    assert n_hits == row[2]


def test_bus_read_id_is_deterministic():
    from arb_memory.hint_reads import _bus_read_id
    import uuid

    a = _bus_read_id("p:", "1-0")
    b = _bus_read_id("p:", "1-0")
    c = _bus_read_id("q:", "1-0")
    assert a == b
    assert a != c
    assert isinstance(a, uuid.UUID)
