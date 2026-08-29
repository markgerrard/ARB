from dataclasses import replace

import anyio
import pytest
from starlette.testclient import TestClient

from arb_memory import store
from arb_memory.mcp.config import Settings


RESOURCE = "https://mem.example.com"


def _settings(**overrides):
    values = {
        "public_base_url": RESOURCE,
        "mcp_dsn": "postgresql://example",
        "login_secret": "passphrase",
        "totp_secret": "totp",
    }
    values.update(overrides)
    return Settings(**values)


def _seed_memory(conn, fake_embed):
    store.write_artefact_and_hints(
        conn,
        artefact={
            "artefact_id": "doc-1",
            "content": "alpha content",
            "mime": "text/plain",
        },
        hints=[
            {
                "text": "alpha memory",
                "embedding": fake_embed("alpha memory"),
                "artefact_id": "doc-1",
                "artefact_version": 1,
                "metadata": {"tags": ["alpha"]},
            }
        ],
    )


async def _search(tools, query, k, token):
    return await tools.memory_search(query, k, access_token=token)


def test_memory_tools_search_get_and_recent(scratch, fake_embed):
    from arb_memory.mcp.tools import MemoryTools

    _seed_memory(scratch, fake_embed)
    tools = MemoryTools(_settings(), conn_factory=lambda: scratch, embed=fake_embed)

    hits = anyio.run(_search, tools, "alpha", 3, "token-1")
    assert hits
    assert hits[0]["hint"]["text"] == "alpha memory"

    artefact = anyio.run(tools.memory_get, "doc-1", 1)
    assert artefact["content"] == "alpha content"

    recent = anyio.run(tools.memory_recent, 5)
    assert recent[0]["artefact_id"] == "doc-1"


def test_memory_recent_clamps_unbounded_limit(monkeypatch, scratch, fake_embed):
    from arb_memory.mcp import tools as tools_module
    from arb_memory.mcp.tools import MemoryTools

    seen_limits = []

    def fake_recent(_conn, limit):
        seen_limits.append(limit)
        return [{"artefact_id": str(i)} for i in range(min(limit, 101))]

    monkeypatch.setattr(tools_module.store, "recent_artefacts", fake_recent)
    tools = MemoryTools(_settings(), conn_factory=lambda: scratch, embed=fake_embed)

    recent = anyio.run(tools.memory_recent, 10**9)

    assert seen_limits == [100]
    assert len(recent) <= 100


def test_memory_search_rejects_long_query_before_embedding(scratch):
    from arb_memory.mcp.tools import MemoryTools

    def fail_embed(_text):
        raise AssertionError("embed should not run for oversized queries")

    tools = MemoryTools(
        replace(_settings(), search_max_query_chars=4),
        conn_factory=lambda: scratch,
        embed=fail_embed,
    )

    with pytest.raises(ValueError, match="query too long"):
        anyio.run(_search, tools, "too long", 3, "token-1")


def test_memory_search_rate_limit_is_per_token(scratch, fake_embed):
    from arb_memory.mcp.tools import MemoryTools

    tools = MemoryTools(
        replace(_settings(), search_rate_per_min=2),
        conn_factory=lambda: scratch,
        embed=fake_embed,
    )

    anyio.run(_search, tools, "alpha", 3, "token-1")
    anyio.run(_search, tools, "alpha", 3, "token-1")
    with pytest.raises(ValueError, match="rate limit"):
        anyio.run(_search, tools, "alpha", 3, "token-1")
    anyio.run(_search, tools, "alpha", 3, "token-2")


def test_build_server_wires_auth_settings_and_tools(scratch, fake_embed):
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = _settings()
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    server = build_server(
        settings=settings,
        provider=provider,
        conn_factory=lambda: scratch,
        embed=fake_embed,
    )

    auth = server.settings.auth
    assert str(auth.issuer_url).rstrip("/") == RESOURCE
    assert str(auth.resource_server_url).rstrip("/") == RESOURCE
    assert auth.client_registration_options.enabled is True
    expected_scopes = [
        "memory.read",
        "memory.write",
        "files.read",
        "files.write",
        "email.send",
        "messages.request",
        "messages.fulfill",
    ]
    assert auth.client_registration_options.valid_scopes == expected_scopes
    assert auth.client_registration_options.default_scopes == expected_scopes
    assert auth.revocation_options.enabled is True
    assert auth.required_scopes == ["memory.read"]
    tool_names = {tool.name for tool in anyio.run(server.list_tools)}
    assert {
        "memory_search",
        "memory_get",
        "memory_recent",
        "memory_related",
        "memory_references",
        "memory_store",
        "memory_remember",
    }.issubset(tool_names)


def test_unauthenticated_mcp_request_returns_prm_challenge(scratch, fake_embed):
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = _settings()
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    server = build_server(
        settings=settings,
        provider=provider,
        conn_factory=lambda: scratch,
        embed=fake_embed,
    )

    with TestClient(server.streamable_http_app()) as client:
        response = client.get("/")

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert 'resource_metadata="https://mem.example.com/.well-known/oauth-protected-resource"' in challenge


def test_registered_memory_store_wrapper_forwards_await_result(monkeypatch):
    from arb_memory.mcp.config import Settings
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server
    from arb_memory.mcp.tools import MemoryTools

    settings = Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://unused",
        login_secret="login",
        totp_secret="totp",
    )
    calls = []

    async def memory_store(
        self,
        content,
        *,
        artefact_id=None,
        mime="text/plain",
        await_result=False,
        expected_version=None,
    ):
        calls.append((content, artefact_id, mime, await_result, expected_version))
        return {"accepted": True, "ulid": "u", "artefact_id": "art", "artefact_outcome": "stored"}

    monkeypatch.setattr(MemoryTools, "memory_store", memory_store)
    server = build_server(
        settings=settings,
        provider=ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: None),
        conn_factory=lambda: None,
        embed=lambda text: [],
    )

    anyio.run(lambda: server.call_tool("memory_store", {"content": "hello", "await_result": True}))

    assert calls == [("hello", None, "text/plain", True, None)]

    # ARB-B19(c): the registration wrapper must forward expected_version too. A
    # parameter added only to MemoryTools never reaches clients (recorded failure:
    # docs/superpowers/plans/2026-07-13-item2-write-result-plan.md:37-40), so assert
    # the value ARRIVES rather than merely that the call does not raise.
    calls.clear()
    anyio.run(
        lambda: server.call_tool(
            "memory_store",
            {"content": "hi", "await_result": True, "artefact_id": "doc", "expected_version": 7},
        )
    )
    assert calls == [("hi", "doc", "text/plain", True, 7)]
