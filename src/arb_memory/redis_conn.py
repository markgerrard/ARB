"""Hardened Redis/Valkey client construction for the memory plane.

A bare ``from_url(url, decode_responses=True)`` leaves redis-py with no keepalive and no pool
health check. A pooled connection that died while idle is then handed out dead, and the FIRST
command on it raises ``ConnectionError`` — the caller never gets a chance to notice.

That is not theoretical. The writer's await-path client (``writer.py``, used only when
``await_result=True``, so its pool is almost always cold) failed exactly this way on
2026-08-08: the BLPOP raised, escaped as a 500, and callers were told "item NOT stored" for an
item that was stored (``art-8679a50030cf5d41``). The hot sync client on the same host, doing
an XADD on every write, was unaffected — which is the tell. Cold pool, no health check.

``src/agent_redis_bridge/redis_io.py`` has carried these settings for a long time and explains
the reasoning; this module is the same treatment for ``arb_memory``, which never got it.

Note on ``SOCKET_TIMEOUT``: it must comfortably EXCEED the longest blocking pop, never match
it. At parity, redis-py can close the socket just as a server-side blocking pop returns nil,
manufacturing the very "timeout reading from socket" errors it looks like it should prevent.
The longest blocking read on this plane is 30s (``writer.WRITE_AWAIT_CAP_S`` and
``fetch.FETCH_TIMEOUT_SECONDS``), so 120s keeps the bridge's 4x headroom.
"""

from __future__ import annotations

SOCKET_CONNECT_TIMEOUT = 10
SOCKET_TIMEOUT = 120
HEALTH_CHECK_INTERVAL = 30


def connect_kwargs(**overrides):
    """Baseline connection hardening, with per-call-site overrides.

    Call sites that do only short operations may lower ``socket_timeout``; nothing should be
    turning off ``socket_keepalive`` or ``health_check_interval``, which are what make a dead
    idle connection surface as a reconnect instead of as a failed command.
    """
    kwargs = {
        "decode_responses": True,
        "socket_connect_timeout": SOCKET_CONNECT_TIMEOUT,
        "socket_timeout": SOCKET_TIMEOUT,
        "socket_keepalive": True,
        "health_check_interval": HEALTH_CHECK_INTERVAL,
        "retry_on_timeout": True,
    }
    kwargs.update(overrides)
    return kwargs


def from_url(url, **overrides):
    """Sync client on the hardened defaults."""
    import redis

    return redis.from_url(url, **connect_kwargs(**overrides))


def async_from_url(url, **overrides):
    """Async client on the hardened defaults.

    Async pools are the ones that bite: they tend to back a single infrequent code path, so
    their connections are stale far more often than a busy sync pool's.
    """
    import redis.asyncio

    return redis.asyncio.Redis.from_url(url, **connect_kwargs(**overrides))
