"""Connection hardening for the memory plane.

These are cheap assertions guarding an expensive lesson: a bare from_url() has no pool health
check, so a connection that died while idle is handed out dead and the next command raises.
That is what took down the writer's await path on 2026-08-08 and got reported to callers as
"item NOT stored" for an item that was stored.
"""

from arb_memory import fetch, redis_conn, writer


def test_baseline_sets_keepalive_and_pool_health_check():
    kwargs = redis_conn.connect_kwargs()
    # These two are the point of the module. A dead idle connection must surface as a
    # reconnect, never as a failed command.
    assert kwargs["socket_keepalive"] is True
    assert kwargs["health_check_interval"] == redis_conn.HEALTH_CHECK_INTERVAL
    assert kwargs["decode_responses"] is True
    assert kwargs["socket_connect_timeout"] == redis_conn.SOCKET_CONNECT_TIMEOUT


def test_socket_timeout_exceeds_every_blocking_read_on_this_plane():
    """socket_timeout must EXCEED the longest blocking pop, never match it.

    At parity redis-py can close the socket exactly as a server-side blocking pop returns nil,
    manufacturing the spurious "timeout reading from socket" errors it appears to prevent.
    This test fails if anyone raises a blocking read past the socket budget.
    """
    # The correctness invariant, against whatever this environment is actually configured for.
    # FETCH_TIMEOUT_SECONDS is env-overridable, so this is the assertion that must always hold.
    longest_blocking_read = max(writer.WRITE_AWAIT_CAP_S, fetch.FETCH_TIMEOUT_SECONDS)
    assert redis_conn.SOCKET_TIMEOUT > longest_blocking_read, (
        "ARB_MEMORY_FETCH_TIMEOUT_SECONDS has been raised past the socket budget; "
        "raise redis_conn.SOCKET_TIMEOUT to match"
    )
    # Headroom, not a hairline pass — the bridge settled on 4x for the same reason. Checked
    # against the fixed cap so an env override cannot make this fail spuriously.
    assert redis_conn.SOCKET_TIMEOUT >= 4 * writer.WRITE_AWAIT_CAP_S


def test_call_sites_may_shorten_timeouts_but_not_drop_the_health_check():
    """visibility.py deliberately fast-fails a black-holed bus (audit VIS-2); that still holds."""
    kwargs = redis_conn.connect_kwargs(socket_connect_timeout=5, socket_timeout=5)
    assert kwargs["socket_timeout"] == 5 and kwargs["socket_connect_timeout"] == 5
    assert kwargs["socket_keepalive"] is True
    assert kwargs["health_check_interval"] == redis_conn.HEALTH_CHECK_INTERVAL


def test_generated_store_scripts_use_the_hardened_constructor():
    """learn_intake and wiki_refresh emit store scripts that run out-of-process, where a bare
    from_url() would reintroduce the dead-idle-connection failure this module exists to close.
    The generated source must route through redis_conn and still be valid Python.
    """
    from agent_redis_bridge import learn_intake, wiki_refresh

    intent = {
        "artefact": {"source": "test", "author": "test"},
        "hints": [],
        "ulid": "01TESTULID0000000000000000",
    }
    for mod in (learn_intake, wiki_refresh):
        script = mod.build_store_script([intent])
        compile(script, f"<{mod.__name__} store script>", "exec")
        assert "redis_conn.from_url(" in script, mod.__name__
        # Substring check is safe: "redis_conn.from_url(" does not contain "redis.from_url(".
        assert "redis.from_url(" not in script, mod.__name__


def test_constructors_pass_the_baseline_through(monkeypatch):
    seen = {}

    class _FakeRedis:
        @staticmethod
        def from_url(url, **kwargs):
            seen["sync"] = (url, kwargs)
            return "sync-client"

    class _FakeAsync:
        class Redis:
            @staticmethod
            def from_url(url, **kwargs):
                seen["async"] = (url, kwargs)
                return "async-client"

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedis)
    monkeypatch.setitem(sys.modules, "redis.asyncio", _FakeAsync)
    _FakeRedis.asyncio = _FakeAsync

    assert redis_conn.from_url("redis://x/0") == "sync-client"
    assert redis_conn.async_from_url("redis://x/0") == "async-client"
    for leg in ("sync", "async"):
        _url, kwargs = seen[leg]
        assert kwargs["socket_keepalive"] is True, leg
        assert kwargs["health_check_interval"] == redis_conn.HEALTH_CHECK_INTERVAL, leg
