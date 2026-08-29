from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import re
import socket
import threading
import time
import uuid

import anyio
import httpx
import pyotp
import pytest
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.mcp.login import _GLOBAL_KEY as DOOR_GLOBAL_KEY
from arb_memory.visibility import VISIBILITY_GLOBAL_KEY
from arb_memory.visibility import build_visibility_app


RESOURCE = "https://mem.example.com"
PASSPHRASE = "passphrase"
TOTP_SECRET = pyotp.random_base32()


class FakeRedis:
    def xrevrange(self, key, count=200):
        return [
            ("2-0", {"orchestrator": "orch-1", "run_id": "run-1"}),
            ("1-0", {"orchestrator": "orch-2", "run_id": "run-2"}),
        ]


class FakeAsyncRedis:
    async def xrange(self, key, count=200):
        return []

    async def xread(self, streams, block=1000):
        await asyncio.sleep(0.01)
        return []

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def clean_login_state(scratch):
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.login_attempts")
    yield
    scratch.execute("DELETE FROM mcp_auth.login_sessions")
    scratch.execute("DELETE FROM mcp_auth.login_attempts")


def _enable_login(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", PASSPHRASE)
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", TOTP_SECRET)


def _disable_login(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_MCP_LOGIN_SECRET", raising=False)
    monkeypatch.delenv("ARB_MEMORY_MCP_TOTP_SECRET", raising=False)


def _client(monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    return TestClient(app, base_url=RESOURCE)


def _app(monkeypatch, *, re_validate_s=0.05):
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr("arb_memory.visibility.aioredis.from_url", lambda *args, **kwargs: FakeAsyncRedis())
    return build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
        re_validate_s=re_validate_s,
    )


def _login_form(client):
    response = client.get("/login")
    assert response.status_code == 200
    session = re.search(r'name="session" value="([^"]+)"', response.text).group(1)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
    return response, session, csrf


def _put_access_token(conn):
    token = f"vis-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return token


def _put_verified_session(conn, *, expires_at):
    session_id = f"vis-{uuid.uuid4().hex}"
    oauth_store.put_login_session(
        conn,
        session_id=session_id,
        csrf_token=f"csrf-{uuid.uuid4().hex}",
        authorize_state={"client_id": "visibility", "redirect_uri": "/"},
        expires_at=expires_at,
    )
    conn.execute(
        "UPDATE mcp_auth.login_sessions SET verified_at = %s WHERE session_id = %s",
        (datetime.now(timezone.utc), session_id),
    )
    return session_id


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(app):
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("uvicorn test server did not start")
    return f"http://127.0.0.1:{port}", server, thread


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


def test_root_redirects_to_login_without_session_or_token(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_login_get_serves_page_sets_cookie_and_csrf(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)

    response, _session, csrf = _login_form(client)

    assert "Authorize this connection" in response.text
    assert f'name="csrf_token" value="{csrf}"' in response.text
    cookie = response.headers["set-cookie"]
    assert "arbmem_login=" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()


def test_login_success_sets_verified_session_and_allows_root(scratch, monkeypatch):
    monkeypatch.setenv("ARB_VIS_SESSION_TTL_SECS", "28800")
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    _response, session, csrf = _login_form(client)

    login = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": PASSPHRASE,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
        follow_redirects=False,
    )

    assert login.status_code == 302
    assert login.headers["location"] == "/"
    cookie = login.headers["set-cookie"]
    assert "arbmem_login=" in cookie
    assert "Max-Age=28800" in cookie
    verified_session = re.search(r"arbmem_login=([^;]+)", cookie).group(1)
    assert verified_session != session
    assert client.get("/").status_code == 200
    assert oauth_store.get_login_session(scratch, session) is None
    stored = oauth_store.get_login_session(scratch, verified_session)
    assert stored["verified_at"] is not None
    assert stored["expires_at"] > datetime.now(timezone.utc) + timedelta(hours=7)


def test_login_wrong_credentials_increment_and_lock(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    _response, session, csrf = _login_form(client)

    for expected in range(1, 5):
        response = client.post(
            "/login",
            data={
                "session": session,
                "csrf_token": csrf,
                "passphrase": "wrong",
                "totp": "000000",
            },
        )
        assert response.status_code == 403
        assert oauth_store.get_login_session(scratch, session)["fail_count"] == expected
        assert client.get("/", follow_redirects=False).status_code == 302

    response = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": "wrong",
            "totp": "000000",
        },
    )
    assert response.status_code == 429


def test_login_csrf_mismatch_is_forbidden(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    _response, session, _csrf = _login_form(client)

    response = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": "not-the-csrf",
            "passphrase": PASSPHRASE,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
    )

    assert response.status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 302


def test_bearer_token_still_authenticates_without_cookie(scratch, monkeypatch):
    _enable_login(monkeypatch)
    token = _put_access_token(scratch)
    client = _client(monkeypatch)

    response = client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"orchestrators": ["orch-1", "orch-2"], "tees": []}


