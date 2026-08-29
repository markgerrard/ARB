"""End-to-end write path: a connector (claude.ai / ChatGPT) write driven through the REAL chain —
door tool (memory_store/memory_remember) → httpx → writer proxy (ASGI) → bus (real redis) →
consumer (handle_write_intent) → store — asserting it lands searchable, plus structural fail-loud.

This is the cross-mode decorrelated check: every component is real (no fakes), so it catches
integration bugs (prefix/key mismatches, intent-shape drift, source/author plumbing) that the
per-component unit tests cannot. Gated on a live local Postgres (scratch) + Redis (redis_bus).
"""
import json

import anyio
import httpx
import pytest

from arb_memory import bus, store
from arb_memory.mcp import tools
from arb_memory.mcp.config import Settings
from arb_memory.writer import build_writer_app

S = Settings(public_base_url="https://x", mcp_dsn="postgresql://x",
             login_secret="l", totp_secret="t")
TOKEN = "e2e-writer-token"


class _AwaitBus:
    def __init__(self):
        self.calls = []

    def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields))
        return "1-0"


class _DelayedResultRedis:
    simulated_delay_seconds = 10.1

    async def blpop(self, key, timeout=0):
        assert timeout >= self.simulated_delay_seconds
        return key, json.dumps({
            "artefact_outcome": "stored",
            "artefact_id": "art-delayed",
            "version": 1,
            "hints_stored": 1,
            "duplicate": False,
        })

    async def aclose(self):
        pass


class _DelayedWriterHttp:
    """Simulates a >10-second result publication without spending 10 wall-clock seconds."""

    def __init__(self, app, delay_seconds):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://writer", timeout=10.0)
        self.delay_seconds = delay_seconds

    async def post(self, url, *, json=None, headers=None, timeout=None):
        effective_timeout = 10.0 if timeout is None else timeout
        if effective_timeout < self.delay_seconds:
            raise httpx.ReadTimeout("simulated delayed write result")
        return await self.client.post(url, json=json, headers=headers, timeout=timeout)


def _grant(monkeypatch, client_id="cid-e2e"):
    monkeypatch.setattr(
        tools, "get_access_token",
        lambda: type("T", (), {"scopes": ["memory.read", "memory.write"], "client_id": client_id})(),
    )


def _door(monkeypatch, redis_bus, conn_factory, fake_embed, writer_token=TOKEN, writer_url="http://writer"):
    """Wire a REAL door: real httpx AsyncClient over the REAL writer app on the REAL redis bus.

    Isolate the bus stream to this test's redis prefix so concurrent consumers can't interfere and
    the redis_bus fixture cleans it up."""
    stream = f"{redis_bus.prefix}arbmem:writes"
    monkeypatch.setattr(bus, "writes_stream", lambda *a, **k: stream)
    app = build_writer_app(redis_bus, token=TOKEN)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=writer_url)
    mt = tools.MemoryTools(S, conn_factory=conn_factory, embed=fake_embed,
                           writer_url=writer_url, writer_token=writer_token, http_client=client)
    return mt, stream


def _drain_one(redis_bus, conn, stream, ulid, fake_embed):
    """Find the just-published intent on the bus and run the REAL consumer logic on it."""
    entries = redis_bus.xrange(stream)
    ours = [fields for _id, fields in entries if fields.get("ulid") == ulid]
    assert ours, f"intent {ulid} not found on the bus stream {stream}"
    intent = json.loads(ours[0]["payload"])
    bus.handle_write_intent(conn, intent, embed=fake_embed)
    return intent


@pytest.mark.e2e
def test_e2e_memory_remember_lands_searchable(monkeypatch, conn_factory, redis_bus, fake_embed):
    _grant(monkeypatch)
    mt, stream = _door(monkeypatch, redis_bus, conn_factory, fake_embed)
    marker = f"e2e-remember-{redis_bus.prefix.strip(':')}"

    res = anyio.run(lambda: mt.memory_remember(f"{marker} a note from a connector", access_token="tok"))
    assert res["accepted"] is True and res["ulid"]

    conn = conn_factory()
    intent = _drain_one(redis_bus, conn, stream, res["ulid"], fake_embed)
    assert intent["hints"][0]["text"].startswith(marker)
    # author is stamped onto each hint by bus.memory_write (setdefault), not at the intent top level
    assert intent["hints"][0]["author"] == "cid-e2e"

    hits = store.retrieve(conn, marker, k=5, embed=fake_embed)
    texts = [h["hint"]["text"] for h in hits]
    assert any(t.startswith(marker) for t in texts), f"remember not searchable; got {texts}"
    # provenance stamped through the whole chain
    src = [h["hint"]["source"] for h in hits if h["hint"]["text"].startswith(marker)]
    assert src and src[0] == "mcp"


@pytest.mark.e2e
def test_e2e_memory_store_artefact_persisted_with_provenance(monkeypatch, conn_factory, redis_bus, fake_embed):
    _grant(monkeypatch)
    mt, stream = _door(monkeypatch, redis_bus, conn_factory, fake_embed)
    marker = f"e2e-store-{redis_bus.prefix.strip(':')}"

    res = anyio.run(lambda: mt.memory_store(f"{marker} artefact body", access_token="tok"))
    assert res["accepted"] is True and res["artefact_id"].startswith("art-")

    conn = conn_factory()
    _drain_one(redis_bus, conn, stream, res["ulid"], fake_embed)
    row = store.fetch_artefact(conn, res["artefact_id"], 1)
    assert row is not None and row["content"].startswith(marker)
    assert row["source"] == "mcp" and row["author"] == "cid-e2e"

    # auto-indexed: a memory_store'd artefact is now SEARCHABLE via its indexing hint
    hits = store.retrieve(conn, marker, k=5, embed=fake_embed)
    found = [h for h in hits if h["artefact"] and h["artefact"]["artefact_id"] == res["artefact_id"]]
    assert found, "stored artefact not searchable via its auto-index hint"
    assert found[0]["hint"]["metadata"].get("kind") == "artefact_index"


@pytest.mark.e2e
def test_e2e_fail_loud_when_writer_rejects(monkeypatch, conn_factory, redis_bus, fake_embed):
    # Structural fail-loud: a connector whose write the writer rejects (bad token -> 401) gets a LOUD
    # error and NO 'accepted' — never a false success.
    _grant(monkeypatch)
    mt, stream = _door(monkeypatch, redis_bus, conn_factory, fake_embed, writer_token="WRONG-token")
    with pytest.raises(RuntimeError):
        anyio.run(lambda: mt.memory_store("should fail loud", access_token="tok"))
    # nothing published
    assert redis_bus.xrange(stream) == []


@pytest.mark.e2e
def test_e2e_delayed_write_result_outlives_the_mcp_default_http_timeout(monkeypatch):
    _grant(monkeypatch)
    async_results = _DelayedResultRedis()
    app = build_writer_app(_AwaitBus(), token=TOKEN, async_redis_client=async_results)
    http = _DelayedWriterHttp(app, async_results.simulated_delay_seconds)
    mt = tools.MemoryTools(
        S,
        conn_factory=lambda: None,
        embed=lambda text: [],
        writer_url="http://writer",
        writer_token=TOKEN,
        http_client=http,
    )

    result = anyio.run(lambda: mt.memory_store("delayed result", await_result=True, access_token="tok"))

    assert result == {
        "accepted": True,
        "ulid": result["ulid"],
        "artefact_outcome": "stored",
        "artefact_id": "art-delayed",
        "version": 1,
        "hints_stored": 1,
        "duplicate": False,
    }
