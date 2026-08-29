from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import types

import pytest

from arb_memory.mcp.read_tools import LocalReadSettings, ReadMemoryTools


class FakeConn:
    def __init__(self):
        self.closed = False


def test_embed_import_is_lazy_and_embed_reuses_openai_client(monkeypatch):
    constructed = []

    class FakeEmbeddings:
        def create(self, *, model, input):
            return types.SimpleNamespace(
                data=[types.SimpleNamespace(embedding=[float(len(input))])]
            )

    class FakeOpenAI:
        def __init__(self, *, api_key):
            constructed.append(api_key)
            self.embeddings = FakeEmbeddings()

    fake_openai = types.SimpleNamespace(OpenAI=FakeOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    import arb_memory.embed as embed_module

    embed_module = importlib.reload(embed_module)

    assert constructed == []
    assert embed_module.embed("a") == [1.0]
    assert embed_module.embed("abcd") == [4.0]
    assert constructed == ["test-key"]


def test_openai_client_first_use_is_thread_safe(monkeypatch):
    constructed = []

    class FakeOpenAI:
        def __init__(self, *, api_key):
            time.sleep(0.02)
            constructed.append(api_key)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "thread-key")

    import arb_memory.embed as embed_module

    embed_module = importlib.reload(embed_module)
    clients = []
    threads = [
        threading.Thread(target=lambda: clients.append(embed_module._openai_client()))
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert constructed == ["thread-key"]
    assert len({id(client) for client in clients}) == 1


def test_run_memory_import_does_not_construct_openai_client(monkeypatch):
    constructed = []

    class FakeOpenAI:
        def __init__(self, *, api_key):
            constructed.append(api_key)

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import arb_memory.embed as embed_module
    import arb_memory.run as run_module

    importlib.reload(embed_module)
    importlib.reload(run_module)

    assert constructed == []


@pytest.mark.anyio
async def test_get_recent_work_without_openai_key_and_search_errors_cleanly(monkeypatch):
    conn = FakeConn()
    called = []

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.fetch_artefact",
        lambda seen_conn, artefact_id, version: called.append(("get", seen_conn))
        or {"artefact_id": artefact_id, "version": version},
    )
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.recent_artefacts",
        lambda seen_conn, limit: called.append(("recent", seen_conn)) or [{"limit": limit}],
    )
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.retrieve",
        lambda *args, **kwargs: pytest.fail("search should fail before store.retrieve"),
    )

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=lambda: conn,
    )

    assert await rt.memory_get("a1", 1) == {"artefact_id": "a1", "version": 1}
    assert await rt.memory_recent(limit=2) == [{"limit": 2}]
    with pytest.raises(RuntimeError, match="memory_search unavailable: OPENAI_API_KEY is not set") as exc:
        await rt.memory_search("anything")

    assert exc.value.__cause__ is None
    assert called == [("get", conn), ("recent", conn)]


@pytest.mark.anyio
async def test_injected_embed_allows_search_without_openai_key(monkeypatch):
    conn = FakeConn()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.retrieve",
        lambda seen_conn, query, *, k, embed: [{"conn": seen_conn, "query": query, "k": k, "embed": embed}],
    )

    def fake_embed(text):
        return [0.0] * 1536

    rt = ReadMemoryTools(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=lambda: conn,
        embed=fake_embed,
    )

    assert await rt.memory_search("anything", k=3) == [
        {"conn": conn, "query": "anything", "k": 3, "embed": fake_embed}
    ]


@pytest.mark.anyio
async def test_default_connection_uses_autocommit_and_reconnects(monkeypatch):
    conns = [FakeConn(), FakeConn()]
    calls = []

    def connect(dsn, *, autocommit):
        conn = conns[len(calls)]
        calls.append((dsn, autocommit, conn))
        return conn

    monkeypatch.setattr("arb_memory.mcp.read_tools.psycopg.connect", connect)
    monkeypatch.setattr(
        "arb_memory.mcp.read_tools.store.recent_artefacts",
        lambda conn, limit: [{"conn": conn}],
    )
    rt = ReadMemoryTools(LocalReadSettings(dsn="postgresql://reader"))

    assert await rt.memory_recent() == [{"conn": conns[0]}]
    assert await rt.memory_recent() == [{"conn": conns[0]}]
    conns[0].closed = True
    assert await rt.memory_recent() == [{"conn": conns[1]}]
    assert calls == [
        ("postgresql://reader", True, conns[0]),
        ("postgresql://reader", True, conns[1]),
    ]