def test_login_secrets_unset_disables_gate_and_rejects_login(scratch, monkeypatch):
    _disable_login(monkeypatch)
    client = _client(monkeypatch)

    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/login").status_code == 404
    response = client.post(
        "/login",
        data={
            "session": "fake",
            "csrf_token": "fake",
            "passphrase": PASSPHRASE,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
    )
    assert response.status_code == 403


def test_deny_proof_wrong_totp_rejected_even_with_correct_passphrase(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    _response, session, csrf = _login_form(client)

    response = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": PASSPHRASE,
            "totp": "000000",
        },
    )

    assert response.status_code == 403
    assert oauth_store.get_login_session(scratch, session)["verified_at"] is None
    assert client.get("/", follow_redirects=False).status_code == 302


def test_verified_session_expiry_controls_root_access(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    expired = _put_verified_session(scratch, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    fresh = _put_verified_session(scratch, expires_at=datetime.now(timezone.utc) + timedelta(hours=8))

    client.cookies.set("arbmem_login", expired, domain="mem.example.com")
    assert client.get("/", follow_redirects=False).status_code == 302

    client.cookies.set("arbmem_login", fresh, domain="mem.example.com")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_visibility_session_ttl_extends_verified_session_beyond_login_ttl(scratch, monkeypatch):
    monkeypatch.setenv("ARB_VIS_SESSION_TTL_SECS", "7200")
    _enable_login(monkeypatch)
    client = _client(monkeypatch)
    _response, session, csrf = _login_form(client)

    login = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": PASSPHRASE,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
        follow_redirects=False,
    )

    verified_session = re.search(r"arbmem_login=([^;]+)", login.headers["set-cookie"]).group(1)
    stored = oauth_store.get_login_session(scratch, verified_session)
    remaining = (stored["expires_at"] - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 6900


def test_visibility_global_fail_bucket_isolated_from_door_global_bucket(scratch, monkeypatch):
    _enable_login(monkeypatch)
    client = _client(monkeypatch)

    for _attempt in range(20):
        oauth_store.bump_global_login_fail(scratch, DOOR_GLOBAL_KEY, window_seconds=300)
    _response, session, csrf = _login_form(client)
    response = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": "wrong",
            "totp": "000000",
        },
    )
    assert response.status_code == 403
    assert oauth_store.get_global_login_fail_count(scratch, DOOR_GLOBAL_KEY, window_seconds=300) == 20
    assert oauth_store.get_global_login_fail_count(scratch, VISIBILITY_GLOBAL_KEY, window_seconds=300) == 1

    scratch.execute("DELETE FROM mcp_auth.login_attempts")
    for _attempt in range(20):
        oauth_store.bump_global_login_fail(scratch, VISIBILITY_GLOBAL_KEY, window_seconds=300)
    _response, session, csrf = _login_form(client)
    response = client.post(
        "/login",
        data={
            "session": session,
            "csrf_token": csrf,
            "passphrase": PASSPHRASE,
            "totp": pyotp.TOTP(TOTP_SECRET).now(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 429
    assert oauth_store.get_global_login_fail_count(scratch, VISIBILITY_GLOBAL_KEY, window_seconds=300) == 20
    assert oauth_store.get_global_login_fail_count(scratch, DOOR_GLOBAL_KEY, window_seconds=300) == 0


async def _session_sse_closes_after_expiry(base_url, session_id, conn):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "GET",
            f"{base_url}/sse/orchestrator/orch-1",
            headers={"Cookie": f"arbmem_login={session_id}"},
        ) as response:
            assert response.status_code == 200
            await asyncio.sleep(0.15)
            assert not response.is_closed
            conn.execute(
                "UPDATE mcp_auth.login_sessions SET expires_at = %s WHERE session_id = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), session_id),
            )

            async def read_to_eof():
                async for _line in response.aiter_lines():
                    pass

            await asyncio.wait_for(read_to_eof(), timeout=2)


def test_session_cookie_sse_revalidates_and_closes_after_session_expiry(scratch, monkeypatch):
    _enable_login(monkeypatch)
    session_id = _put_verified_session(scratch, expires_at=datetime.now(timezone.utc) + timedelta(hours=8))
    base_url, server, thread = _start_server(_app(monkeypatch, re_validate_s=0.05))
    try:
        anyio.run(lambda: _session_sse_closes_after_expiry(base_url, session_id, scratch))
    finally:
        _stop_server(server, thread)
