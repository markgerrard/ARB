import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

import anyio
import httpx
import pytest

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app
from arb_memory.watch import sse_client
from tests.arb_memory.test_visibility_sse import _start_server, _stop_server


RESOURCE = "https://mem.example.com"
ORCHESTRATOR = "claude-bridge-dev"


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


def _live_entry(task_id, seat_id, event_type, *, data=None):
    return {
        "run_id": "run-1",
        "task_id": task_id,
        "seat_id": seat_id,
        "orchestrator": ORCHESTRATOR,
        "event_type": event_type,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": json.dumps(data or {}),
    }


def test_parse_frames_skips_comments_buffers_tail_and_marks_resumable_ids():
    frames, tail = sse_client.parse_frames(
        ": ping\n\n"
        "id: 1-0\n"
        "event: seat_appear\n"
        'data: {"task_id":"t1"}\n\n'
        "id: backfill-1\n"
        "event: backfill\n"
        'data: {"task_id":"t1","source":"eval"}\n\n'
        "id: 2-0\n"
        "event: seat_update\n"
        'data: {"task_id":'
    )

    assert tail == 'id: 2-0\nevent: seat_update\ndata: {"task_id":'
    assert frames == [
        {
            "id": "1-0",
            "resumable_id": "1-0",
            "event": "seat_appear",
            "data": {"task_id": "t1"},
        },
        {
            "id": "backfill-1",
            "resumable_id": None,
            "event": "backfill",
            "data": {"task_id": "t1", "source": "eval"},
        },
    ]


def test_resumable_id_accepts_only_redis_stream_ids():
    assert sse_client.is_resumable_event_id("1-0")
    assert sse_client.is_resumable_event_id("1719340000000-12")
    assert not sse_client.is_resumable_event_id("backfill-1")
    assert not sse_client.is_resumable_event_id("stale-task-1")
    assert not sse_client.is_resumable_event_id("")


def test_stream_sets_bearer_reconnects_with_valid_last_event_id(monkeypatch):
    requests = []
    sleeps = []

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks
            self.status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_text(self):
            for chunk in self._chunks:
                yield chunk

    class FakeStream:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        responses = [
            FakeResponse(['id: 1-0\nevent: seat_appear\ndata: {"task_id":"t1"}\n\n']),
            FakeResponse(['id: 2-0\nevent: seat_update\ndata: {"task_id":"t1"}\n\n']),
        ]

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            requests.append((method, url, dict(headers)))
            return FakeStream(self.responses.pop(0))

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sse_client.asyncio, "sleep", fake_sleep)

    async def collect_two():
        got = []
        async for frame in sse_client.stream("http://visibility/sse/orchestrator/o1", "token-1"):
            got.append(frame)
            if len(got) == 2:
                return got

    frames = anyio.run(collect_two)

    assert [frame["id"] for frame in frames] == ["1-0", "2-0"]
    assert requests[0][2] == {"Authorization": "Bearer token-1"}
    assert requests[1][2] == {"Authorization": "Bearer token-1", "Last-Event-ID": "1-0"}
    assert sleeps == [pytest.approx(0.25)]


def test_stream_does_not_send_synthetic_initial_last_id(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_text(self):
            yield 'id: 3-0\nevent: event\ndata: {"task_id":"t1"}\n\n'

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            requests.append(dict(headers))
            return FakeStream()

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)

    async def read_one():
        async for frame in sse_client.stream("http://visibility/sse/seat/t1", "token-1", last_id="backfill-1"):
            return frame

    frame = anyio.run(read_one)

    assert frame["id"] == "3-0"
    assert requests == [{"Authorization": "Bearer token-1"}]


