import time

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

from arb_memory.bus import memory_query


def test_read_timeout_returns_none(redis_bus):
    # memory_query returns None on timeout/miss — the SIGNAL for the caller to fall back.
    # The grep fallback itself is CALLER-side discipline (the seat greps its own source; see
    # arb-memory-architecture §3) and is NOT a mechanism inside arb_memory — so this test
    # asserts only the None return. Renamed from ..._then_grep, which over-claimed a grep this
    # test never exercised (a vacuously-green name; see docs/defect-classes/).
    start = time.monotonic()
    out = memory_query(redis_bus, "anything", timeout_s=1.0, prefix=redis_bus.prefix)
    elapsed = time.monotonic() - start

    assert out is None
    assert 0.9 <= elapsed <= 2.0
