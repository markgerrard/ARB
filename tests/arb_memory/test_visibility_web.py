import os
import socket
import threading
import time
import json
from datetime import datetime, timedelta, timezone
import uuid

import httpx
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app


RESOURCE = "https://mem.example.com"


class FakeRedis:
    def xrevrange(self, key, count=200):
        return []


def _app_client(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory:test@127.0.0.1:5544/arb_memory")
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    return TestClient(app)


def _put_access_token(conn):
    token = f"access-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return token


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


def test_index_and_appjs_public(monkeypatch):
    client = _app_client(monkeypatch)

    index = client.get("/")
    appjs = client.get("/app.js")

    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert appjs.status_code == 200
    assert "application/javascript" in appjs.headers["content-type"]


def test_data_routes_still_gated(monkeypatch):
    client = _app_client(monkeypatch)

    assert client.get("/orchestrators").status_code == 401


def test_journey_graph_requires_auth_json_no_redirect(monkeypatch):
    client = _app_client(monkeypatch)

    response = client.get("/journey/graph.json", headers={"Accept": "text/html"})

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_journey_page_unauthenticated_without_login_is_401(monkeypatch):
    client = _app_client(monkeypatch)

    response = client.get("/journey", headers={"Accept": "text/html"})

    assert response.status_code == 401


def test_journey_page_unauthenticated_with_login_redirects(monkeypatch, scratch):
    monkeypatch.setenv("ARB_MEMORY_DSN", os.environ["ARB_MEMORY_DSN"])
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", "secret")
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    client = TestClient(app)

    response = client.get("/journey", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_journey_graph_missing_snapshot_is_503(scratch, monkeypatch, tmp_path):
    token = _put_access_token(scratch)
    monkeypatch.setenv("ARB_JOURNEY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    client = TestClient(app)

    response = client.get("/journey/graph.json", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert response.json() == {"error": "no snapshot yet — run arb-journey-export"}


def test_journey_graph_serves_snapshot_to_token(scratch, monkeypatch, tmp_path):
    token = _put_access_token(scratch)
    graph = {"generated_at": "2026-07-07T00:00:00Z", "nodes": [], "edges": [], "free_hints": [], "counts": {}}
    (tmp_path / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setenv("ARB_JOURNEY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    client = TestClient(app)

    response = client.get("/journey/graph.json", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == graph


def test_sse_orchestrator_no_buffer_headers(scratch, monkeypatch):
    token = _put_access_token(scratch)
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    base_url, server, thread = _start_server(app)

    try:
        with httpx.stream(
            "GET",
            f"{base_url}/sse/orchestrator/claude-bridge-dev",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        ) as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
    finally:
        _stop_server(server, thread)
