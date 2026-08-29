"""Resilient refresh: the /token endpoint must resolve a public client from its refresh
token when the refresh request omits or carries an unresolvable client_id.

Root cause of the "reconnect to re-authenticate every hour" bug: the MCP SDK's client-auth
runs before grant dispatch and 401s a refresh whose client_id it can't resolve (Missing/Invalid
client_id). A public connector (auth_method=none) that doesn't echo client_id on refresh therefore
can never refresh and is forced into a full re-authorize. The refresh token already identifies the
client; we inject that client_id into the form so the SDK can authenticate it.
"""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

import anyio

from arb_memory.mcp import oauth_store
from arb_memory.mcp.server import _TokenResourceMiddleware

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://mem.example.com"

# NOTE: mcp_auth.* is a single fixed schema (not the per-test temp schema), shared with any live MCP
# door on the same DB. So this file uses UNIQUE ids per test and deletes nothing — it must never wipe
# live OAuth state (clients/tokens), unlike the DELETE-everything pattern in test_dcr.py.


def _replayed_body(scratch, body: bytes) -> bytes:
    captured = {}

    async def fake_app(scope, receive, send):
        b = b""
        more = True
        while more:
            m = await receive()
            b += m.get("body", b"")
            more = m.get("more_body", False)
        captured["body"] = b

    mw = _TokenResourceMiddleware(fake_app, public_base_url=RESOURCE, conn_factory=lambda: scratch)
    scope = {"type": "http", "path": "/token", "method": "POST"}
    state = {"sent": False}

    async def receive():
        if state["sent"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        captured.setdefault("messages", []).append(message)

    anyio.run(mw, scope, receive, send)
    return captured.get("body", b"")


def _seed(scratch):
    cid = f"cid-{uuid.uuid4().hex}"
    token = f"rt-{uuid.uuid4().hex}"
    oauth_store.put_client(
        scratch, client_id=cid, redirect_uris=[REDIRECT],
        metadata={"token_endpoint_auth_method": "none"},
    )
    oauth_store.put_refresh_token(
        scratch, token=token, client_id=cid, access_token=f"at-{uuid.uuid4().hex}",
        resource=RESOURCE, scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    return cid, token


def test_refresh_missing_client_id_is_resolved_from_token(scratch):
    cid, token = _seed(scratch)
    body = urlencode({"grant_type": "refresh_token", "refresh_token": token}).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert out.get("client_id") == [cid]  # injected from the refresh token's binding


def test_refresh_unresolvable_client_id_is_replaced_from_token(scratch):
    cid, token = _seed(scratch)
    body = urlencode(
        {"grant_type": "refresh_token", "refresh_token": token, "client_id": "stale-pruned-id"}
    ).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert out.get("client_id") == [cid]  # stale id replaced with the token's real client


def test_refresh_valid_client_id_left_untouched(scratch):
    cid, token = _seed(scratch)
    body = urlencode(
        {"grant_type": "refresh_token", "refresh_token": token, "client_id": cid}
    ).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert out.get("client_id") == [cid]


def test_unknown_refresh_token_not_injected(scratch):
    body = urlencode(
        {"grant_type": "refresh_token", "refresh_token": f"no-such-{uuid.uuid4().hex}"}
    ).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert "client_id" not in out  # nothing to resolve; SDK rejects with 400 invalid_grant


def test_authorization_code_grant_body_unchanged(scratch):
    body = urlencode({"grant_type": "authorization_code", "code": "x", "resource": RESOURCE}).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert out.get("grant_type") == ["authorization_code"] and "code" in out


def test_refresh_does_not_inject_for_confidential_client(scratch):
    # defense-in-depth: a client WITH a secret must authenticate normally; never auto-resolve its
    # identity from a bearer refresh token (would let a stolen refresh token stand in for the secret).
    cid = f"cid-{uuid.uuid4().hex}"
    token = f"rt-{uuid.uuid4().hex}"
    oauth_store.put_client(
        scratch, client_id=cid, redirect_uris=[REDIRECT], client_secret="a-real-secret",
        metadata={"token_endpoint_auth_method": "client_secret_post"},
    )
    oauth_store.put_refresh_token(
        scratch, token=token, client_id=cid, access_token=f"at-{uuid.uuid4().hex}",
        resource=RESOURCE, scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    body = urlencode({"grant_type": "refresh_token", "refresh_token": token}).encode()
    out = parse_qs(_replayed_body(scratch, body).decode())
    assert "client_id" not in out  # confidential client NOT auto-resolved


def test_build_server_wires_conn_factory_from_provider(scratch):
    # deny-proof the dormant-in-prod bug: run_mcp() passes only `provider` (no conn_factory) to
    # build_server; the middleware must still get a working conn_factory derived from the provider.
    from arb_memory.mcp.config import Settings
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = Settings(
        public_base_url=RESOURCE, mcp_dsn="postgresql://example",
        login_secret="passphrase", totp_secret="totp",
    )
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    server = build_server(settings=settings, provider=provider)
    assert server._conn_factory is not None  # None under the dormant bug
    assert server._conn_factory() is scratch  # and it's the provider's real factory
