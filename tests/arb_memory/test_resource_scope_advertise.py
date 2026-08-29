"""The protected-resource metadata (RFC 9728) must advertise memory.write.

Root cause of "claude.ai write blocked / memory.write scope required": the MCP SDK advertises the
protected-resource scopes_supported as required_scopes (we keep that minimal at memory.read so reads
aren't forced to carry write, and write is gated per-tool). But RFC-9728 clients (claude.ai) request
exactly the scopes the *resource* advertises — so with only memory.read advertised there, claude.ai
never requests memory.write and its token is read-only. The resource must advertise the full
valid_scopes; the transport's required_scopes stays minimal.
"""
from starlette.testclient import TestClient

from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
from arb_memory.mcp.server import build_server

S = Settings(public_base_url="https://mem.example.com", mcp_dsn="postgresql://x",
             login_secret="l", totp_secret="t")


def _server():
    provider = ArbMemoryOAuthProvider(settings=S, conn_factory=lambda: None)
    return build_server(settings=S, provider=provider, conn_factory=lambda: None)


def test_protected_resource_advertises_write_scope():
    client = TestClient(_server().streamable_http_app())
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    scopes = r.json().get("scopes_supported") or []
    assert "memory.write" in scopes, f"resource must advertise memory.write; got {scopes}"
    assert "memory.read" in scopes


def test_auth_server_metadata_still_advertises_both():
    client = TestClient(_server().streamable_http_app())
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert set(r.json().get("scopes_supported") or []) >= {"memory.read", "memory.write"}
