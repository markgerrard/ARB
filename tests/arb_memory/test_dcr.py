from datetime import datetime, timedelta, timezone
import base64
import hashlib
import uuid
from urllib.parse import parse_qs, urlsplit

import anyio
import pyotp
import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.login import login_routes
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
from arb_memory.mcp.redirect_policy import OOB_REDIRECT_URI


RESOURCE = "https://mem.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def clean_mcp_auth(scratch):
    scratch.execute("DELETE FROM mcp_auth.auth_codes")
    scratch.execute("DELETE FROM mcp_auth.access_tokens")
    scratch.execute("DELETE FROM mcp_auth.refresh_tokens")
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.oauth_clients")
    yield
    scratch.execute("DELETE FROM mcp_auth.auth_codes")
    scratch.execute("DELETE FROM mcp_auth.access_tokens")
    scratch.execute("DELETE FROM mcp_auth.refresh_tokens")
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.oauth_clients")


def _settings(**overrides):
    values = {
        "public_base_url": RESOURCE,
        "mcp_dsn": "postgresql://example",
        "login_secret": "passphrase",
        "totp_secret": "totp",
    }
    values.update(overrides)
    return Settings(**values)


def _provider(conn, **settings):
    return ArbMemoryOAuthProvider(settings=_settings(**settings), conn_factory=lambda: conn)


def _client_info(*, redirect_uris=None, client_name="registered", scope="memory.read"):
    return OAuthClientInformationFull(
        redirect_uris=redirect_uris or [REDIRECT],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        client_name=client_name,
    )


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_register_client_persists_pinned_redirects(scratch):
    provider = _provider(scratch)
    info = _client_info()

    anyio.run(provider.register_client, info)

    assert info.client_id
    loaded = anyio.run(provider.get_client, info.client_id)
    assert loaded is not None
    assert [str(uri) for uri in loaded.redirect_uris] == [REDIRECT]


def test_register_client_creates_public_pkce_client_without_secret_cache(scratch):
    provider = _provider(scratch)
    info = _client_info()

    anyio.run(provider.register_client, info)

    row = oauth_store.get_client(scratch, info.client_id)
    loaded = anyio.run(provider.get_client, info.client_id)
    assert not hasattr(provider, "_client_secrets")
    assert info.client_secret is None
    assert row["client_secret_hash"] is None
    assert row["metadata"]["token_endpoint_auth_method"] == "none"
    assert loaded.client_secret is None
    assert loaded.token_endpoint_auth_method == "none"


