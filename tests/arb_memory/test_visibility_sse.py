import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import threading
import time
import uuid

import anyio
import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import STALE_GRACE_S, _reduce_seat, build_visibility_app


RESOURCE = "https://mem.example.com"
ORCHESTRATOR = "claude-bridge-dev"
requires_memory_dsn = pytest.mark.skipif("ARB_MEMORY_DSN" not in os.environ, reason="no ARB_MEMORY_DSN")


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _token(prefix="access"):
    return f"{prefix}-{uuid.uuid4().hex}"


def _put_access_token(conn, *, resource=RESOURCE):
    token = _token()
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=resource,
        scopes=["memory.read"],
        expires_at=_future(),
    )
    return token


@pytest.fixture
def visibility_redis():
    redis = pytest.importorskip("redis")
    url = "redis://127.0.0.1:6379/14"
    client = redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no redis")
    prefix = f"vis_test_{uuid.uuid4().hex}:"
    try:
        yield url, prefix, client
    finally:
        keys = []
        cursor = 0
        while True:
            cursor, batch = client.scan(cursor=cursor, match=f"{prefix}*", count=1000)
            keys.extend(batch)
            if cursor == 0:
                break
        if keys:
            client.delete(*keys)
        client.close()


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


def _app(*, redis_url, prefix):
    return build_visibility_app(
        bus_redis_url=redis_url,
        bus_prefix=prefix,
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )


def _live_entry(task_id, seat_id, event_type, *, sent_at=None, data=None, orchestrator=ORCHESTRATOR):
    return {
        "run_id": "run-1",
        "task_id": task_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator,
        "event_type": event_type,
        "sent_at": sent_at or datetime.now(timezone.utc).isoformat(),
        "data": json.dumps(data or {}),
    }


def test_reduce_seat_tracks_lifecycle_vote_and_stale():
    started = _reduce_seat({}, _live_entry("t1", "codex", "task_started"))
    assert started["state"] == "running"
    assert started["task_id"] == "t1"
    assert started["seat_id"] == "codex"

    failed = _reduce_seat(started, _live_entry("t1", "codex", "task_finished", data={"ok": False}))
    assert failed["state"] == "failed"

    done = _reduce_seat(started, _live_entry("t1", "codex", "task_finished", data={"ok": True}))
    assert done["state"] == "done"

    voted = _reduce_seat(done, _live_entry("t1", "codex", "vote", data={"stance": "approve"}))
    assert voted["state"] == "done"
    assert voted["voted"] is True
    assert voted["stance"] == "approve"

    old = (datetime.now(timezone.utc) - timedelta(seconds=STALE_GRACE_S + 1)).isoformat()
    stale = _reduce_seat({"task_id": "t-old", "state": "running", "last_event_ts": old}, {})
    assert stale["state"] == "stale"


@requires_memory_dsn
def test_sse_orchestrator_401_without_token(monkeypatch):
    class NoopRedis:
        def xrevrange(self, key, count=200):
            return []

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopRedis())
    app = build_visibility_app(
        bus_redis_url="redis://bridge-bus",
        bus_prefix="agent_scratch:",
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    client = TestClient(app)

    assert client.get(f"/sse/orchestrator/{ORCHESTRATOR}").status_code == 401


async def _read_sse_events(base_url, token, orchestrator, count):
    events = []
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "GET",
            f"{base_url}/sse/orchestrator/{orchestrator}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            assert response.status_code == 200
            current = {}
            async for line in response.aiter_lines():
                if line == "":
                    if current:
                        events.append(current)
                        if len(events) >= count:
                            return events
                        current = {}
                    continue
                if line.startswith("id: "):
                    current["id"] = line[4:]
                elif line.startswith("event: "):
                    current["event"] = line[7:]
                elif line.startswith("data: "):
                    current["data"] = json.loads(line[6:])
    return events


@asynccontextmanager
async def _open_sse(client, base_url, token, orchestrator):
    cm = client.stream(
        "GET",
        f"{base_url}/sse/orchestrator/{orchestrator}",
        headers={"Authorization": f"Bearer {token}"},
    )
    response = await cm.__aenter__()
    try:
        assert response.status_code == 200
        yield response
    finally:
        await cm.__aexit__(None, None, None)


async def _read_one_sse_event(response):
    current = {}
    async for line in response.aiter_lines():
        if line == "":
            if current:
                return current
            continue
        if line.startswith("id: "):
            current["id"] = line[4:]
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
    raise AssertionError("stream ended before an SSE event")


def test_sse_orchestrator_backfills_matching_roster_events(scratch, visibility_redis):
    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    stream = f"{prefix}events:live"
    redis_client.xadd(stream, _live_entry("t1", "codex", "task_started"))
    redis_client.xadd(stream, _live_entry("t2", "agy", "task_started"))
    redis_client.xadd(stream, _live_entry("t1", "codex", "task_finished", data={"ok": True}))
    redis_client.xadd(stream, _live_entry("ignored", "other", "task_started", orchestrator="other-orch"))
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))
    try:
        events = anyio.run(
            lambda: asyncio.wait_for(_read_sse_events(base_url, token, ORCHESTRATOR, 3), timeout=5)
        )
    finally:
        _stop_server(server, thread)

    assert [event["event"] for event in events] == ["seat_appear", "seat_appear", "seat_finish"]
    assert [event["data"]["task_id"] for event in events] == ["t1", "t2", "t1"]


