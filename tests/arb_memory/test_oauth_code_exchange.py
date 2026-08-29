from datetime import datetime, timedelta, timezone
import uuid

import anyio
import pytest
from mcp.server.auth.provider import AuthorizationCode, TokenError

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider


RESOURCE = "https://mem.example.com"
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


def _client(conn, client_id):
    oauth_store.put_client(conn, client_id=client_id, redirect_uris=[REDIRECT], metadata={})
    return anyio.run(_provider(conn).get_client, client_id)


def _seed_code(conn, client_id):
    code = f"code-{uuid.uuid4().hex}"
    oauth_store.put_code(
        conn,
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        resource=RESOURCE,
        code_challenge="challenge",
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return code


def _crafted(code, client_id, *, redirect_uri=REDIRECT, resource=RESOURCE):
    return AuthorizationCode(
        code=code,
        scopes=["memory.read"],
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp(),
        client_id=client_id,
        code_challenge="challenge",
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


def test_exchange_authorization_code_is_one_use(scratch):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = _seed_code(scratch, client_id)
    loaded = anyio.run(provider.load_authorization_code, client, code)

    first = anyio.run(provider.exchange_authorization_code, client, loaded)
    assert first.access_token
    with pytest.raises(TokenError) as excinfo:
        anyio.run(provider.exchange_authorization_code, client, loaded)
    assert excinfo.value.error == "invalid_grant"


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "wrong-client"),
        ("redirect_uri", "https://chatgpt.com/connector_platform_oauth_redirect"),
        ("resource", "https://other.example.com"),
    ],
)
def test_exchange_authorization_code_rejects_binding_mismatch_without_burning_code(
    scratch, field, value
):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = _seed_code(scratch, client_id)
    kwargs = {"redirect_uri": REDIRECT, "resource": RESOURCE}
    crafted_client_id = client_id
    if field == "client_id":
        crafted_client_id = value
    else:
        kwargs[field] = value
    crafted = _crafted(code, crafted_client_id, **kwargs)

    with pytest.raises(TokenError) as excinfo:
        anyio.run(provider.exchange_authorization_code, client, crafted)
    assert excinfo.value.error == "invalid_grant"
    assert oauth_store.get_code(scratch, code) is not None

    loaded = anyio.run(provider.load_authorization_code, client, code)
    assert anyio.run(provider.exchange_authorization_code, client, loaded).access_token


def test_load_authorization_code_does_not_consume_before_sdk_pkce_check(scratch):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = _seed_code(scratch, client_id)

    loaded = anyio.run(provider.load_authorization_code, client, code)
    assert loaded is not None
    assert oauth_store.get_code(scratch, code) is not None
    assert anyio.run(provider.exchange_authorization_code, client, loaded).access_token


def test_success_mints_access_token_bound_to_resource(scratch):
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = _seed_code(scratch, client_id)
    loaded = anyio.run(provider.load_authorization_code, client, code)

    token = anyio.run(provider.exchange_authorization_code, client, loaded)

    access_row = oauth_store.get_access_token(scratch, token.access_token)
    assert access_row["resource"] == RESOURCE
    assert token.refresh_token


def test_exchange_accepts_trailing_slash_resource_from_real_client(scratch):
    # Real claude.ai sends the RFC 8707 resource indicator as the RFC 3986-canonical URI for a bare
    # authority, which carries a trailing slash (https://host/). public_base_url is stored
    # slash-stripped (load_settings), so a strict-equality audience check 400s EVERY real token
    # exchange on a spurious mismatch. Caught by the Phase 3 connector canary against real claude.ai
    # (POST /token -> 400 invalid_grant after a clean DCR + 2FA). The fix canonicalizes the resource
    # (trailing slash) at every comparison, so https://host/ == https://host.
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = f"code-{uuid.uuid4().hex}"
    oauth_store.put_code(
        scratch,
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        resource=RESOURCE + "/",  # trailing slash, exactly as the real client sends it
        code_challenge="challenge",
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    loaded = anyio.run(provider.load_authorization_code, client, code)
    token = anyio.run(provider.exchange_authorization_code, client, loaded)
    assert token.access_token
    # ...and the minted token must still validate downstream: load_access_token applies the SAME
    # public_base_url audience check, so a half-fix (exchange only) would 401 the first real call.
    assert anyio.run(provider.load_access_token, token.access_token) is not None


def test_exchange_rejects_code_minted_for_foreign_resource(scratch):
    # A code whose STORED resource is not our public_base_url (e.g. authorize accepted a
    # client-supplied foreign resource). The binding check (presented==stored) PASSES because the
    # presented resource matches the stored one; the public_base_url audience check is the only
    # thing that rejects it. Without it, a token bound to a foreign audience would be minted.
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    foreign = "https://attacker.example.com"
    code = f"code-{uuid.uuid4().hex}"
    oauth_store.put_code(
        scratch,
        code=code,
        client_id=client_id,
        redirect_uri=REDIRECT,
        resource=foreign,
        code_challenge="challenge",
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    loaded = anyio.run(provider.load_authorization_code, client, code)
    with pytest.raises(TokenError):
        anyio.run(provider.exchange_authorization_code, client, loaded)


def test_exchange_authorization_code_rolls_back_torn_write(scratch, monkeypatch):
    # Autocommit commits each write immediately; without an explicit transaction wrapping
    # consume_code + put_access + put_refresh, a failure in put_refresh leaves the code CONSUMED
    # and an access token minted with no refresh -> partial auth state (a torn rotation that
    # fails silent and surfaces days later as "connector mysteriously stopped"). The wrap must
    # make the sequence atomic so a mid-write failure rolls the whole thing back.
    provider = _provider(scratch)
    client_id = f"client-{uuid.uuid4().hex}"
    client = _client(scratch, client_id)
    code = _seed_code(scratch, client_id)
    loaded = anyio.run(provider.load_authorization_code, client, code)

    def boom(*args, **kwargs):
        raise RuntimeError("refresh write failed mid-sequence")

    monkeypatch.setattr(oauth_store, "put_refresh_token", boom)
    with pytest.raises(RuntimeError):
        anyio.run(provider.exchange_authorization_code, client, loaded)

    # rolled back: the code was NOT consumed (used_at still NULL) -> the client can retry
    used = scratch.execute(
        "SELECT used_at FROM mcp_auth.auth_codes WHERE code_hash = %s",
        (oauth_store.hash_token(code),),
    ).fetchone()
    assert used is not None and used[0] is None
