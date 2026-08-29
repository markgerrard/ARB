import concurrent.futures
import time

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

from arb_memory.bus import ReadLoop, WriteLoop, handle_read_request, memory_query, memory_write, reply_key


def _wait_until(fn, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def test_reply_key_validation(redis_bus, scratch, fake_embed):
    cid = "c1"
    foreign = f"{redis_bus.prefix}foreign"
    valid = reply_key(cid, prefix=redis_bus.prefix)

    handle_read_request(
        redis_bus,
        scratch,
        {"cid": cid, "reply": foreign, "query": "anything", "k": "1"},
        embed=fake_embed,
        prefix=redis_bus.prefix,
    )

    assert redis_bus.llen(foreign) == 0
    assert redis_bus.llen(valid) == 0


def test_read_error_reply_is_a_miss(redis_bus, conn_factory):
    loop = ReadLoop(redis_bus, conn_factory, embed=lambda text: (_ for _ in ()).throw(RuntimeError("boom")), prefix=redis_bus.prefix)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memory_query, redis_bus, "anything", 1, timeout_s=2.0, prefix=redis_bus.prefix)
        _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") == 1)
        loop.step()
        assert future.result(timeout=1.0) is None


def test_write_then_read_roundtrip(redis_bus, conn_factory, fake_embed):
    write_loop = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    read_loop = ReadLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    memory_write(
        redis_bus,
        artefact={"artefact_id": "d.md", "content": "c"},
        hints=[{"text": "the target hint"}],
        prefix=redis_bus.prefix,
    )
    entry_id, intent = write_loop.read_one()
    write_loop.handle_and_ack(entry_id, intent)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memory_query, redis_bus, "the target hint", 1, timeout_s=2.0, prefix=redis_bus.prefix)
        _wait_until(lambda: redis_bus.xlen(f"{redis_bus.prefix}arbmem:reads") == 1)
        read_loop.step()
        out = future.result(timeout=1.0)

    assert out is not None
    assert out[0]["hint"]["text"] == "the target hint"
