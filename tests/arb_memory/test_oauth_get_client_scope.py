"""get_client must return the client's REGISTERED scope, not a hardcoded one.

Regression: get_client hardcoded scope="memory.read", so the MCP SDK's /authorize handler
(client.validate_scope) rejected any request for memory.write — even from a client that registered
with it — with invalid_scope ("Client was not registered with scope memory.write"). That bounced the
OAuth flow back to the connector before /login, so the passphrase+TOTP page never rendered. The
registered scope lives in the client's stored metadata; get_client must honour it.
"""
import uuid

import anyio

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider

REDIRECT = "https://chatgpt.com/connector/oauth/test"
SETTINGS = Settings(public_base_url="https://mem.example.com", mcp_dsn="postgresql://x",
                    login_secret="l", totp_secret="t")


def _provider(scratch):
    return ArbMemoryOAuthProvider(settings=SETTINGS, conn_factory=lambda: scratch)


def _seed_client(scratch, scope):
    cid = f"cid-{uuid.uuid4().hex}"
    oauth_store.put_client(
        scratch, client_id=cid, redirect_uris=[REDIRECT],
        metadata={"token_endpoint_auth_method": "none", "scope": scope},
    )
    return cid


def test_get_client_returns_registered_write_scope(scratch):
    cid = _seed_client(scratch, "memory.read memory.write")
    client = anyio.run(lambda: _provider(scratch).get_client(cid))
    assert client is not None
    # the client registered for read+write; get_client must report both, or /authorize rejects write
    assert set((client.scope or "").split()) == {"memory.read", "memory.write"}


def test_get_client_preserves_read_only_scope(scratch):
    cid = _seed_client(scratch, "memory.read")
    client = anyio.run(lambda: _provider(scratch).get_client(cid))
    assert (client.scope or "").split() == ["memory.read"]


def test_get_client_validate_scope_accepts_registered_write(scratch):
    # drive the SDK validator the /authorize handler uses (the actual rejection site)
    cid = _seed_client(scratch, "memory.read memory.write")
    client = anyio.run(lambda: _provider(scratch).get_client(cid))
    assert client.validate_scope("memory.read memory.write") is not None  # no InvalidScopeError
