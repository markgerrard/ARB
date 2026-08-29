"""S4a: bus hint-read record-intent producer + store.retrieve withheld (TDD).

Asserts against the real argument passed to redis.xadd — never a hand-built
expected-wire twin that can drift independently of production (DoD item 7).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

import pytest

from arb_memory.bus import MAXLEN, handle_read_request, hint_reads_stream, reply_key
from arb_memory.store import retrieve, write_artefact_and_hints

pytest_plugins = ("tests.arb_memory.conftest",)


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
    """One learn_proposal (withheld) + one ordinary; both searchable."""
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "learn-s4a", "content": "EXTERNAL BODY"},
        hints=[
            {
                "text": "learn proposal s4a",
                "embedding": fake_embed("learn proposal s4a"),
                "metadata": {"kind": "artefact_index", "learn_proposal": True},
            }
        ],
    )
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "note-s4a", "content": "ordinary note"},
        hints=[
            {
                "text": "ordinary note s4a",
                "embedding": fake_embed("ordinary note s4a"),
                "metadata": {"kind": "artefact_index"},
            }
        ],
    )


def _call_read(spy, conn, fake_embed, *, query="learn proposal ordinary note s4a", k=5, cid="cid-s4a", prefix=""):
    reply = reply_key(cid, prefix=prefix)
    handle_read_request(
        spy,
        conn,
        {"cid": cid, "reply": reply, "query": query, "k": str(k)},
        embed=fake_embed,
        prefix=prefix,
    )
    return reply


def _last_xadd(spy):
    assert spy.xadd_calls, "expected at least one xadd"
    return spy.xadd_calls[-1]


# --- Part A: store.retrieve emits withheld ---------------------------------


def test_retrieve_emits_outer_withheld(scratch, fake_embed):
    """D-3 / F-09: withheld is on the outer retrieve element, from learn_proposal."""
    _seed_two_hints(scratch, fake_embed)
    hits = retrieve(scratch, "learn proposal ordinary note s4a", k=5, embed=fake_embed)
    by_aid = {h["hint"]["artefact_id"]: h for h in hits}
    assert "withheld" in by_aid["learn-s4a"]
    assert by_aid["learn-s4a"]["withheld"] is True
    assert by_aid["note-s4a"]["withheld"] is False
    assert set(by_aid["learn-s4a"].keys()) >= {"hint", "artefact", "repo_pointer", "withheld"}


# --- Part B: bus producer ---------------------------------------------------


def test_wire_shape_from_real_call_no_raw_query_by_default(scratch, fake_embed, monkeypatch):
    """Spy on xadd; assert captured fields carry expected keys and no raw query_text."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()
    prefix = "p:"
    reply = _call_read(spy, scratch, fake_embed, prefix=prefix, query="find things", k=5)

    # reply still delivered
    assert spy.llen(reply) == 1
    envelope = json.loads(spy.lists[reply][0])
    assert envelope["status"] == "ok"
    assert "hits" in envelope

    stream, fields, kwargs = _last_xadd(spy)
    assert stream == hint_reads_stream(prefix)

    expected_keys = {
        "query_hmac",
        "query_truncated",
        "k",
        "outcome",
        "hit_count",
        "cid",
        "served_at",
        "hits",
    }
    assert expected_keys <= set(fields.keys())
    assert "query_text" not in fields
    assert "run_id" not in fields
    assert "seat_id" not in fields
    assert fields["outcome"] == "ok"
    assert fields["cid"] == "cid-s4a"
    assert fields["k"] == "5"
    assert fields["query_truncated"] in ("0", "1")
    assert fields["query_truncated"] == "0"
    # hit_count matches real retrieve length, not a hand-built twin
    assert int(fields["hit_count"]) == len(envelope["hits"])
    # served_at is an ISO timestamp string
    assert "T" in fields["served_at"]


def test_withheld_round_trips_from_outer_level(scratch, fake_embed, monkeypatch):
    """Hit with learn_proposal → wire hits[].withheld true; ordinary → false.

    Driven through store.retrieve's real output shape (Part A), not a hand-made dict.
    """
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query="learn proposal ordinary note s4a", k=5)

    _, fields, _ = _last_xadd(spy)
    wire_hits = json.loads(fields["hits"])
    assert wire_hits, "expected at least one wire hit"

    # Map wire hits back via real retrieve shape
    real = retrieve(scratch, "learn proposal ordinary note s4a", k=5, embed=fake_embed)
    real_by_id = {h["hint"]["id"]: h for h in real}
    for wh in wire_hits:
        real_hit = real_by_id[wh["hint_id"]]
        assert wh["withheld"] is real_hit["withheld"]
        assert isinstance(wh["withheld"], bool)

    assert any(wh["withheld"] is True for wh in wire_hits)
    assert any(wh["withheld"] is False for wh in wire_hits)