def test_sse_orchestrator_backfill_uses_status_helper_for_stalled_at(scratch, visibility_redis, monkeypatch):
    import arb_memory.visibility as visibility

    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    stream = f"{prefix}events:live"
    redis_client.hset(f"{prefix}task:t-stall:status", mapping={"stalled_at": "2026-07-07T12:00:00+00:00"})
    redis_client.xadd(stream, _live_entry("t-stall", "codex", "task_started"))
    calls = []
    original = visibility._apply_status_backfill

    def recording_backfill(seat, redis_client_arg, bus_prefix):
        calls.append((seat.get("task_id"), bus_prefix))
        return original(seat, redis_client_arg, bus_prefix)

    monkeypatch.setattr(visibility, "_apply_status_backfill", recording_backfill)
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))
    try:
        events = anyio.run(
            lambda: asyncio.wait_for(_read_sse_events(base_url, token, ORCHESTRATOR, 1), timeout=5)
        )
    finally:
        _stop_server(server, thread)

    assert calls == [("t-stall", prefix)]
    assert events[0]["data"]["stalled_at"] == "2026-07-07T12:00:00+00:00"


def test_sse_orchestrator_live_event_uses_status_helper_for_stalled_at(scratch, visibility_redis, monkeypatch):
    import arb_memory.visibility as visibility

    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    stream = f"{prefix}events:live"
    calls = []
    original = visibility._apply_status_backfill

    def recording_backfill(seat, redis_client_arg, bus_prefix):
        calls.append((seat.get("task_id"), bus_prefix))
        return original(seat, redis_client_arg, bus_prefix)

    monkeypatch.setattr(visibility, "_apply_status_backfill", recording_backfill)
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))

    async def scenario():
        async with httpx.AsyncClient(timeout=None) as client:
            async with _open_sse(client, base_url, token, ORCHESTRATOR) as response:
                redis_client.hset(
                    f"{prefix}task:t-live-stall:status",
                    mapping={"stalled_at": "2026-07-07T12:30:00+00:00"},
                )
                redis_client.xadd(stream, _live_entry("t-live-stall", "codex", "task_started"))
                return await asyncio.wait_for(_read_one_sse_event(response), timeout=5)

    try:
        event = anyio.run(scenario)
    finally:
        _stop_server(server, thread)

    assert calls == [("t-live-stall", prefix)]
    assert event["data"]["stalled_at"] == "2026-07-07T12:30:00+00:00"


def test_two_concurrent_sse_clients_receive_live_events(scratch, visibility_redis):
    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    stream = f"{prefix}events:live"
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))

    async def scenario():
        async with httpx.AsyncClient(timeout=None) as c1, httpx.AsyncClient(timeout=None) as c2:
            async with _open_sse(c1, base_url, token, ORCHESTRATOR) as r1, _open_sse(
                c2, base_url, token, ORCHESTRATOR
            ) as r2:
                redis_client.xadd(stream, _live_entry("t-live", "codex", "task_started"))
                got1, got2 = await asyncio.wait_for(
                    asyncio.gather(_read_one_sse_event(r1), _read_one_sse_event(r2)),
                    timeout=5,
                )
                return got1, got2

    try:
        got1, got2 = anyio.run(scenario)
    finally:
        _stop_server(server, thread)

    assert got1["event"] == "seat_appear"
    assert got2["event"] == "seat_appear"
    assert got1["data"]["task_id"] == "t-live"
    assert got2["data"]["task_id"] == "t-live"


def test_each_sse_stream_owns_and_closes_its_aioredis_client(scratch, monkeypatch):
    token = _put_access_token(scratch)
    clients = []

    class NoopSyncRedis:
        def xrevrange(self, key, count=200):
            return []

    class SpyAsyncRedis:
        def __init__(self):
            self.closed = False
            clients.append(self)

        async def xrange(self, key, count=200):
            return [("1-0", _live_entry("t-owned", "codex", "task_started"))]

        async def xread(self, streams, block=0):
            await asyncio.sleep(60)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopSyncRedis())
    monkeypatch.setattr("arb_memory.visibility.aioredis.from_url", lambda *args, **kwargs: SpyAsyncRedis())
    app = _app(redis_url="redis://bridge-bus", prefix="agent_scratch:")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/sse/orchestrator/{orchestrator_id}")

    async def run_one_stream():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/sse/orchestrator/{ORCHESTRATOR}",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "path_params": {"orchestrator_id": ORCHESTRATOR},
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "scheme": "http",
            },
            receive,
        )
        response = await endpoint(request)
        body = response.body_iterator
        first = await body.__anext__()
        frame = first.encode() if isinstance(first, str) else first
        assert b"event: seat_appear" in frame
        await body.aclose()

    anyio.run(run_one_stream)
    anyio.run(run_one_stream)

    assert len(clients) == 2
    assert all(client.closed for client in clients)
