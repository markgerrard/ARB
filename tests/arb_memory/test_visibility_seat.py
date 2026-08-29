import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
import httpx
import pytest
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.testclient import TestClient

from arb_memory.mcp import oauth_store
from arb_memory.visibility import _backfill_seat, _backfill_transcript, build_visibility_app
from arb_memory.test_visibility_sse import _start_server, _stop_server


RESOURCE = "https://mem.example.com"
TASK_ID = "task-seat-1"
RUN_ID = "run-seat-1"
SEAT_ID = "codex-bridge-dev"
ORCHESTRATOR = "claude-bridge-dev"
requires_memory_dsn = pytest.mark.skipif("ARB_MEMORY_DSN" not in os.environ, reason="no ARB_MEMORY_DSN")


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def _put_access_token(conn):
    token = f"access-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        conn,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
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
    prefix = f"vis_seat_test_{uuid.uuid4().hex}:"
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


def _scratch_dsn(scratch):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    parts = urlsplit(os.environ["ARB_MEMORY_DSN"])
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _app(*, redis_url, prefix, dsn=None, trace_redis_url="", trace_prefix=None):
    return build_visibility_app(
        bus_redis_url=redis_url,
        bus_prefix=prefix,
        trace_redis_url=trace_redis_url,
        trace_prefix=trace_prefix,
        dsn=dsn or os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )


def _seed_backfill(conn):
    conn.execute(
        """
        INSERT INTO eval_event_raw
            (run_id, task_id, seat_id, orchestrator, event_type, sent_at, payload, stream_entry_id)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s),
            (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            RUN_ID, TASK_ID, SEAT_ID, ORCHESTRATOR, "task_started",
            "2026-06-25T10:00:00+00:00", Jsonb({"phase": "start"}), f"eval-{uuid.uuid4().hex}",
            RUN_ID, TASK_ID, SEAT_ID, ORCHESTRATOR, "tool_call",
            "2026-06-25T10:00:02+00:00", Jsonb({"tool_name": "shell"}), f"eval-{uuid.uuid4().hex}",
        ),
    )
    conn.execute(
        """
        INSERT INTO audit_events (run_id, seq, source, kind, ts, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            RUN_ID,
            7,
            "seat:" + SEAT_ID,
            "vote",
            "2026-06-25T10:00:01+00:00",
            Jsonb({"actor": "seat:" + SEAT_ID, "stance": "approve"}),
        ),
    )