def test_stream_resets_partial_tail_between_reconnects(monkeypatch):
    sleeps = []

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks
            self.status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_text(self):
            for chunk in self._chunks:
                yield chunk

    class FakeStream:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        responses = [
            FakeResponse(['id: 1-0\nevent: seat_appear\ndata: {"task_id":"partial"']),
            FakeResponse(['id: 2-0\nevent: seat_appear\ndata: {"task_id":"fresh"}\n\n']),
        ]

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            return FakeStream(self.responses.pop(0))

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sse_client.asyncio, "sleep", fake_sleep)

    async def read_one():
        async for frame in sse_client.stream("http://visibility/sse/orchestrator/o1", "token-1"):
            return frame

    frame = anyio.run(read_one)

    assert frame["id"] == "2-0"
    assert frame["data"] == {"task_id": "fresh"}
    assert sleeps == [pytest.approx(0.25)]


def test_stream_reconnects_after_transient_network_error(monkeypatch):
    sleeps = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_text(self):
            yield 'id: 1-0\nevent: seat_appear\ndata: {"task_id":"t1"}\n\n'

    class RaisingStream:
        async def __aenter__(self):
            raise httpx.ConnectError("connect failed")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            self.calls += 1
            if self.calls == 1:
                return RaisingStream()
            return FakeStream()

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sse_client.asyncio, "sleep", fake_sleep)

    async def read_one():
        async for frame in sse_client.stream("http://visibility/sse/orchestrator/o1", "token-1"):
            return frame

    frame = anyio.run(read_one)

    assert frame["id"] == "1-0"
    assert sleeps == [pytest.approx(0.25)]


def test_stream_reraises_4xx_http_status(monkeypatch):
    request = httpx.Request("GET", "http://visibility/sse/orchestrator/o1")
    response = httpx.Response(401, request=request)

    class FakeResponse:
        status_code = 401

        def raise_for_status(self):
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            return FakeStream()

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)

    async def read_one():
        async for frame in sse_client.stream("http://visibility/sse/orchestrator/o1", "token-1"):
            return frame

    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(read_one)


def test_stream_resets_backoff_after_successful_frame(monkeypatch):
    sleeps = []

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks
            self.status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_text(self):
            for chunk in self._chunks:
                yield chunk

    class RaisingStream:
        async def __aenter__(self):
            raise httpx.RemoteProtocolError("dropped")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeStream:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        responses = [
            RaisingStream(),
            FakeStream(FakeResponse(['id: 1-0\nevent: seat_appear\ndata: {"task_id":"t1"}\n\n'])),
            FakeStream(FakeResponse(['id: 2-0\nevent: seat_update\ndata: {"task_id":"t1"}\n\n'])),
        ]

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, *, headers):
            return self.responses.pop(0)

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sse_client.asyncio, "sleep", fake_sleep)

    async def collect_two():
        got = []
        async for frame in sse_client.stream("http://visibility/sse/orchestrator/o1", "token-1"):
            got.append(frame)
            if len(got) == 2:
                return got

    frames = anyio.run(collect_two)

    assert [frame["id"] for frame in frames] == ["1-0", "2-0"]
    assert sleeps == [pytest.approx(0.25), pytest.approx(0.25)]


def test_parse_frames_preserves_multiline_data():
    frames, tail = sse_client.parse_frames('event: event\ndata: {"a":1,\ndata: "b":2}\n\n')

    assert tail == ""
    assert frames[0]["data"] == json.loads('{"a":1,\n"b":2}')


def test_stream_reads_real_visibility_gateway(scratch, redis_bus):
    token = _put_access_token(scratch)
    stream_key = f"{redis_bus.prefix}events:live"
    redis_bus.xadd(stream_key, _live_entry("t-live", "codex", "task_started"))
    app = build_visibility_app(
        bus_redis_url=os.environ.get("ARB_MEMORY_REDIS_URL", "redis://127.0.0.1:6379/15"),
        bus_prefix=redis_bus.prefix,
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
    )
    base_url, server, thread = _start_server(app)

    async def read_one():
        async for frame in sse_client.stream(f"{base_url}/sse/orchestrator/{ORCHESTRATOR}", token):
            return frame

    try:
        frame = anyio.run(lambda: asyncio.wait_for(read_one(), timeout=5))
    finally:
        _stop_server(server, thread)

    assert frame["event"] == "seat_appear"
    assert frame["resumable_id"]
    assert frame["data"]["task_id"] == "t-live"
