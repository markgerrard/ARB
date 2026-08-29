from datetime import datetime, timedelta, timezone
import os
import uuid

import anyio
import pytest
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app


RESOURCE = "https://mem.example.com"
FOREIGN_RESOURCE = "https://other.example.com"
requires_memory_dsn = pytest.mark.skipif("ARB_MEMORY_DSN" not in os.environ, reason="no ARB_MEMORY_DSN")


class FakeRedis:
    def __init__(self):
        self.calls = []

    def xrevrange(self, key, count=200):
        self.calls.append((key, count))
        return [
            ("3-0", {"orchestrator": "claude-bridge-dev", "run_id": "run-1"}),
            ("2-0", {"orchestrator": "claude-bridge-dev", "run_id": "run-1"}),
            ("1-0", {"orchestrator": "other-orch", "run_id": "run-2"}),
            ("0-0", {"run_id": "run-ignored"}),
        ]


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _past():
    return datetime.now(timezone.utc) - timedelta(minutes=10)


def _token(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def _put_access_token(conn, *, resource=RESOURCE, expires_at=None):
    token = _token("access")
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=resource,
        scopes=["memory.read"],
        expires_at=expires_at or _future(),
    )
    return token


def _app_client(monkeypatch):
    fake = FakeRedis()

    def from_url(url, **kwargs):
        # **kwargs on purpose. This stands in for redis.from_url, whose keywords come from
        # redis_conn.connect_kwargs() and grow as the baseline hardens — a closed keyword-only
        # signature here turned every test in this file into a TypeError at construction time
        # the moment socket_keepalive was added. Assert only what THIS call site chooses to
        # override; that the inherited baseline survives the override is asserted by
        # test_redis_conn.test_call_sites_may_shorten_timeouts_but_not_drop_the_health_check,
        # which owns it for this call site by name.
        assert url == "redis://bridge-bus"
        assert kwargs["decode_responses"] is True
        # VIS-2: fail-fast socket timeouts so a black-holed bus can't hang a worker thread
        assert kwargs["socket_connect_timeout"] == 5
        assert kwargs["socket_timeout"] == 5
        return fake

    monkeypatch.setattr("redis.from_url", from_url)
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    return TestClient(app), fake


@requires_memory_dsn
def test_orchestrators_401_without_token(monkeypatch):
    client, _fake = _app_client(monkeypatch)

    assert client.get("/orchestrators").status_code == 401


def test_orchestrators_401_with_foreign_resource_token(scratch, monkeypatch):
    token = _put_access_token(scratch, resource=FOREIGN_RESOURCE)
    client, _fake = _app_client(monkeypatch)

    assert client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_orchestrators_401_with_revoked_and_expired_tokens(scratch, monkeypatch):
    revoked = _put_access_token(scratch)
    oauth_store.revoke_access_token(scratch, revoked)
    expired = _put_access_token(scratch, expires_at=_past())
    client, _fake = _app_client(monkeypatch)

    assert client.get("/orchestrators", headers={"Authorization": f"Bearer {revoked}"}).status_code == 401
    assert client.get("/orchestrators", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_orchestrators_200_with_valid_token(scratch, monkeypatch):
    token = _put_access_token(scratch, resource=RESOURCE + "/")
    client, fake = _app_client(monkeypatch)

    r = client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    assert r.json() == {"orchestrators": ["claude-bridge-dev", "other-orch"], "tees": []}
    assert fake.calls == [("agent_scratch:events:live", 200)]


def test_auth_check_runs_off_thread_for_async_routes(scratch, monkeypatch):
    token = _put_access_token(scratch, resource=RESOURCE)
    calls = []
    real_run_sync = anyio.to_thread.run_sync

    async def spy_run_sync(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr("arb_memory.visibility.anyio.to_thread.run_sync", spy_run_sync)
    client, _fake = _app_client(monkeypatch)

    assert client.get("/orchestrators", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert "_authenticate_blocking" in calls
