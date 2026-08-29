import json
import os
from pathlib import Path
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app

REPO_ROOT = Path(__file__).resolve().parents[2]


RESOURCE = "https://mem.example.com"


class FakeRedis:
    def xrevrange(self, key, count=200):
        return []


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


def test_journey_static_renders_snapshot_with_playwright(scratch, monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")

    snapshot = {
        "generated_at": "2026-07-07T12:00:00Z",
        "counts": {"nodes": 4, "edges": 2, "free_hints": 0, "isolated": 1, "dangling": 0},
        "nodes": [
            {
                "id": "wiki-hub",
                "title": "Hub",
                "tags": ["hub"],
                "kind": "wiki-page",
                "source": "wiki",
                "author": "bot",
                "created_at": "2026-07-07T12:00:00Z",
                "version": 2,
                "in_degree": 2,
                "out_degree": 2,
                "anchored_hint_count": 1,
            },
            {
                "id": "wiki-a",
                "title": "A",
                "tags": [],
                "kind": "wiki-page",
                "source": "wiki",
                "author": "bot",
                "created_at": "2026-07-07T12:00:00Z",
                "version": 1,
                "in_degree": 1,
                "out_degree": 0,
                "anchored_hint_count": 0,
            },
            {
                "id": "wiki-b",
                "title": "B",
                "tags": [],
                "kind": "wiki-page",
                "source": "wiki",
                "author": "bot",
                "created_at": "2026-07-07T12:00:00Z",
                "version": 1,
                "in_degree": 1,
                "out_degree": 0,
                "anchored_hint_count": 0,
            },
            {
                "id": "wiki-<img src=x onerror=alert(1)>",
                "title": "Evil",
                "tags": [],
                "kind": "wiki-page",
                "source": "wiki",
                "author": "bot",
                "created_at": "2026-07-07T12:00:00Z",
                "version": 1,
                "in_degree": 0,
                "out_degree": 0,
                "anchored_hint_count": 0,
            },
        ],
        "edges": [["wiki-hub", "wiki-a"], ["wiki-hub", "wiki-b"]],
        "free_hints": [],
    }
    (tmp_path / "graph.json").write_text(json.dumps(snapshot), encoding="utf-8")
    token = _put_access_token(scratch)
    monkeypatch.setenv("ARB_JOURNEY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: FakeRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    base_url, server, thread = _start_server(app)

    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(extra_http_headers={"Authorization": f"Bearer {token}"})
            response = page.goto(f"{base_url}/journey")
            assert response.status == 200
            page.wait_for_selector('[data-testid="node-row"]')
            assert page.locator('[data-testid="node-row"]').count() == snapshot["counts"]["nodes"]
            page.get_by_text("Hub").click()
            assert "memory_get('wiki-hub', 2)" in page.locator("#detail").inner_text()
            assert "2026-07-07T12:00:00Z" in page.locator("#generated-at").inner_text()
            page.get_by_text("Evil").click()
            assert "<img" not in page.locator("#detail").inner_html()
            browser.close()
    finally:
        _stop_server(server, thread)


def test_journey_static_has_no_external_assets():
    html = (REPO_ROOT / "src" / "arb_memory" / "static" / "journey.html").read_text(encoding="utf-8")

    assert "https://" not in html
    assert "<script src=" not in html
    assert "escapeAttr(value) { return escapeHtml" in html
