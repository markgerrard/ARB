from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import uuid

import pyotp
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import Settings
from arb_memory.mcp.login import login_routes
from arb_memory.mcp.redirect_policy import OOB_REDIRECT_URI


class FakeProvider:
    def issue_authorization_code(self, authorize_state):
        return "issued-code"


@pytest.fixture(autouse=True)
def clean_login_state(scratch):
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.login_attempts")
    yield
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.login_attempts")


def _settings(totp_secret):
    return Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret=totp_secret,
    )


def _session(conn, session_id=None, csrf=None):
    session_id = session_id or f"sess-{uuid.uuid4().hex}"
    csrf = csrf or f"csrf-{uuid.uuid4().hex}"
    oauth_store.put_login_session(
        conn,
        session_id=session_id,
        csrf_token=csrf,
        authorize_state={
            "client_id": "client-1",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "state": "connector-state",
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return session_id, csrf


def _client(conn, settings):
    app = Starlette(
        routes=login_routes(
            FakeProvider(),
            settings=settings,
            conn_factory=lambda: conn,
        )
    )
    return TestClient(app)


def test_login_get_returns_csrf_and_cookie_flags(scratch):
    totp_secret = pyotp.random_base32()
    session_id, csrf = _session(scratch)
    client = _client(scratch, _settings(totp_secret))

    response = client.get(f"/login?session={session_id}")

    assert response.status_code == 200
    assert f'name="csrf_token" value="{csrf}"' in response.text
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_login_post_rejects_missing_csrf(scratch):
    totp_secret = pyotp.random_base32()
    session_id, _csrf = _session(scratch)
    client = _client(scratch, _settings(totp_secret))

    response = client.post(
        "/login",
        data={"session": session_id, "passphrase": "passphrase", "totp": "123456"},
    )

    assert response.status_code == 403


def test_wrong_2fa_increments_fail_count_and_locks(scratch):
    totp_secret = pyotp.random_base32()
    session_id, csrf = _session(scratch)
    client = _client(scratch, _settings(totp_secret))

    for expected in range(1, 5):
        response = client.post(
            "/login",
            data={
                "session": session_id,
                "csrf_token": csrf,
                "passphrase": "wrong",
                "totp": "000000",
            },
        )
        assert response.status_code == 403
        assert oauth_store.get_login_session(scratch, session_id)["fail_count"] == expected

    response = client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": csrf,
            "passphrase": "wrong",
            "totp": "000000",
        },
    )
    assert response.status_code == 429


def test_wrong_2fa_fresh_sessions_still_hit_client_rate_limit(scratch):
    totp_secret = pyotp.random_base32()
    client = _client(scratch, _settings(totp_secret))

    for _attempt in range(4):
        session_id, csrf = _session(scratch)
        response = client.post(
            "/login",
            data={
                "session": session_id,
                "csrf_token": csrf,
                "passphrase": "wrong",
                "totp": "000000",
            },
        )
        assert response.status_code == 403

    session_id, csrf = _session(scratch)
    response = client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": csrf,
            "passphrase": "wrong",
            "totp": "000000",
        },
    )

    assert response.status_code == 429


def test_success_redirects_to_bound_redirect_uri_not_request_supplied(scratch):
    totp_secret = pyotp.random_base32()
    session_id, csrf = _session(scratch)
    client = _client(scratch, _settings(totp_secret))
    code = pyotp.TOTP(totp_secret).now()

    response = client.post(
        "/login",
        data={
            "session": session_id,
            "csrf_token": csrf,
            "passphrase": "passphrase",
            "totp": code,
            "redirect_uri": "https://attacker.com/callback",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlsplit(location)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://claude.ai/api/mcp/auth_callback"
    )
    query = parse_qs(parsed.query)
    assert query["code"] == ["issued-code"]
    assert query["state"] == ["connector-state"]
    assert "arbmem_login=" in response.headers["set-cookie"]


def test_success_with_oob_redirect_uri_displays_code_instead_of_redirecting(scratch):
    totp_secret = pyotp.random_base32()
    session_id = f"sess-{uuid.uuid4().hex}"
    csrf = f"csrf-{uuid.uuid4().hex}"
    oauth_store.put_login_session(
        scratch,
        session_id=session_id,
        csrf_token=csrf,
        authorize_state={
            "client_id": "client-1",
            "redirect_uri": OOB_REDIRECT_URI,
            "state": "connector-state",
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    client = _client(scratch, _settings(totp_secret))
    code = pyotp.TOTP(totp_secret).now()

    response = client.post(
        "/login",
        data={"session": session_id, "csrf_token": csrf, "passphrase": "passphrase", "totp": code},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "issued-code" in response.text
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "arbmem_login=" in response.headers["set-cookie"]


def _session_for(conn, client_id):
    session_id = f"sess-{uuid.uuid4().hex}"
    csrf = f"csrf-{uuid.uuid4().hex}"
    oauth_store.put_login_session(
        conn,
        session_id=session_id,
        csrf_token=csrf,
        authorize_state={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "state": "connector-state",
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return session_id, csrf


def test_wrong_2fa_fresh_CLIENTS_still_hit_global_rate_limit(scratch):
    # The per-client throttle is bypassable by registering fresh DCR clients (cap 200) -> ~1000
    # 2FA attempts. A client-INDEPENDENT global ceiling must lock regardless of client_id.
    settings = Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret=pyotp.random_base32(),
        login_global_fail_cap=3,
    )
    client = _client(scratch, settings)
    last = None
    for i in range(3):
        session_id, csrf = _session_for(scratch, client_id=f"client-{i}")  # DISTINCT client each time
        last = client.post(
            "/login",
            data={"session": session_id, "csrf_token": csrf, "passphrase": "wrong", "totp": "000000"},
        )
    # Per-client counter never reached 5 (one failure per client); only the global ceiling can lock.
    assert last.status_code == 429
