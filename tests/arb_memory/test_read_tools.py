from datetime import datetime, timezone
import json

import pytest

from arb_memory.mcp.config import Settings
from arb_memory.mcp import read_tools as read_tools_mod
from arb_memory.mcp.read_tools import LocalReadSettings, ReadMemoryTools


class FakeConn:
    def __init__(self):
        self.closed = False


def fake_embed(text):
    return [0.0] * 1536


def test_local_read_settings_defaults_match_door_settings():
    settings = LocalReadSettings(dsn="postgresql://ignored")
    assert settings.search_max_query_chars == Settings(
        public_base_url="https://memory.example",
        mcp_dsn="postgresql://door",
        login_secret="login",
        totp_secret="totp",
    ).search_max_query_chars
    assert settings.search_rate_per_min == Settings(
        public_base_url="https://memory.example",
        mcp_dsn="postgresql://door",
        login_secret="login",
        totp_secret="totp",
    ).search_rate_per_min


def test_read_tools_has_no_write_surface():
    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=FakeConn,
        embed=fake_embed,
    )
    for attr in (
        "memory_store",
        "memory_remember",
        "_publish",
        "writer_url",
        "writer_token",
        "http_client",
        "_write_hits",
    ):
        assert not hasattr(rt, attr)


@pytest.mark.anyio
async def test_read_tools_search_get_recent_delegate_to_store(monkeypatch):
    conn = FakeConn()
    calls = []

    def retrieve(seen_conn, query, *, k, embed):
        calls.append(("retrieve", seen_conn, query, k, embed))
        return [{"hint": {"text": query}, "artefact": None, "repo_pointer": None}]

    def fetch_artefact(seen_conn, artefact_id, version):
        calls.append(("fetch_artefact", seen_conn, artefact_id, version))
        return {"artefact_id": artefact_id, "version": version}

    def recent_artefacts(seen_conn, limit):
        calls.append(("recent_artefacts", seen_conn, limit))
        return [{"artefact_id": "known", "version": 1}]

    monkeypatch.setattr("arb_memory.mcp.read_tools.store.retrieve", retrieve)
    monkeypatch.setattr("arb_memory.mcp.read_tools.store.fetch_artefact", fetch_artefact)
    monkeypatch.setattr("arb_memory.mcp.read_tools.store.recent_artefacts", recent_artefacts)

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=lambda: conn,
        embed=fake_embed,
    )

    assert await rt.memory_recent(limit=5) == [{"artefact_id": "known", "version": 1}]
    assert await rt.memory_search("anything", k=3) == [
        {"hint": {"text": "anything"}, "artefact": None, "repo_pointer": None}
    ]
    assert await rt.memory_get("known", 1) == {"artefact_id": "known", "version": 1}
    assert calls == [
        ("recent_artefacts", conn, 5),
        ("retrieve", conn, "anything", 3, fake_embed),
        ("fetch_artefact", conn, "known", 1),
    ]


@pytest.mark.anyio
async def test_read_tools_get_and_recent_normalize_binary_artefacts_for_json(monkeypatch):
    created_at = datetime(2026, 6, 26, 12, 30, tzinfo=timezone.utc)
    binary = {
        "artefact_id": "bin-1",
        "version": 1,
        "content": None,
        "content_bytes": b"\x00\xffpayload",
        "content_mime": "application/octet-stream",
        "created_at": created_at,
    }

    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.fetch_artefact",
        lambda conn, artefact_id, version: dict(binary),
    )
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.recent_artefacts",
        lambda conn, limit: [dict(binary)],
    )

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=FakeConn,
        embed=fake_embed,
    )

    got = await rt.memory_get("bin-1", 1)
    recent = await rt.memory_recent(limit=1)

    assert got["content_bytes_b64"] == "AP9wYXlsb2Fk"
    assert got["content_bytes"] is None
    assert got["created_at"] == "2026-06-26T12:30:00+00:00"
    assert recent == [got]
    json.dumps(got)
    json.dumps(recent)


@pytest.mark.anyio
async def test_read_tools_search_normalizes_linked_binary_artefact_for_json(monkeypatch):
    created_at = datetime(2026, 6, 26, 12, 45, tzinfo=timezone.utc)
    hit = {
        "hint": {
            "id": 7,
            "text": "linked binary",
            "created_at": created_at,
        },
        "artefact": {
            "artefact_id": "bin-1",
            "version": 1,
            "content": None,
            "content_bytes": b"\x00\xffpayload",
            "content_mime": "application/octet-stream",
            "created_at": created_at,
        },
        "repo_pointer": None,
    }

    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.retrieve",
        lambda conn, query, *, k, embed: [hit],
    )

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=FakeConn,
        embed=fake_embed,
    )

    result = await rt.memory_search("linked binary", k=1)

    assert result[0]["artefact"]["content_bytes_b64"] == "AP9wYXlsb2Fk"
    assert result[0]["artefact"]["content_bytes"] is None
    assert result[0]["artefact"]["created_at"] == "2026-06-26T12:45:00+00:00"
    assert result[0]["hint"]["created_at"] == "2026-06-26T12:45:00+00:00"
    json.dumps(result)


