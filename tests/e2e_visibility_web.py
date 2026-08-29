"""E2E for ARB Visibility Slice 4b-web.

Run:
  PYTHONPATH=<wt>:<wt>/src .venv/bin/python tests/e2e_visibility_web.py
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid

import httpx
import psycopg
import redis as redislib

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisCli, RedisConfig
from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app


DSN = os.environ["ARB_MEMORY_DSN"]
BUS_URL = os.environ.get("ARB_MEMORY_REDIS_URL", "redis://127.0.0.1:6379/15")
PREFIX = f"e2e_vis_web_{uuid.uuid4().hex[:8]}:"
ORCH = f"claude-web-e2e-{uuid.uuid4().hex[:6]}"
RESOURCE = os.environ.get("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com")
TOKEN = f"e2e-vis-web-{uuid.uuid4().hex}"
CLIENT_ID = f"e2e-client-{uuid.uuid4().hex}"

failures = []
bus = redislib.from_url(BUS_URL, decode_responses=True)


def check(condition, message):
    print(("  ok: " if condition else "  FAIL: ") + message)
    if not condition:
        failures.append(message)


def _redis_config():
    parsed = urlparse(BUS_URL)
    db = parsed.path.lstrip("/") or "0"
    return RedisConfig(parsed.hostname or "127.0.0.1", str(parsed.port or 6379), db, PREFIX)


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


def seat_bridge(agent_id):
    bridge = Bridge.__new__(Bridge)
    bridge.redis_config = _redis_config()
    bridge.redis = RedisCli(bridge.redis_config)
    bridge.args = SimpleNamespace(max_task_events=500, events_ttl=300)
    bridge.agent_id = agent_id
    bridge.eval_redis = None
    bridge.audit_redis = None
    bridge._audit_prefix = ""
    return bridge


def req(task_id):
    return Envelope(
        id=task_id,
        sender=ORCH,
        branch="b",
        recipient="x",
        kind="request",
        sent_at=datetime.now(timezone.utc).isoformat(),
        payload={},
        run_id=f"run-{ORCH}",
    )


@asynccontextmanager
async def _stream(client, url, token):
    cm = client.stream("GET", url, headers={"Authorization": f"Bearer {token}"})
    response = await cm.__aenter__()
    try:
        yield response
    finally:
        await cm.__aexit__(None, None, None)


async def _read_roster_events(base_url):
    seen = {}
    current = {}
    async with httpx.AsyncClient(timeout=None) as client:
        async with _stream(client, f"{base_url}/sse/orchestrator/{ORCH}", TOKEN) as response:
            check(response.status_code == 200, f"/sse/orchestrator 200 with token (got {response.status_code})")
            check(response.headers.get("cache-control") == "no-cache", "SSE carries Cache-Control: no-cache")
            check(response.headers.get("x-accel-buffering") == "no", "SSE carries X-Accel-Buffering: no")
            async with asyncio.timeout(5):
                async for line in response.aiter_lines():
                    if line == "":
                        if current:
                            data = json.loads(current.get("data", "{}"))
                            if data.get("task_id"):
                                seen[data["task_id"]] = data
                            current = {}
                            if {"task-A", "task-B"} <= set(seen):
                                return seen
                        continue
                    if line.startswith("id: "):
                        current["id"] = line[4:]
                    elif line.startswith("event: "):
                        current["event"] = line[7:]
                    elif line.startswith("data: "):
                        current["data"] = line[6:]
    return seen


async def main():
    print(f"E2E visibility web  orch={ORCH}  prefix={PREFIX}  redis={BUS_URL}")
    bus.ping()
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        oauth_store.put_access_token(
            conn,
            token=TOKEN,
            client_id=CLIENT_ID,
            resource=RESOURCE,
            scopes=["memory.read"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    seat_bridge("codex-web-e2e").push_task_event(req("task-A"), "task_started", {"task_id": "task-A"})
    seat_bridge("agy-web-e2e").push_task_event(req("task-B"), "task_started", {"task_id": "task-B"})
    entries = bus.xrange(f"{PREFIX}events:live")
    check(len(entries) == 2, f"real bridge teed 2 entries to events:live (got {len(entries)})")

    app = build_visibility_app(bus_redis_url=BUS_URL, bus_prefix=PREFIX, dsn=DSN, public_base_url=RESOURCE)
    base_url, server, thread = _start_server(app)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
            shell = await client.get("/")
            check(shell.status_code == 200 and "text/html" in shell.headers.get("content-type", ""),
                  f"GET / serves public shell (got {shell.status_code})")
            appjs = await client.get("/app.js")
            check(appjs.status_code == 200 and "application/javascript" in appjs.headers.get("content-type", ""),
                  f"GET /app.js serves public JS (got {appjs.status_code})")
            denied = await client.get("/orchestrators")
            check(denied.status_code == 401, f"/orchestrators 401 without token (got {denied.status_code})")
            roster = await client.get("/orchestrators", headers={"Authorization": f"Bearer {TOKEN}"})
            check(roster.status_code == 200, f"/orchestrators 200 with token (got {roster.status_code})")
            check(ORCH in roster.json().get("orchestrators", []),
                  f"gateway lists orchestrator from real bridge entries (got {roster.json()})")

        seen = await _read_roster_events(base_url)
        check(set(seen) == {"task-A", "task-B"}, f"SSE roster surfaces both seats (got {sorted(seen)})")
        check({seat.get("seat_id") for seat in seen.values()} == {"codex-web-e2e", "agy-web-e2e"},
              f"SSE roster carries both seat ids (got {[seat.get('seat_id') for seat in seen.values()]})")
    finally:
        _stop_server(server, thread)


def cleanup():
    for key in bus.scan_iter(f"{PREFIX}*"):
        bus.delete(key)
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        conn.execute("DELETE FROM mcp_auth.access_tokens WHERE client_id = %s", (CLIENT_ID,))
    print("cleanup: removed e2e stream keys + token")


try:
    asyncio.run(main())
finally:
    cleanup()

print()
if failures:
    print(f"E2E FAILED - {len(failures)}: " + "; ".join(failures))
    sys.exit(1)
print("E2E PASS - public shell + bearer-gated data + live SSE roster show both seats.")