def _seed_transcript_backfill(conn):
    conn.execute(
        """
        INSERT INTO transcript_io
            (run_id, task_id, seat_id, orchestrator, turn_index, item_id, seq,
             kind, tool_name, content, meta, ts, stream_entry_id)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s),
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            RUN_ID, TASK_ID, SEAT_ID, ORCHESTRATOR, 0, "turn-1:text", 1,
            "model_text", None, "hello ‹redacted›", Jsonb({}),
            "2026-06-25T10:00:00.500000+00:00", f"trace-{uuid.uuid4().hex}",
            RUN_ID, TASK_ID, SEAT_ID, ORCHESTRATOR, 0, "turn-1:patch", 2,
            "command_finished", "apply_patch", "patch body", Jsonb({"file": "foo.py", "added": 3, "removed": 1}),
            "2026-06-25T10:00:01.500000+00:00", f"trace-{uuid.uuid4().hex}",
        ),
    )


def _live_entry(task_id=TASK_ID):
    return {
        "run_id": RUN_ID,
        "task_id": task_id,
        "seat_id": SEAT_ID,
        "orchestrator": ORCHESTRATOR,
        "event_type": "task_finished",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": json.dumps({"ok": True}),
    }


def _trace_entry(task_id=TASK_ID, *, kind="model_text", content="streamed text"):
    return {
        "run_id": RUN_ID,
        "task_id": task_id,
        "seat_id": SEAT_ID,
        "orchestrator": ORCHESTRATOR,
        "turn_index": "0",
        "item_id": "turn-1:text",
        "seq": "3",
        "kind": kind,
        "tool_name": "",
        "content": content,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def test_backfill_seat_merges_eval_events_and_audit_vote_by_actor(scratch):
    _seed_backfill(scratch)

    events = _backfill_seat(scratch, TASK_ID)

    assert [event["event_type"] for event in events] == ["task_started", "vote", "tool_call"]
    assert events[0]["source"] == "eval"
    assert events[0]["payload"] == {"phase": "start"}
    assert events[1]["source"] == "audit"
    assert events[1]["seq"] == 7
    assert events[1]["payload"]["actor"] == "seat:" + SEAT_ID
    assert events[2]["payload"] == {"tool_name": "shell"}


def test_backfill_seat_merges_transcript_rows_by_ts(scratch):
    _seed_backfill(scratch)
    _seed_transcript_backfill(scratch)

    events = _backfill_seat(scratch, TASK_ID)

    assert [event["event_type"] for event in events] == [
        "task_started",
        "model_text",
        "vote",
        "command_finished",
        "tool_call",
    ]
    assert events[1]["source"] == "transcript"
    assert events[1]["content"] == "hello ‹redacted›"
    assert events[3]["meta"] == {"file": "foo.py", "added": 3, "removed": 1}


def _seed_transcript_row(conn, *, task_id, seat_id, orchestrator, item_id, seq, ts):
    conn.execute(
        """
        INSERT INTO transcript_io
            (run_id, task_id, seat_id, orchestrator, turn_index, item_id, seq,
             kind, tool_name, content, meta, ts, stream_entry_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        ("run-x", task_id, seat_id, orchestrator, 0, item_id, seq,
         "model_text", None, "x", Jsonb({}), ts, f"trace-{uuid.uuid4().hex}"),
    )


def test_orchestrator_backfill_is_capped_by_window(scratch, monkeypatch):
    # The orchestrator (seat_id == orchestrator) is persistent; its backfill is capped to the
    # recent window. Time cap (3h) drops the old row; line cap keeps only the most recent N,
    # in chronological order.
    monkeypatch.setenv("ARB_VIS_BACKFILL_HOURS", "3")
    monkeypatch.setenv("ARB_VIS_BACKFILL_LINES", "2000")
    now = datetime.now(timezone.utc)
    tid = "task-orch-cap"
    orch = "claude-bridge-dev"
    _seed_transcript_row(scratch, task_id=tid, seat_id=orch, orchestrator=orch, item_id="old", seq=1, ts=now - timedelta(hours=5))
    _seed_transcript_row(scratch, task_id=tid, seat_id=orch, orchestrator=orch, item_id="r1", seq=2, ts=now - timedelta(minutes=30))
    _seed_transcript_row(scratch, task_id=tid, seat_id=orch, orchestrator=orch, item_id="r2", seq=3, ts=now - timedelta(minutes=10))

    assert [e["item_id"] for e in _backfill_transcript(scratch, tid)] == ["r1", "r2"]  # 'old' dropped by 3h window

    monkeypatch.setenv("ARB_VIS_BACKFILL_HOURS", "24")
    monkeypatch.setenv("ARB_VIS_BACKFILL_LINES", "1")
    assert [e["item_id"] for e in _backfill_transcript(scratch, tid)] == ["r2"]  # line cap -> last 1


def test_seat_backfill_not_capped(scratch, monkeypatch):
    # A short-lived seat (seat_id != orchestrator) keeps its FULL history even under a tiny cap.
    monkeypatch.setenv("ARB_VIS_BACKFILL_HOURS", "3")
    monkeypatch.setenv("ARB_VIS_BACKFILL_LINES", "1")
    now = datetime.now(timezone.utc)
    tid = "task-seat-cap"
    _seed_transcript_row(scratch, task_id=tid, seat_id="codex-bridge-dev", orchestrator="claude-bridge-dev", item_id="s_old", seq=1, ts=now - timedelta(hours=5))
    _seed_transcript_row(scratch, task_id=tid, seat_id="codex-bridge-dev", orchestrator="claude-bridge-dev", item_id="s_new", seq=2, ts=now - timedelta(minutes=5))

    assert [e["item_id"] for e in _backfill_transcript(scratch, tid)] == ["s_old", "s_new"]  # uncapped


def test_null_metadata_task_not_treated_as_orchestrator(scratch, monkeypatch):
    # Regression (panel: agy REQUEST-CHANGES): seat_id/orchestrator are nullable; a NULL==NULL row
    # must NOT be mistaken for the orchestrator (None==None is True) and capped — that would drop
    # legacy/external history silently. Even under a tiny cap, such a task keeps full history.
    monkeypatch.setenv("ARB_VIS_BACKFILL_HOURS", "3")
    monkeypatch.setenv("ARB_VIS_BACKFILL_LINES", "1")
    now = datetime.now(timezone.utc)
    tid = "task-null-meta"
    _seed_transcript_row(scratch, task_id=tid, seat_id=None, orchestrator=None, item_id="n_old", seq=1, ts=now - timedelta(hours=5))
    _seed_transcript_row(scratch, task_id=tid, seat_id=None, orchestrator=None, item_id="n_new", seq=2, ts=now - timedelta(minutes=5))

    assert [e["item_id"] for e in _backfill_transcript(scratch, tid)] == ["n_old", "n_new"]  # uncapped


@requires_memory_dsn
def test_sse_seat_401_without_token(monkeypatch):
    class NoopRedis:
        def xrevrange(self, key, count=200):
            return []

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopRedis())
    client = TestClient(_app(redis_url="redis://bridge-bus", prefix="agent_scratch:"))

    assert client.get(f"/sse/seat/{TASK_ID}").status_code == 401


