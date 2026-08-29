from datetime import datetime, timedelta, timezone
import uuid

import anyio
import pytest
from mcp.server.auth.provider import TokenError

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider


RESOURCE = "https://mem.example.com"
FOREIGN_RESOURCE = "https://other.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _settings():
    return Settings(
        public_base_url=RESOURCE,
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )


def _provider(conn):
    return ArbMemoryOAuthProvider(settings=_settings(), conn_factory=lambda: conn)


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _client(conn, client_id):
    oauth_store.put_client(conn, client_id=client_id, redirect_uris=[REDIRECT], metadata={})
    return anyio.run(_provider(conn).get_client, client_id)


def test_load_access_token_rejects_revoked_and_foreign_resource_tokens(scratch):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    access_token = f"access-{uuid.uuid4().hex}"
    foreign_token = f"foreign-access-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        scratch,
        token=access_token,
        client_id=client_id,
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=_future(),
    )
    oauth_store.put_access_token(
        scratch,
        token=foreign_token,
        client_id=client_id,
        resource=FOREIGN_RESOURCE,
        scopes=["memory.read"],
        expires_at=_future(),
    )

    loaded = anyio.run(provider.load_access_token, access_token)
    assert loaded is not None
    assert loaded.resource == RESOURCE
    assert anyio.run(provider.load_access_token, foreign_token) is None

    anyio.run(provider.revoke_token, loaded)
    assert anyio.run(provider.load_access_token, access_token) is None


def test_exchange_refresh_token_rotates_and_reuse_revokes_family(scratch):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    old_access = f"access-old-{uuid.uuid4().hex}"
    old_refresh = f"refresh-old-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        scratch,
        token=old_access,
        client_id=client_id,
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=_future(),
    )
    oauth_store.put_refresh_token(
        scratch,
        token=old_refresh,
        client_id=client_id,
        access_token=old_access,
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=_future(),
    )

    loaded_refresh = anyio.run(provider.load_refresh_token, client, old_refresh)
    rotated = anyio.run(provider.exchange_refresh_token, client, loaded_refresh, ["memory.read"])
    assert rotated.access_token != old_access
    assert rotated.refresh_token != old_refresh
    assert anyio.run(provider.load_refresh_token, client, rotated.refresh_token) is not None

    with pytest.raises(TokenError) as excinfo:
        anyio.run(provider.exchange_refresh_token, client, loaded_refresh, ["memory.read"])
    assert excinfo.value.error == "invalid_grant"

    assert anyio.run(provider.load_refresh_token, client, old_refresh) is None
    assert anyio.run(provider.load_refresh_token, client, rotated.refresh_token) is None
    assert anyio.run(provider.load_access_token, old_access) is None
    assert anyio.run(provider.load_access_token, rotated.access_token) is None
