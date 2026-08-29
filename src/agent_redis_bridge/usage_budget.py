"""Daily usage-budget decision — the claim_gate-style no-I/O split (ARB-B13).

This module performs NO I/O and never reads a clock: limits arrive as values
and counters arrive through injected zero-argument readers, so every branch is
reachable in a test without Redis. The motivating gap: the previous in-``Bridge``
form computed ``datetime.now()`` internally, so the one test touching it had to
mock the whole method out — a mocked-out branch is a green test exactly the way
a skipped one is. ``Bridge.check_usage_budget`` is now the thin adapter that
supplies the real clock and the real Redis-backed readers.

Laziness is part of the contract, not an optimization: a reader is invoked ONLY
when its limit is enabled, preserving the original behaviour of performing no
Redis round-trip for disabled limits (the common fleet configuration).
"""

from __future__ import annotations

from typing import Callable

REQUEST_LIMIT_REACHED = "daily-request-limit-reached"
TURN_SECONDS_LIMIT_REACHED = "daily-turn-seconds-limit-reached"


def evaluate_usage_budget(
    *,
    request_limit: int,
    turn_seconds_limit: int,
    read_requests: Callable[[], int],
    read_turn_seconds: Callable[[], int],
) -> str | None:
    """Refusal token when a daily budget is exhausted, else None.

    Order is load-bearing and pinned by tests: the request limit is checked
    before the turn-seconds limit, matching the pre-split behaviour so refusal
    tokens seen by dispatchers do not change.
    """
    if request_limit > 0 and read_requests() >= request_limit:
        return REQUEST_LIMIT_REACHED
    if turn_seconds_limit > 0 and read_turn_seconds() >= turn_seconds_limit:
        return TURN_SECONDS_LIMIT_REACHED
    return None
