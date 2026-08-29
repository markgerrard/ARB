"""S3b: wire `_record_local_read` into `memory_search` (TDD).

Covers frozen-guide §9 rows for the local read path: COMMIT, ISOLATION (both
forcing mechanisms), H-02 end-to-end hit shape, over-cap, G-08 bounding, and
recording failure never failing a successful read.
"""
from __future__ import annotations

import os
from unittest import mock

import psycopg
import pytest
from pgvector.psycopg import register_vector

from arb_memory.mcp.read_tools import (
    LocalReadSettings,
    ReadMemoryTools,
    SearchRateLimitExceeded,
    _cap,
)
from arb_memory.store import write_artefact_and_hints

pytest_plugins = ("tests.arb_memory.conftest",)


def _schema(conn):
    return conn.execute("SELECT current_schema()").fetchone()[0]


def _second_conn(scratch):
    """Independent connection into the same scratch schema (COMMIT visibility)."""
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    conn.execute(f'SET search_path TO "{schema}", public')
    register_vector(conn)
    return conn


def _seed_searchable(conn, fake_embed):
    """learn_proposal (withheld) + ordinary — real store.retrieve shape (H-02)."""
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "learn-s3b", "content": "EXTERNAL BODY"},
        hints=[
            {
                "text": "learn proposal s3b wiring",
                "embedding": fake_embed("learn proposal s3b wiring"),
                "metadata": {"kind": "artefact_index", "learn_proposal": True},
            }
        ],
    )
    write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "note-s3b", "content": "ordinary note"},
        hints=[
            {
                "text": "ordinary note s3b wiring",
                "embedding": fake_embed("ordinary note s3b wiring"),
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


async def _capture_exc(coro):
    try:
        await coro
    except Exception as exc:
        return {
            "type": type(exc),
            "message": str(exc),
            "cause": exc.__cause__,
            "context": exc.__context__,
            "exc": exc,
        }
    raise AssertionError("expected exception")


# --- §9 (a) COMMIT, local ---------------------------------------------------


@pytest.mark.anyio
async def test_section9_a_commit_local_visible_from_second_connection(
    scratch, conn_factory, fake_embed
):
    """§9 (a) COMMIT, local — row visible on a second, independent connection."""
    _seed_searchable(scratch, fake_embed)
    rt = _make_rt(conn_factory, fake_embed)
    hits = await rt.memory_search("learn proposal ordinary note s3b wiring", k=5)
    assert hits  # search itself worked

    other = _second_conn(scratch)
    try:
        parents = other.execute(
            "SELECT door, outcome, hit_count FROM hint_read"
        ).fetchall()
        assert len(parents) == 1
        assert parents[0][0] == "local"
        assert parents[0][1] == "ok"
        assert parents[0][2] == len(hits)
        hit_n = other.execute("SELECT count(*) FROM hint_read_hit").fetchone()[0]
        assert hit_n == len(hits)
    finally:
        other.close()


# --- §9 (b) ISOLATION, local — both forcing mechanisms ----------------------


@pytest.mark.anyio
async def test_section9_b_isolation_local_non_autocommit_success_and_rejection(
    scratch, fake_embed, monkeypatch
):
    """§9 (b)(i) non-autocommit conn_factory — return/exception match no-op recording."""
    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = _schema(scratch)
    _seed_searchable(scratch, fake_embed)
    owned: list[psycopg.Connection] = []

    def non_ac_factory():
        conn = psycopg.connect(dsn)
        conn.autocommit = False
        conn.execute(f'SET search_path TO "{schema}", public')
        register_vector(conn)
        owned.append(conn)
        return conn

    def ac_factory():
        conn = psycopg.connect(dsn)
        conn.autocommit = True
        conn.execute(f'SET search_path TO "{schema}", public')
        register_vector(conn)
        owned.append(conn)
        return conn

    try:
        # --- success path ---
        rt_broken = _make_rt(non_ac_factory, fake_embed)
        result_broken = await rt_broken.memory_search(
            "learn proposal ordinary note s3b wiring", k=5
        )

        with mock.patch(
            "arb_memory.mcp.read_tools._record_local_read", return_value=None
        ):
            rt_noop = _make_rt(ac_factory, fake_embed)
            result_noop = await rt_noop.memory_search(
                "learn proposal ordinary note s3b wiring", k=5
            )

        assert result_broken == result_noop

        # --- rejection path (rate_limited) ---
        rt_broken_rej = _make_rt(non_ac_factory, fake_embed, search_rate_per_min=0)
        broken_exc = await _capture_exc(rt_broken_rej.memory_search("short"))

        with mock.patch(
            "arb_memory.mcp.read_tools._record_local_read", return_value=None
        ):
            rt_noop_rej = _make_rt(ac_factory, fake_embed, search_rate_per_min=0)
            noop_exc = await _capture_exc(rt_noop_rej.memory_search("short"))

        assert broken_exc["type"] is noop_exc["type"] is SearchRateLimitExceeded
        assert broken_exc["message"] == noop_exc["message"]
        assert broken_exc["cause"] is noop_exc["cause"] is None
        assert broken_exc["context"] is noop_exc["context"] is None
    finally:
        for conn in owned:
            if conn.closed:
                continue
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()


@pytest.mark.anyio
async def test_section9_b_isolation_local_spy_insert_raises_success_and_rejection(
    scratch, conn_factory, fake_embed
):
    """§9 (b)(ii) spy execute raises on hint_read INSERT — incl. rejected path.

    The rejected/rate-limited path is required so should_record sits inside the
    forced failure, not merely the write.
    """
    _seed_searchable(scratch, fake_embed)

    def wrap_factory(base_factory):
        def factory():
            conn = base_factory()
            real_execute = conn.execute

            def execute_spy(query, params=None, **kwargs):
                q = query if isinstance(query, str) else str(query)
                if "INSERT INTO hint_read" in q and "hint_read_hit" not in q:
                    raise RuntimeError("forced hint_read insert failure")
                if params is None:
                    return real_execute(query, **kwargs)
                return real_execute(query, params, **kwargs)

            conn.execute = execute_spy  # type: ignore[method-assign]
            return conn

        return factory

    spy_factory = wrap_factory(conn_factory)

    # --- success path ---
    rt_broken = _make_rt(spy_factory, fake_embed)
    result_broken = await rt_broken.memory_search(
        "learn proposal ordinary note s3b wiring", k=5
    )

    with mock.patch(
        "arb_memory.mcp.read_tools._record_local_read", return_value=None
    ):
        rt_noop = _make_rt(conn_factory, fake_embed)
        result_noop = await rt_noop.memory_search(
            "learn proposal ordinary note s3b wiring", k=5
        )

    assert result_broken == result_noop
    # forced failure must not have left a parent row from the broken run;
    # noop may have written if not patched on the same factory — we patched the
    # function, so zero rows from either success comparison run is fine either way.

    # --- rejection path (rate_limited): should_record + write inside guard ---
    rt_broken_rej = _make_rt(spy_factory, fake_embed, search_rate_per_min=0)
    broken_exc = await _capture_exc(rt_broken_rej.memory_search("short"))

    with mock.patch(
        "arb_memory.mcp.read_tools._record_local_read", return_value=None
    ):
        rt_noop_rej = _make_rt(conn_factory, fake_embed, search_rate_per_min=0)
        noop_exc = await _capture_exc(rt_noop_rej.memory_search("short"))

    assert broken_exc["type"] is noop_exc["type"] is SearchRateLimitExceeded
    assert broken_exc["message"] == noop_exc["message"]
    assert broken_exc["cause"] is noop_exc["cause"] is None
    assert broken_exc["context"] is noop_exc["context"] is None


# --- H-02: the test whose absence let the nesting bug ship ------------------


@pytest.mark.anyio
async def test_h02_e2e_memory_search_records_real_retrieve_hit_shape(
    scratch, conn_factory, fake_embed
):
    """Drive memory_search end-to-end against real store.retrieve; inspect rows.

    Must NOT call _record_local_read with a hand-built hits tuple, and must NOT
    assert only on memory_search's return value (that was never wrong).
    """
    _seed_searchable(scratch, fake_embed)
    rt = _make_rt(conn_factory, fake_embed)

    # Real retrieve first so we know the ground-truth shape from the store.
    from arb_memory import store as store_mod

    real_hits = store_mod.retrieve(
        scratch,
        "learn proposal ordinary note s3b wiring",
        k=5,
        embed=fake_embed,
    )
    assert real_hits, "seed must be searchable"
    assert all("withheld" in h and "hint" in h for h in real_hits)

    result = await rt.memory_search("learn proposal ordinary note s3b wiring", k=5)
    # return value comes from store.retrieve — not the evidence under test
    assert len(result) == len(real_hits)

    parents = scratch.execute(
        "SELECT read_id, outcome, hit_count FROM hint_read"
    ).fetchall()
    assert len(parents) == 1
    read_id, outcome, hit_count = parents[0]
    assert outcome == "ok"
    assert hit_count == len(real_hits)

    rows = scratch.execute(
        "SELECT rank, hint_id, withheld, vector_distance, lexical_rank "
        "FROM hint_read_hit WHERE read_id = %s ORDER BY rank",
        (read_id,),
    ).fetchall()
    assert len(rows) == len(real_hits)

    for rank, hit in enumerate(real_hits, start=1):
        row = rows[rank - 1]
        assert row[0] == rank
        assert row[1] == hit["hint"]["id"]
        assert row[1] is not None
        assert row[2] is hit["withheld"]
        assert row[3] is not None  # vector_distance NON-NULL (H-02)
        assert float(row[3]) == pytest.approx(float(hit["hint"]["vector_distance"]))
        # lexical_rank may be 0.0 for pure vector matches; still non-None from store
        if hit["hint"].get("lexical_rank") is not None:
            assert float(row[4]) == pytest.approx(float(hit["hint"]["lexical_rank"]))


# --- Over-cap query ---------------------------------------------------------


@pytest.mark.anyio
async def test_over_cap_query_rejected_but_recorded_truncated(
    scratch, conn_factory, fake_embed, monkeypatch
):
    """Over-cap: rejected as a read; still recorded with query_truncated and capped text."""
    monkeypatch.setenv("ARB_HINT_READ_QUERY_KEY", "s3b-key")
    monkeypatch.setenv("ARB_HINT_READ_QUERY_RAW", "1")
    cap = 10
    query = "x" * 25
    capped, truncated = _cap(query, cap)
    assert truncated is True
    assert len(capped) == cap

    rt = _make_rt(conn_factory, fake_embed, search_max_query_chars=cap)
    with pytest.raises(ValueError, match="query too long") as excinfo:
        await rt.memory_search(query)
    assert type(excinfo.value) is ValueError  # not SearchRateLimitExceeded
    assert not isinstance(excinfo.value, SearchRateLimitExceeded)

    row = scratch.execute(
        "SELECT outcome, hit_count, query_truncated, query_text, query_hmac "
        "FROM hint_read"
    ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert row[1] == 0
    assert row[2] is True
    assert row[3] == capped
    assert len(row[3]) == cap
    assert row[4] is not None  # hmac of capped text


# --- G-08 bounding ----------------------------------------------------------


@pytest.mark.anyio
async def test_g08_bounding_same_class_one_row_different_classes_two(
    scratch, conn_factory, fake_embed, monkeypatch
):
    """G-08: same rejection class → ≤1 row / 60s; different classes → two rows."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Use injected embed so missing_api_key is not hit unless we build without embed.

    # Same class: two query_too_long rejections → one error row
    rt_same = _make_rt(conn_factory, fake_embed, search_max_query_chars=4)
    with pytest.raises(ValueError, match="query too long"):
        await rt_same.memory_search("12345")
    with pytest.raises(ValueError, match="query too long"):
        await rt_same.memory_search("67890")
    n_same = scratch.execute(
        "SELECT count(*) FROM hint_read WHERE outcome = 'error'"
    ).fetchone()[0]
    assert n_same == 1

    # Different classes on a fresh tools instance (independent bound state would
    # not matter; same instance with two classes must produce two).
    # Wipe for a clean count on this instance's writes only — use a second tools
    # object and count delta.
    before = scratch.execute("SELECT count(*) FROM hint_read WHERE outcome = 'error'").fetchone()[0]
    rt_diff = _make_rt(
        conn_factory,
        fake_embed,
        search_max_query_chars=4,
        search_rate_per_min=0,
    )
    with pytest.raises(ValueError, match="query too long"):
        await rt_diff.memory_search("abcde")  # query_too_long
    with pytest.raises(SearchRateLimitExceeded, match="search rate limit exceeded"):
        await rt_diff.memory_search("ab")  # rate_limited (short enough)
    after = scratch.execute("SELECT count(*) FROM hint_read WHERE outcome = 'error'").fetchone()[0]
    assert after - before == 2


# --- Recording failure never fails a successful read ------------------------


@pytest.mark.anyio
async def test_recording_failure_never_fails_successful_read(
    scratch, conn_factory, fake_embed
):
    """Force recorder to raise on the success path; memory_search still returns hits."""
    _seed_searchable(scratch, fake_embed)
    rt = _make_rt(conn_factory, fake_embed)

    with mock.patch(
        "arb_memory.mcp.read_tools._record_local_read",
        side_effect=RuntimeError("recorder boom"),
    ):
        hits = await rt.memory_search(
            "learn proposal ordinary note s3b wiring", k=5
        )
    assert hits
    assert all("hint" in h for h in hits)
    # no committed receipt (recorder raised every time)
    assert scratch.execute("SELECT count(*) FROM hint_read").fetchone()[0] == 0


# --- Smoke: _cap helper and exception type export ---------------------------


def test_cap_truncates_once():
    text, truncated = _cap("abcdefghij", 5)
    assert text == "abcde"
    assert truncated is True
    text2, truncated2 = _cap("abc", 5)
    assert text2 == "abc"
    assert truncated2 is False


def test_search_rate_limit_exceeded_is_valueerror_subclass():
    assert issubclass(SearchRateLimitExceeded, ValueError)
    assert SearchRateLimitExceeded is not ValueError