def test_public_registered_client_completes_pkce_token_flow_without_secret(scratch):
    totp_secret = pyotp.random_base32()
    settings = _settings(totp_secret=totp_secret)
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    app = Starlette(routes=login_routes(provider, settings=settings, conn_factory=lambda: scratch))
    test_client = TestClient(app)
    info = _client_info()
    verifier = "verifier-1234567890"

    anyio.run(provider.register_client, info)
    client = anyio.run(provider.get_client, info.client_id)
    login_url = anyio.run(
        provider.authorize,
        client,
        AuthorizationParams(
            state="state-1",
            scopes=["memory.read"],
            code_challenge=_challenge(verifier),
            redirect_uri=REDIRECT,
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
    )
    session_id = parse_qs(urlsplit(login_url).query)["session"][0]
    session = oauth_store.get_login_session(scratch, session_id)

    response = test_client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": session["csrf_token"],
            "passphrase": "passphrase",
            "totp": pyotp.TOTP(totp_secret).now(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    loaded_code = anyio.run(provider.load_authorization_code, client, code)
    token = anyio.run(provider.exchange_authorization_code, client, loaded_code)
    assert token.access_token
    assert token.refresh_token

    # Direct regression guard for the connector_host claim added for ARB Messages'
    # generalization -- a real register->authorize->login->exchange->load_access_token round
    # trip (REDIRECT is claude.ai's real callback) must result in a loaded AccessToken carrying
    # connector_host="claude.ai", which arb_messages doors use for a low-maintenance allowlist
    # instead of the churning per-registration client_id.
    loaded_token = anyio.run(provider.load_access_token, token.access_token)
    assert loaded_token is not None
    assert loaded_token.claims == {"connector_host": "claude.ai"}


def test_oob_client_completes_full_round_trip_via_displayed_code(scratch):
    # Mirrors test_public_registered_client_completes_pkce_token_flow_without_secret above, but
    # for a client with no loopback listener: register with OOB_REDIRECT_URI, get the code from
    # the rendered page (not a redirect Location), exchange it normally.
    totp_secret = pyotp.random_base32()
    settings = _settings(totp_secret=totp_secret)
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    app = Starlette(routes=login_routes(provider, settings=settings, conn_factory=lambda: scratch))
    test_client = TestClient(app)
    info = _client_info(redirect_uris=[OOB_REDIRECT_URI])
    verifier = "verifier-1234567890"

    anyio.run(provider.register_client, info)
    client = anyio.run(provider.get_client, info.client_id)
    login_url = anyio.run(
        provider.authorize,
        client,
        AuthorizationParams(
            state="state-1",
            scopes=["memory.read"],
            code_challenge=_challenge(verifier),
            redirect_uri=OOB_REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
    )
    session_id = parse_qs(urlsplit(login_url).query)["session"][0]
    session = oauth_store.get_login_session(scratch, session_id)

    response = test_client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": session["csrf_token"],
            "passphrase": "passphrase",
            "totp": pyotp.TOTP(totp_secret).now(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 200  # not a 302 -- no listener to redirect to
    # Auth codes are stored only as a hash (mcp_auth.auth_codes.code_hash) -- there is no plaintext
    # copy anywhere except this rendered page, exactly like what a human copies out by hand. Pull
    # it from the markup the same way, rather than reaching into the DB.
    marker = '<p class="code" id="code">'
    start = response.text.index(marker) + len(marker)
    code = response.text[start : response.text.index("</p>", start)]
    assert code  # non-empty: something was actually rendered

    loaded_code = anyio.run(provider.load_authorization_code, client, code)
    token = anyio.run(provider.exchange_authorization_code, client, loaded_code)
    assert token.access_token
    assert token.refresh_token


def test_register_client_rejects_attacker_redirect(scratch):
    provider = _provider(scratch)
    info = _client_info(redirect_uris=["https://attacker.com/callback"])

    with pytest.raises(RegistrationError) as excinfo:
        anyio.run(provider.register_client, info)
    assert excinfo.value.error == "invalid_redirect_uri"


# History: a P0 found during ARB Messages generalization review (agy-print, confirmed against
# the MCP SDK source, sharpened by cold-Opus: claude.ai auto-requests every advertised scope)
# established that valid_scopes vs default_scopes alone does not stop a client from explicitly
# requesting and receiving ANY scope in valid_scopes via self-service DCR, with zero human
# review. register_client briefly stripped messages.fulfill (an operator-only capability) for
# exactly this reason. Operator decision (2026-07-02, explicit, after the ChatGPT-can-never-
# pick-up-a-non-default-scope tradeoff was surfaced): default-grant messages.fulfill instead,
# judged acceptable for this single-operator deployment where every token issuance already
# requires a personal passphrase + TOTP regardless of which client requests it -- see
# server.py's ClientRegistrationOptions comment for the full reasoning. The strip mechanism was
# removed; this test now confirms the CURRENT behavior (explicitly requesting messages.fulfill
# succeeds and is preserved, like any other scope) rather than the historical stripping.
def test_register_client_grants_messages_fulfill_like_any_other_scope(scratch):
    provider = _provider(scratch)
    info = _client_info(scope="memory.read memory.write messages.fulfill")

    anyio.run(provider.register_client, info)

    assert info.client_id
    loaded = anyio.run(provider.get_client, info.client_id)
    granted = set(loaded.scope.split())
    assert granted == {"memory.read", "memory.write", "messages.fulfill"}


def test_register_client_enforces_global_cap(scratch):
    provider = _provider(scratch, dcr_global_cap=1)
    oauth_store.put_client(scratch, client_id="existing", redirect_uris=[REDIRECT], metadata={})

    with pytest.raises(RegistrationError) as excinfo:
        anyio.run(provider.register_client, _client_info())
    assert excinfo.value.error == "invalid_client_metadata"


def test_register_client_rejects_oversized_metadata(scratch):
    provider = _provider(scratch, dcr_metadata_max_bytes=64)
    info = _client_info(client_name="x" * 200)

    with pytest.raises(RegistrationError) as excinfo:
        anyio.run(provider.register_client, info)
    assert excinfo.value.error == "invalid_client_metadata"


def test_register_client_gc_unused_clients_before_cap(scratch):
    provider = _provider(scratch, dcr_global_cap=1)
    oauth_store.put_client(scratch, client_id="stale", redirect_uris=[REDIRECT], metadata={})
    scratch.execute(
        """
        UPDATE mcp_auth.oauth_clients
        SET created_at = %s
        WHERE client_id = 'stale'
        """,
        (datetime.now(timezone.utc) - timedelta(days=2),),
    )
    info = _client_info()

    anyio.run(provider.register_client, info)

    assert oauth_store.get_client(scratch, "stale") is None
    assert oauth_store.get_client(scratch, info.client_id) is not None


def test_register_client_gc_preserves_used_old_clients(scratch):
    provider = _provider(scratch, dcr_global_cap=2)
    oauth_store.put_client(scratch, client_id="used", redirect_uris=[REDIRECT], metadata={})
    oauth_store.put_client(scratch, client_id="unused", redirect_uris=[REDIRECT], metadata={})
    scratch.execute(
        """
        UPDATE mcp_auth.oauth_clients
        SET created_at = %s
        WHERE client_id IN ('used', 'unused')
        """,
        (datetime.now(timezone.utc) - timedelta(days=2),),
    )

    assert anyio.run(provider.get_client, "used") is not None
    anyio.run(provider.register_client, _client_info())

    assert oauth_store.get_client(scratch, "used") is not None
    assert oauth_store.get_client(scratch, "unused") is None
    assert oauth_store.count_clients(scratch) == 2


def test_register_then_login_without_2fa_does_not_issue_code(scratch):
    provider = _provider(scratch)
    info = _client_info()
    anyio.run(provider.register_client, info)
    client = anyio.run(provider.get_client, info.client_id)
    params = AuthorizationParams(
        state="state-1",
        scopes=["memory.read"],
        code_challenge="challenge-1",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    login_url = anyio.run(provider.authorize, client, params)
    session_id = login_url.rsplit("session=", 1)[1]
    session = oauth_store.get_login_session(scratch, session_id)
    app = Starlette(routes=login_routes(provider, settings=_settings(), conn_factory=lambda: scratch))
    test_client = TestClient(app)

    response = test_client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": session["csrf_token"],
            "passphrase": "passphrase",
            "totp": "000000",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert scratch.execute("SELECT count(*) FROM mcp_auth.auth_codes").fetchone()[0] == 0
