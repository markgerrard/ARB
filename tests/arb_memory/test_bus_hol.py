import time

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

from tests.arb_memory.conftest import _row_exists, _wait_until

from arb_memory.bus import MemoryConsumer, memory_query, memory_write


def test_reads_unaffected_by_write_backlog(redis_bus, conn_factory, fake_embed, make_slow_embed):
    slow = make_slow_embed(fake_embed, delay=0.5)
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=slow, prefix=redis_bus.prefix)
    consumer.start()
    try:
        memory_write(
            redis_bus,
            artefact={"artefact_id": "target", "content": "c"},
            hints=[{"text": "the target hint"}],
            prefix=redis_bus.prefix,
        )
        _wait_until(lambda: _row_exists(conn_factory(), "target"), timeout=10)

        for i in range(20):
            memory_write(
                redis_bus,
                artefact={"artefact_id": f"d{i}", "content": "c"},
                hints=[{"text": f"t{i}"}],
                prefix=redis_bus.prefix,
            )

        start = time.monotonic()
        out = memory_query(redis_bus, "the target hint", k=1, timeout_s=2.0, prefix=redis_bus.prefix)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert out is not None and len(out) >= 1
    finally:
        consumer.stop()
