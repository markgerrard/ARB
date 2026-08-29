import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import anyio
import pyotp
import pytest
from starlette.testclient import TestClient

from arb_memory import store
from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
from arb_memory.mcp.server import build_server
from arb_memory.mcp.tools import MemoryTools


RESOURCE = "https://mem.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def clean_mcp_auth(scratch):
    scratch.execute("DELETE FROM mcp_auth.auth_codes")
    scratch.execute("DELETE FROM mcp_auth.access_tokens")
    scratch.execute("DELETE FROM mcp_auth.refresh_tokens")
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.oauth_clients")


def _settings(totp_secret):
    return Settings(
        public_base_url=RESOURCE,
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret=totp_secret,
    )


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _search(tools, query, k, token):
    return await tools.memory_search(query, k, access_token=token)


@pytest.mark.e2e
def test_phase3_dcr_login_token_and_authenticated_memory_search(scratch, fake_embed):
    """Local protocol proof; real claude.ai/ChatGPT connector compatibility is a pre-go-live canary."""
    totp_secret = pyotp.random_base32()
    settings = _settings(totp_secret)
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: scratch)
    server = build_server(
        settings=settings,
        provider=provider,
        conn_factory=lambda: scratch,
        embed=fake_embed,
    )
    store.write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "e2e-doc", "content": "phase three memory"},
        hints=[
            {
                "text": "phase three searchable memory",
                "embedding": fake_embed("phase three searchable memory"),
                "artefact_id": "e2e-doc",
                "artefact_version": 1,
            }
        ],
    )
    verifier = "verifier-1234567890"

    with TestClient(server.streamable_http_app()) as client:
        attacker = client.post(
            "/register",
            json={
                "redirect_uris": ["https://attacker.com/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "memory.read",
            },
        )
        assert attacker.status_code == 400

        registered = client.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "memory.read",
            },
        )
        assert registered.status_code == 201
        client_info = registered.json()

        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_info["client_id"],
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "scope": "memory.read",
                "resource": RESOURCE,
                "state": "state-1",
            },
            follow_redirects=False,
        )
        assert authorize.status_code == 302

        login_path = urlsplit(authorize.headers["location"]).path
        login_query = urlsplit(authorize.headers["location"]).query
        login_get = client.get(f"{login_path}?{login_query}")
        assert login_get.status_code == 200
        session_id = parse_qs(login_query)["session"][0]
        session = oauth_store.get_login_session(scratch, session_id)

        login_post = client.post(
            "/login",
            data={
                "session": session_id,
                "csrf_token": session["csrf_token"],
                "passphrase": "passphrase",
                "totp": pyotp.TOTP(totp_secret).now(),
            },
            follow_redirects=False,
        )
        assert login_post.status_code == 302
        code = parse_qs(urlsplit(login_post.headers["location"]).query)["code"][0]

        missing_resource = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_info["client_id"],
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
            },
        )
        assert missing_resource.status_code == 400
        assert missing_resource.json()["error"] == "invalid_target"

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_info["client_id"],
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "resource": RESOURCE,
            },
        )
        assert token.status_code == 200
        token_body = token.json()

        unauth = client.get("/")
        assert unauth.status_code == 401

    loaded = anyio.run(provider.load_access_token, token_body["access_token"])
    assert loaded is not None
    access_row = oauth_store.get_access_token(scratch, token_body["access_token"])
    assert access_row["resource"] == RESOURCE
    tools = MemoryTools(settings, conn_factory=lambda: scratch, embed=fake_embed)
    hits = anyio.run(_search, tools, "phase three", 3, token_body["access_token"])
    assert hits[0]["artefact"]["artefact_id"] == "e2e-doc"
