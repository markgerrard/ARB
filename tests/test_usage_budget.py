"""ARB-B13: every usage-budget branch runs without Redis, and dedup without anything.

The filing's acceptance is converted coverage, not moved lines: the budget
decision previously hid behind an internal datetime.now() and was mocked out by
the only test that met it. These tests run the real branches. The in-process
duplicate window (`Bridge.is_duplicate`) was already pure — direct tests here
pin that fact so the filing's "needs live Redis" premise stays refuted.
"""

from __future__ import annotations

from collections import deque
from unittest import mock

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.usage_budget import (
    REQUEST_LIMIT_REACHED,
    TURN_SECONDS_LIMIT_REACHED,
    evaluate_usage_budget,
)


class _CountingReader:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.value


def test_disabled_limits_refuse_nothing_and_read_nothing():
    requests = _CountingReader(10**9)
    seconds = _CountingReader(10**9)
    assert (
        evaluate_usage_budget(
            request_limit=0,
            turn_seconds_limit=0,
            read_requests=requests,
            read_turn_seconds=seconds,
        )
        is None
    )
    # Laziness is contract: disabled limits must cost zero reads (no Redis
    # round-trip on the pre-split path either).
    assert requests.calls == 0
    assert seconds.calls == 0


def test_request_limit_under_at_over():
    for count, expected in ((0, None), (4, None), (5, REQUEST_LIMIT_REACHED), (9, REQUEST_LIMIT_REACHED)):
        got = evaluate_usage_budget(
            request_limit=5,
            turn_seconds_limit=0,
            read_requests=_CountingReader(count),
            read_turn_seconds=_CountingReader(0),
        )
        assert got == expected, f"count={count}"


def test_turn_seconds_limit_under_at_over():
    for count, expected in ((0, None), (599, None), (600, TURN_SECONDS_LIMIT_REACHED)):
        got = evaluate_usage_budget(
            request_limit=0,
            turn_seconds_limit=600,
            read_requests=_CountingReader(0),
            read_turn_seconds=_CountingReader(count),
        )
        assert got == expected, f"count={count}"


def test_request_limit_checked_before_turn_seconds():
    # Both exhausted -> the request token wins; the seconds reader is never
    # consulted. Pinned because dispatchers key on the token text.
    seconds = _CountingReader(10**9)
    got = evaluate_usage_budget(
        request_limit=1,
        turn_seconds_limit=1,
        read_requests=_CountingReader(1),
        read_turn_seconds=seconds,
    )
    assert got == REQUEST_LIMIT_REACHED
    assert seconds.calls == 0


class _FakeIntRedis:
    def __init__(self) -> None:
        self.ints: dict[str, int] = {}
        self.reads: list[str] = []

    def get_int(self, key: str) -> int:
        self.reads.append(key)
        return self.ints.get(key, 0)


def test_bridge_adapter_reads_todays_key_for_real():
    # The adapter (not a mock of it) must consult TODAY's usage key — the gap
    # that forced the old test to mock the whole method was a hardcoded date.
    from datetime import datetime
    from types import SimpleNamespace

    from agent_redis_bridge.redis_io import RedisConfig

    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(daily_request_limit=1, daily_turn_seconds_limit=0)
    bridge.usage_identity = "codex-test"
    bridge.redis_config = RedisConfig(
        host="127.0.0.1", port="6379", db="0", prefix="agent_scratch:"
    )
    fake = _FakeIntRedis()
    day = datetime.now().astimezone().strftime("%Y%m%d")
    fake.ints[bridge.redis_config.usage_key("codex-test", day, "requests")] = 1
    bridge.redis = fake

    assert Bridge.check_usage_budget(bridge) == REQUEST_LIMIT_REACHED
    assert fake.reads, "adapter performed no read at all"
    assert day in fake.reads[0], f"adapter read {fake.reads[0]!r}, not today's key"


class _DedupHost:
    """Minimal host for Bridge.is_duplicate — the method touches only seen_request_ids."""

    def __init__(self) -> None:
        self.seen_request_ids: deque = deque()


def test_duplicate_window_detects_repeat_without_any_backend():
    host = _DedupHost()
    assert Bridge.is_duplicate(host, "req-1") is False
    assert Bridge.is_duplicate(host, "req-1") is True
    assert Bridge.is_duplicate(host, "req-2") is False


def test_duplicate_window_expires_after_sixty_seconds():
    host = _DedupHost()
    with mock.patch("agent_redis_bridge.bridge.time.monotonic", return_value=1000.0):
        assert Bridge.is_duplicate(host, "req-1") is False
    with mock.patch("agent_redis_bridge.bridge.time.monotonic", return_value=1059.0):
        assert Bridge.is_duplicate(host, "req-1") is True
    with mock.patch("agent_redis_bridge.bridge.time.monotonic", return_value=1061.0):
        assert Bridge.is_duplicate(host, "req-1") is False, "entry older than 60s must expire"