async def _read_sse_events(base_url, token, task_id, count, *, last_event_id=None):
    events = []
    headers = {"Authorization": f"Bearer {token}"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "GET",
            f"{base_url}/sse/seat/{task_id}",
            headers=headers,
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


def test_sse_seat_streams_backfill_then_live_tail(scratch, visibility_redis):
    _seed_backfill(scratch)
    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    stream = f"{prefix}events:live"
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix, dsn=_scratch_dsn(scratch)))

    async def scenario():
        task = asyncio.create_task(_read_sse_events(base_url, token, TASK_ID, 4))
        await asyncio.sleep(0.2)
        redis_client.xadd(stream, _live_entry(TASK_ID))
        return await asyncio.wait_for(task, timeout=5)

    try:
        events = anyio.run(scenario)
    finally:
        _stop_server(server, thread)

    assert [event["event"] for event in events] == ["backfill", "backfill", "backfill", "event"]
    assert [event["data"]["event_type"] for event in events] == ["task_started", "vote", "tool_call", "task_finished"]
    assert events[1]["data"]["payload"]["actor"] == "seat:" + SEAT_ID
    assert events[3]["data"]["task_id"] == TASK_ID


def test_sse_seat_fans_in_live_events_and_transcript_streams(scratch, visibility_redis):
    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    events_stream = f"{prefix}events:live"
    trace_stream = f"{prefix}arbmem:trace"
    app = _app(
        redis_url=redis_url,
        prefix=prefix,
        trace_redis_url=redis_url,
        trace_prefix=prefix,
        dsn=_scratch_dsn(scratch),
    )
    base_url, server, thread = _start_server(app)

    async def scenario():
        task = asyncio.create_task(_read_sse_events(base_url, token, TASK_ID, 2))
        await asyncio.sleep(0.2)
        redis_client.xadd(trace_stream, _trace_entry(TASK_ID, content="live transcript"))
        redis_client.xadd(events_stream, _live_entry(TASK_ID))
        return await asyncio.wait_for(task, timeout=5)

    try:
        events = anyio.run(scenario)
    finally:
        _stop_server(server, thread)

    assert {event["event"] for event in events} == {"transcript", "event"}
    by_event = {event["event"]: event["data"] for event in events}
    assert by_event["transcript"]["kind"] == "model_text"
    assert by_event["transcript"]["content"] == "live transcript"
    assert by_event["event"]["event_type"] == "task_finished"


