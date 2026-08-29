from datetime import datetime, timedelta, timezone
import uuid
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider


def _settings():
    return Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )


def _provider(conn):
    return ArbMemoryOAuthProvider(settings=_settings(), conn_factory=lambda: conn)


def _params(redirect_uri):
    return AuthorizationParams(
        state="state-1",
        scopes=["memory.read"],
        code_challenge="challenge",
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=redirect_uri is not None,
        resource="https://mem.example.com",
    )


def test_authorize_rejects_attacker_redirect_seeded_directly(scratch):
    provider = _provider(scratch)
    client_id = f"evil-{uuid.uuid4().hex}"
    oauth_store.put_client(
        scratch,
        client_id=client_id,
        redirect_uris=["https://attacker.com/cb"],
        metadata={},
    )
    client = anyio.run(provider.get_client, client_id)

    with pytest.raises(AuthorizeError) as excinfo:
        anyio.run(provider.authorize, client, _params("https://attacker.com/cb"))

    assert excinfo.value.error == "unauthorized_client"


def test_authorize_rejects_when_no_redirect_param_falls_back_to_attacker_uri(scratch):
    provider = _provider(scratch)
    client_id = f"evil-fallback-{uuid.uuid4().hex}"
    oauth_store.put_client(
        scratch,
        client_id=client_id,
        redirect_uris=["https://attacker.com/cb"],
        metadata={},
    )
    client = anyio.run(provider.get_client, client_id)
    params = AuthorizationParams.model_construct(
        state="state-1",
        scopes=["memory.read"],
        code_challenge="challenge",
        redirect_uri=None,
        redirect_uri_provided_explicitly=False,
        resource="https://mem.example.com",
    )

    with pytest.raises(AuthorizeError) as excinfo:
        anyio.run(provider.authorize, client, params)

    assert excinfo.value.error == "unauthorized_client"


def test_authorize_allows_pinned_connector_and_redirects_to_login(scratch):
    provider = _provider(scratch)
    client_id = f"claude-{uuid.uuid4().hex}"
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    oauth_store.put_client(
        scratch,
        client_id=client_id,
        redirect_uris=[redirect_uri],
        metadata={},
    )
    client = anyio.run(provider.get_client, client_id)

    url = anyio.run(provider.authorize, client, _params(redirect_uri))

    parsed = urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://mem.example.com/login"
    session_id = parse_qs(parsed.query)["session"][0]
    session = oauth_store.get_login_session(scratch, session_id)
    assert session["authorize_state"]["client_id"] == client_id
    assert session["authorize_state"]["redirect_uri"] == redirect_uri
    assert session["authorize_state"]["code_challenge"] == "challenge"
    assert session["expires_at"] > datetime.now(timezone.utc)
