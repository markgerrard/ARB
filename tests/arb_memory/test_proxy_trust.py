from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
from mcp.server.auth.provider import AuthorizationParams
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
from arb_memory.mcp.server import build_server


RESOURCE = "https://mem.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HOSTILE = {
    "host": "attacker.com",
    "x-forwarded-host": "attacker.com",
    "x-forwarded-proto": "http",
}


def _settings():
    return Settings(
        public_base_url=RESOURCE,
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )


def _server(conn):
    settings = _settings()
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: conn)
    return settings, provider, build_server(
        settings=settings,
        provider=provider,
        conn_factory=lambda: conn,
        embed=lambda _text: [],
    )


def test_metadata_401_authorize_and_minted_token_ignore_hostile_proxy_headers(scratch):
    settings, provider, server = _server(scratch)
    oauth_store.put_client(scratch, client_id="client-1", redirect_uris=[REDIRECT], metadata={})

    with TestClient(server.streamable_http_app()) as client:
        prm = client.get("/.well-known/oauth-protected-resource", headers=HOSTILE)
        asm = client.get("/.well-known/oauth-authorization-server", headers=HOSTILE)
        unauth = client.get("/", headers=HOSTILE)
        authorize = client.get(
            "/authorize?"
            + urlencode(
                {
                    "client_id": "client-1",
                    "redirect_uri": REDIRECT,
                    "response_type": "code",
                    "code_challenge": "challenge-1",
                    "code_challenge_method": "S256",
                    "scope": "memory.read",
                    "resource": RESOURCE,
                    "state": "state-1",
                }
            ),
            headers=HOSTILE,
            follow_redirects=False,
        )

    assert prm.json()["resource"] == settings.public_base_url
    assert prm.json()["authorization_servers"] == [settings.public_base_url]
    assert asm.json()["issuer"] == settings.public_base_url
    assert asm.json()["authorization_endpoint"] == f"{settings.public_base_url}/authorize"
    assert unauth.status_code == 401
    assert (
        f'resource_metadata="{settings.public_base_url}/.well-known/oauth-protected-resource"'
        in unauth.headers["www-authenticate"]
    )
    assert authorize.status_code == 302
    assert authorize.headers["location"].startswith(f"{settings.public_base_url}/login?")

    session_id = parse_qs(urlsplit(authorize.headers["location"]).query)["session"][0]
    session = oauth_store.get_login_session(scratch, session_id)
    assert session["authorize_state"]["resource"] == settings.public_base_url
    code = provider.issue_authorization_code(session["authorize_state"])
    client_info = anyio.run(provider.get_client, "client-1")
    loaded = anyio.run(provider.load_authorization_code, client_info, code)
    token = anyio.run(provider.exchange_authorization_code, client_info, loaded)

    access_row = oauth_store.get_access_token(scratch, token.access_token)
    assert access_row["resource"] == settings.public_base_url


def test_build_server_allows_public_host_for_dns_rebinding():
    # The streamable-HTTP transport applies DNS-rebinding protection POST-auth: an authenticated
    # request whose Host is not allow-listed is rejected with 421 "Invalid Host header", which a
    # connector surfaces as a generic post-OAuth "authorization failed". We bind 0.0.0.0 in the
    # container, so the SDK does NOT auto-allow the public host (it only auto-allows localhost) —
    # build_server must allow it explicitly from public_base_url. Reverting this leaves the SDK
    # localhost-only default, so "mem.example.com" drops out of allowed_hosts and this reds.
    settings = Settings(
        public_base_url=RESOURCE,
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )
    server = build_server(settings=settings, conn_factory=lambda: None, embed=lambda _text: [])
    ts = server.settings.transport_security
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    assert "mem.example.com" in ts.allowed_hosts


def test_as_metadata_advertises_none_for_public_clients(scratch):
    # Our DCR registers clients as PUBLIC (token_endpoint_auth_method="none", no secret), but the SDK
    # hardcodes token_endpoint_auth_methods_supported to secret-based methods only. A strict OAuth client
    # (claude.ai) registers, gets a token, then aborts post-/token because the metadata claims the server
    # doesn't support the client's own "none" auth method — the Phase 3 connector-canary failure. The
    # metadata must advertise "none". Reverting the injection drops it and this reds.
    from starlette.testclient import TestClient

    _settings_obj, _provider, server = _server(scratch)
    with TestClient(server.streamable_http_app()) as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "none" in meta["token_endpoint_auth_methods_supported"]


def test_server_stays_stateful_chatgpt_requires_session_id():
    # STATEFUL by design. We tried stateless_http+json_response (to smooth ChatGPT's distributed backend)
    # and it BROKE ChatGPT live: stateless returns no Mcp-Session-Id, which ChatGPT's connector requires —
    # its requests still 200'd but it couldn't maintain the connection. Reverted. This pins the decision so
    # the flags aren't re-flipped without a full three-connector re-verify. (Connector-canary follow-up.)
    settings = Settings(
        public_base_url=RESOURCE,
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )
    server = build_server(settings=settings, conn_factory=lambda: None, embed=lambda _text: [])
    assert server.settings.stateless_http is False
    assert server.settings.json_response is False


def test_sse_app_accepts_positional_mount_path(scratch):
    # FastMCP.run_sse_async calls self.sse_app(mount_path) POSITIONALLY. Our PinnedBaseFastMCP override
    # must accept+forward it or the server crash-loops at startup (TypeError) and cloudflared 502s — a
    # crash invisible to tests that call sse_app() with no args. This calls it the way the runtime does.
    _settings_obj, _provider, server = _server(scratch)
    app = server.sse_app("/")  # must not raise
    assert any(getattr(r, "path", None) == "/sse" for r in app.routes)


def test_direct_authorize_uses_public_base_url_for_login_redirect(scratch):
    settings, provider, _server_obj = _server(scratch)
    oauth_store.put_client(scratch, client_id="client-2", redirect_uris=[REDIRECT], metadata={})
    client_info = anyio.run(provider.get_client, "client-2")
    params = AuthorizationParams(
        state="state-1",
        scopes=["memory.read"],
        code_challenge="challenge-1",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )

    login_url = anyio.run(provider.authorize, client_info, params)

    assert login_url.startswith(f"{settings.public_base_url}/login?")