def test_sse_seat_reconnect_resumes_each_live_stream_from_its_own_cursor(scratch, visibility_redis):
    token = _put_access_token(scratch)
    redis_url, prefix, redis_client = visibility_redis
    events_stream = f"{prefix}events:live"
    trace_stream = f"{prefix}arbmem:trace"
    app = _app(
        redis_url=redis_url,
        prefix=prefix,
        trace_redis_url=redis_url,
        trace_prefix=prefix,
        dsn=_scratch_dsn(scratch),
    )
    base_url, server, thread = _start_server(app)

    async def scenario():
        first = asyncio.create_task(_read_sse_events(base_url, token, TASK_ID, 2))
        await asyncio.sleep(0.2)
        redis_client.xadd(trace_stream, _trace_entry(TASK_ID, content="trace-1"))
        redis_client.xadd(events_stream, _live_entry(TASK_ID))
        first_events = await asyncio.wait_for(first, timeout=5)
        last_event_id = first_events[-1]["id"]

        redis_client.xadd(trace_stream, _trace_entry(TASK_ID, content="trace-2"))
        redis_client.xadd(events_stream, _live_entry(TASK_ID))
        second_events = await asyncio.wait_for(
            _read_sse_events(base_url, token, TASK_ID, 2, last_event_id=last_event_id),
            timeout=5,
        )
        return first_events, second_events

    try:
        first_events, second_events = anyio.run(scenario)
    finally:
        _stop_server(server, thread)

    assert first_events[-1]["id"].startswith("e=")
    assert ";t=" in first_events[-1]["id"]
    assert [event["event"] for event in second_events].count("transcript") == 1
    assert [event["event"] for event in second_events].count("event") == 1
    assert [event["data"].get("content") for event in second_events if event["event"] == "transcript"] == ["trace-2"]
    assert all(event["data"].get("content") != "trace-1" for event in second_events)
    assert [event["data"].get("event_type") for event in second_events if event["event"] == "event"] == [
        "task_finished"
    ]


def test_sse_seat_reader_failure_closes_response(scratch, monkeypatch):
    token = _put_access_token(scratch)

    class NoopSyncRedis:
        def xrevrange(self, key, count=200):
            return []

    class FailingAsyncRedis:
        async def xread(self, streams, block=0):
            raise RuntimeError("redis reader died")

        async def aclose(self):
            return None

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopSyncRedis())
    monkeypatch.setattr("arb_memory.visibility.aioredis.from_url", lambda *args, **kwargs: FailingAsyncRedis())
    app = _app(redis_url="redis://bridge-bus", prefix="agent_scratch:", dsn=_scratch_dsn(scratch))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/sse/seat/{task_id}")

    async def run_stream():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/sse/seat/{TASK_ID}",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "path_params": {"task_id": TASK_ID},
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "scheme": "http",
            },
            receive,
        )
        response = await endpoint(request)
        body = response.body_iterator
        with pytest.raises(StopAsyncIteration):
            with anyio.fail_after(2):
                await body.__anext__()

    anyio.run(run_stream)


def test_sse_seat_owns_and_closes_its_aioredis_client(scratch, monkeypatch):
    _seed_backfill(scratch)
    token = _put_access_token(scratch)
    clients = []

    class NoopSyncRedis:
        def xrevrange(self, key, count=200):
            return []

    class SpyAsyncRedis:
        def __init__(self):
            self.closed = False
            clients.append(self)

        async def xread(self, streams, block=0):
            await asyncio.sleep(60)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopSyncRedis())
    monkeypatch.setattr("arb_memory.visibility.aioredis.from_url", lambda *args, **kwargs: SpyAsyncRedis())
    app = _app(redis_url="redis://bridge-bus", prefix="agent_scratch:", dsn=_scratch_dsn(scratch))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/sse/seat/{task_id}")

    async def run_one_stream():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/sse/seat/{TASK_ID}",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "path_params": {"task_id": TASK_ID},
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
        assert b"event: backfill" in frame
        await body.aclose()

    anyio.run(run_one_stream)
    anyio.run(run_one_stream)

    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_sse_seat_backfill_runs_off_thread(scratch, monkeypatch):
    _seed_backfill(scratch)
    token = _put_access_token(scratch)
    calls = []
    real_run_sync = anyio.to_thread.run_sync

    class NoopSyncRedis:
        def xrevrange(self, key, count=200):
            return []

    class SpyAsyncRedis:
        async def xread(self, streams, block=0):
            await asyncio.sleep(60)

        async def aclose(self):
            return None

    async def spy_run_sync(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", repr(func)))
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: NoopSyncRedis())
    monkeypatch.setattr("arb_memory.visibility.aioredis.from_url", lambda *args, **kwargs: SpyAsyncRedis())
    monkeypatch.setattr("arb_memory.visibility.anyio.to_thread.run_sync", spy_run_sync)
    app = _app(redis_url="redis://bridge-bus", prefix="agent_scratch:", dsn=_scratch_dsn(scratch))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/sse/seat/{task_id}")

    async def run_one_stream():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/sse/seat/{TASK_ID}",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "path_params": {"task_id": TASK_ID},
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
        assert b"event: backfill" in frame
        await body.aclose()

    anyio.run(run_one_stream)

    assert "_authenticate_blocking" in calls
    assert "_backfill_seat_blocking" in calls
