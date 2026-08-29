import asyncio
from datetime import datetime, timedelta, timezone
import os
import uuid

import anyio
import httpx
import pytest

from arb_memory.mcp import oauth_store
from arb_memory.visibility import build_visibility_app, mint_visibility_token
from tests.arb_memory.test_visibility_sse import _start_server, _stop_server


RESOURCE = "https://mem.example.com"
ORCHESTRATOR = "claude-bridge-dev"


@pytest.fixture
def visibility_redis():
    redis = pytest.importorskip("redis")
    url = "redis://127.0.0.1:6379/14"
    client = redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no redis")
    prefix = f"vis_token_test_{uuid.uuid4().hex}:"
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


def _app(*, redis_url, prefix, re_validate_s=0.05):
    return build_visibility_app(
        bus_redis_url=redis_url,
        bus_prefix=prefix,
        dsn=os.environ["ARB_MEMORY_DSN"],
        public_base_url=RESOURCE,
        re_validate_s=re_validate_s,
    )


async def _stream_status(base_url, token):
    async with httpx.AsyncClient(timeout=5) as client:
        async with client.stream(
            "GET",
            f"{base_url}/sse/orchestrator/{ORCHESTRATOR}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            return response.status_code


def test_long_lived_visibility_token_auths_at_sse_connect(scratch, visibility_redis):
    token = mint_visibility_token(scratch, public_base_url=RESOURCE)
    redis_url, prefix, _redis_client = visibility_redis
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))
    try:
        status = anyio.run(lambda: _stream_status(base_url, token))
    finally:
        _stop_server(server, thread)

    assert status == 200


def test_expired_visibility_token_401s_at_sse_connect(scratch, visibility_redis):
    token = f"expired-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        scratch,
        token=token,
        client_id=f"client-{uuid.uuid4().hex}",
        resource=RESOURCE,
        scopes=["memory.read"],
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    redis_url, prefix, _redis_client = visibility_redis
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix))
    try:
        status = anyio.run(lambda: _stream_status(base_url, token))
    finally:
        _stop_server(server, thread)

    assert status == 401


async def _stream_closes_after_revoke(base_url, token, conn, path):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "GET",
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            assert response.status_code == 200
            oauth_store.revoke_access_token(conn, token)

            async def read_to_eof():
                async for _line in response.aiter_lines():
                    pass

            await asyncio.wait_for(read_to_eof(), timeout=2)


def test_revoked_visibility_token_closes_sse_stream_within_revalidation_interval(scratch, visibility_redis):
    token = mint_visibility_token(scratch, public_base_url=RESOURCE)
    redis_url, prefix, _redis_client = visibility_redis
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix, re_validate_s=0.05))
    try:
        anyio.run(lambda: _stream_closes_after_revoke(base_url, token, scratch, f"/sse/orchestrator/{ORCHESTRATOR}"))
    finally:
        _stop_server(server, thread)


def test_revoked_visibility_token_closes_seat_sse_stream_within_revalidation_interval(scratch, visibility_redis):
    token = mint_visibility_token(scratch, public_base_url=RESOURCE)
    redis_url, prefix, _redis_client = visibility_redis
    base_url, server, thread = _start_server(_app(redis_url=redis_url, prefix=prefix, re_validate_s=0.05))
    try:
        anyio.run(lambda: _stream_closes_after_revoke(base_url, token, scratch, "/sse/seat/task-1"))
    finally:
        _stop_server(server, thread)