@pytest.mark.anyio
async def test_read_tools_reuses_connection_and_reconnects(monkeypatch):
    conns = [FakeConn(), FakeConn()]
    made = []

    def conn_factory():
        conn = conns[len(made)]
        made.append(conn)
        return conn

    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.recent_artefacts",
        lambda conn, limit: [{"conn": conn, "limit": limit}],
    )

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=conn_factory,
        embed=fake_embed,
    )

    assert await rt.memory_recent(limit=2) == [{"conn": conns[0], "limit": 2}]
    assert await rt.memory_recent(limit=3) == [{"conn": conns[0], "limit": 3}]
    conns[0].closed = True
    assert await rt.memory_recent(limit=4) == [{"conn": conns[1], "limit": 4}]
    assert made == conns


@pytest.mark.anyio
async def test_read_tools_clamps_recent_limit(monkeypatch):
    seen_limits = []

    def recent_artefacts(conn, limit):
        seen_limits.append(limit)
        return []

    monkeypatch.setattr("arb_memory.mcp.read_tools.store.recent_artefacts", recent_artefacts)
    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=FakeConn,
        embed=fake_embed,
    )

    await rt.memory_recent(limit=0)
    await rt.memory_recent(limit=101)

    assert seen_limits == [1, 100]


@pytest.mark.anyio
async def test_read_tools_query_length_and_rate_limit(monkeypatch):
    monkeypatch.setattr("arb_memory.mcp.read_tools.store.retrieve", lambda *args, **kwargs: [])
    rt = ReadMemoryTools(
        LocalReadSettings(
            dsn="postgresql://ignored",
            search_max_query_chars=4,
            search_rate_per_min=1,
        ),
        conn_factory=FakeConn,
        embed=fake_embed,
    )

    with pytest.raises(ValueError, match="query too long"):
        await rt.memory_search("12345")

    assert await rt.memory_search("1234") == []
    with pytest.raises(ValueError, match="search rate limit exceeded"):
        await rt.memory_search("1234")


def _rt(monkeypatch, **graph_stubs):
    rt = ReadMemoryTools(LocalReadSettings(dsn="postgresql://ignored"),
                         conn_factory=FakeConn, embed=fake_embed)
    for name, fn in graph_stubs.items():
        monkeypatch.setattr(read_tools_mod.graph, name, fn)
    return rt


@pytest.mark.anyio
async def test_memory_related_preserves_none_sentinel(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return [("other", 2, 0.1)]

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=fake_related)
    out = await rt.memory_related("subject")
    assert seen["version"] is None                    # sentinel reached graph verbatim
    assert seen["subject_hints"] == "live"
    assert out == [{"artefact_id": "other", "version": 2, "distance": 0.1}]


@pytest.mark.anyio
async def test_memory_related_explicit_version_is_as_written(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return []

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=fake_related)
    await rt.memory_related("subject", version=1)
    assert seen["version"] == 1 and seen["subject_hints"] == "as_written"


@pytest.mark.anyio
async def test_memory_related_param_validation(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=lambda *a, **k: [])
    for bad in ({"k": 0}, {"k": 21}, {"threshold": 0.0}, {"threshold": 2.1}):
        with pytest.raises(ValueError):
            await rt.memory_related("subject", **bad)


@pytest.mark.anyio
async def test_memory_related_unknown_artefact_raises(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: False)
    with pytest.raises(ValueError, match="artefact not found"):
        await rt.memory_related("ghost")
    with pytest.raises(ValueError, match="artefact not found"):
        await rt.memory_references("ghost")


@pytest.mark.anyio
async def test_graph_rate_limit_bucket_is_separate_from_search(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=lambda *a, **k: [],
             references=lambda *a, **k: {"references": [], "referenced_by": []},
             latest_version=lambda c, a: 1)
    rt.settings = LocalReadSettings(dsn="postgresql://ignored", graph_rate_per_min=2)
    await rt.memory_related("s")
    await rt.memory_references("s")
    with pytest.raises(ValueError, match="graph rate limit"):
        await rt.memory_related("s")
    assert rt._search_hits == []                      # search bucket untouched


@pytest.mark.anyio
async def test_memory_references_resolves_none_via_latest_version(monkeypatch):
    seen = {}

    def fake_refs(conn, artefact_id, version):
        seen["version"] = version
        return {"references": [], "referenced_by": []}

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             references=fake_refs, latest_version=lambda c, a: 7)
    await rt.memory_references("subject")
    assert seen["version"] == 7
    await rt.memory_references("subject", version=3)
    assert seen["version"] == 3