def test_nesting_hint_metrics_from_inner_hint(scratch, fake_embed, monkeypatch):
    """H-02: hint_id / vector_distance / lexical_rank come from hit['hint']."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query="learn proposal ordinary note s4a", k=5)

    _, fields, _ = _last_xadd(spy)
    wire_hits = json.loads(fields["hits"])
    real = retrieve(scratch, "learn proposal ordinary note s4a", k=5, embed=fake_embed)

    # ranks are 1-based and match order of retrieve output
    assert [wh["rank"] for wh in wire_hits] == list(range(1, len(wire_hits) + 1))
    for wh, real_hit in zip(wire_hits, real):
        inner = real_hit["hint"]
        assert wh["hint_id"] == inner["id"]
        assert wh["vector_distance"] == pytest.approx(inner.get("vector_distance"))
        # lexical_rank may be None or float; compare with get
        if inner.get("lexical_rank") is None:
            assert wh["lexical_rank"] is None
        else:
            assert wh["lexical_rank"] == pytest.approx(inner["lexical_rank"])


def test_recording_xadd_failure_never_fails_read(scratch, fake_embed, monkeypatch):
    """§7: force XADD to raise; read still returns its hits normally."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()

    def boom(*args, **kwargs):
        raise RuntimeError("forced xadd failure")

    spy.xadd = boom  # type: ignore[method-assign]
    reply = _call_read(spy, scratch, fake_embed, query="ordinary note s4a", k=3)
    assert spy.llen(reply) == 1
    envelope = json.loads(spy.lists[reply][0])
    assert envelope["status"] == "ok"
    assert len(envelope["hits"]) >= 1


def test_recording_construction_failure_never_fails_read(scratch, fake_embed, monkeypatch):
    """§7 + req 1: construction-time failure is inside the guard; read still replies.

    A hit missing a required outer key forces KeyError during fields build.
    If construction sits outside the try/except, this exception escapes.
    """
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    spy = SpyRedis()
    cid = "cid-construct"
    reply = reply_key(cid, prefix="")

    # retrieve returns a malformed hit (no outer withheld) so fields construction KeyErrors
    bad_hits = [
        {
            "hint": {"id": 1, "vector_distance": 0.1, "lexical_rank": 0.2},
            "artefact": None,
            "repo_pointer": None,
            # deliberately no "withheld"
        }
    ]

    with mock.patch("arb_memory.store.retrieve", return_value=bad_hits):
        # must not raise
        handle_read_request(
            spy,
            scratch,
            {"cid": cid, "reply": reply, "query": "q", "k": "1"},
            embed=fake_embed,
            prefix="",
        )

    assert spy.llen(reply) == 1
    envelope = json.loads(spy.lists[reply][0])
    assert envelope["status"] == "ok"
    assert envelope["hits"] == bad_hits
    # construction failed → no successful xadd
    assert spy.xadd_calls == []


def test_stream_is_bounded(scratch, fake_embed, monkeypatch):
    """G-04: captured xadd passes maxlen=MAXLEN, approximate=True."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "test-secret")
    _seed_two_hints(scratch, fake_embed)
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed)
    _, _, kwargs = _last_xadd(spy)
    assert kwargs.get("maxlen") == MAXLEN
    assert kwargs.get("approximate") is True


def test_d4_hmac_default_and_raw_opt_in(scratch, fake_embed, monkeypatch):
    """D-4: HMAC key set → query_hmac, no raw; raw=1 → both."""
    key = "d4-secret"
    query = "d4 query text"
    expected_hmac = hmac.new(key.encode(), query.encode(), hashlib.sha256).hexdigest()

    _seed_two_hints(scratch, fake_embed)

    # default: hmac, no raw
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", key)
    monkeypatch.delenv("ARB_HINT_READ_QUERY_RAW", raising=False)
    spy = SpyRedis()
    _call_read(spy, scratch, fake_embed, query=query, cid="cid-hmac")
    _, fields, _ = _last_xadd(spy)
    assert fields["query_hmac"] == expected_hmac
    assert "query_text" not in fields

    # raw opt-in: both
    monkeypatch.setenv("ARB_HINT_READ_QUERY_RAW", "1")
    spy2 = SpyRedis()
    _call_read(spy2, scratch, fake_embed, query=query, cid="cid-raw")
    _, fields2, _ = _last_xadd(spy2)
    assert fields2["query_hmac"] == expected_hmac
    assert fields2["query_text"] == query
