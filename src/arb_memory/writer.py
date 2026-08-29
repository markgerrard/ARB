from contextlib import asynccontextmanager
import hmac
import json
import math
import os
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arb_memory import bus

WRITE_AWAIT_CAP_S = 30


def build_writer_app(redis_client, *, token=None, async_redis_client=None):
    token = token if token is not None else os.environ.get("ARB_MEMORY_WRITER_TOKEN", "")
    if async_redis_client is None and os.environ.get("ARB_MEMORY_REDIS_URL"):
        # Hardened, not bare: this pool serves ONLY the await path, so it is idle almost all
        # the time and its connections are stale far more often than the sync client's. Without
        # health_check_interval redis-py hands one out dead and the BLPOP below raises.
        from arb_memory import redis_conn

        async_redis_client = redis_conn.async_from_url(os.environ["ARB_MEMORY_REDIS_URL"])

    @asynccontextmanager
    async def lifespan(app):
        yield
        if async_redis_client is not None:
            await async_redis_client.aclose()

    async def publish(request: Request):
        auth = request.headers.get("Authorization", "")
        presented = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(presented, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        intent = await request.json()
        if "request_id" in intent:
            return JSONResponse({"error": "client request_id is not permitted"}, status_code=400)
        await_result = bool(intent.get("await", False))
        # Second door (design §3): is not None, not key-presence — null must not 400.
        artefact = intent.get("artefact")
        if (
            isinstance(artefact, dict)
            and artefact.get("expected_version") is not None
            and not await_result
        ):
            return JSONResponse(
                {"error": "expected_version requires await"}, status_code=400
            )
        if await_result and async_redis_client is None:
            return JSONResponse({"error": "result channel unavailable"}, status_code=503)
        timeout = float(intent.get("timeout", WRITE_AWAIT_CAP_S))
        if not math.isfinite(timeout):
            timeout = WRITE_AWAIT_CAP_S
        timeout = min(max(0.001, timeout), WRITE_AWAIT_CAP_S)
        request_id = uuid.uuid4().hex if await_result else None
        try:
            ulid = bus.memory_write(
                redis_client,
                artefact=intent.get("artefact"),
                hints=intent.get("hints", []),
                source="mcp",
                author=intent.get("author", "mcp"),
                request_id=request_id,
            )
        except Exception:
            return JSONResponse({"error": "bus unavailable"}, status_code=503)
        if await_result:
            # The XADD above has COMMITTED. Everything from here is the result channel, and a
            # raise here used to escape as a 500 — which the door (mcp/tools.py) turned into
            # "item NOT stored" for an item that WAS stored (2026-08-08: art-8679a50030cf5d41,
            # reported failed, present at v1). Never let a result-channel fault be reported as a
            # failed write: past the XADD the honest answer is "unknown", the same shape the
            # timeout path below already returns.
            try:
                result = await async_redis_client.blpop(
                    bus.write_result_key(request_id), timeout=timeout
                )
                if result is None:
                    return JSONResponse(
                        {"ulid": ulid, "artefact_outcome": "unknown", "timed_out": True}
                    )
                _, envelope = result
                if isinstance(envelope, bytes):
                    envelope = envelope.decode()
                # Decoding is post-commit too: a malformed envelope must not read as a failed write.
                decoded = json.loads(envelope)
            except Exception:
                return JSONResponse(
                    {"ulid": ulid, "artefact_outcome": "unknown", "result_channel_error": True}
                )
            return JSONResponse({"ulid": ulid, **decoded})
        return JSONResponse({"ulid": ulid})

    return Starlette(routes=[Route("/publish", publish, methods=["POST"])], lifespan=lifespan)
