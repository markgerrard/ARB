import json

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

from tests.arb_memory.conftest import _deadlettered, _row_exists, _wait_until

from arb_memory.bus import GROUP, MemoryConsumer, ReadLoop, WriteLoop, memory_write, reads_stream, reply_key, writes_stream


def test_pel_recovery_on_restart(redis_bus, conn_factory, fake_embed):
    loop = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="w1")
    memory_write(
        redis_bus,
        artefact={"artefact_id": "d.md", "content": "c"},
        hints=[{"text": "t"}],
        prefix=redis_bus.prefix,
    )

    entry_id, intent = loop.read_one()
    assert intent is not None
    assert loop.conn.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 0

    loop2 = WriteLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="w1")
    loop2.drain_pending()
    assert loop2.conn.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 1

    pend = redis_bus.xpending(f"{redis_bus.prefix}arbmem:writes", GROUP)
    assert pend["pending"] == 0


def test_write_loop_constructs_one_persistent_connection(redis_bus, conn_factory, fake_embed):
    calls = []

    def counted_factory():
        calls.append(True)
        return conn_factory()

    loop = WriteLoop(redis_bus, conn_factory=counted_factory, embed=fake_embed, prefix=redis_bus.prefix)

    assert len(calls) == 1
    assert loop.conn is not None


def test_poison_write_is_acked_and_does_not_block_valid_write(redis_bus, conn_factory, fake_embed):
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    redis_bus.xadd(
        writes_stream(redis_bus.prefix),
        {"ulid": "poison", "payload": "{"},
    )
    memory_write(
        redis_bus,
        artefact={"artefact_id": "valid.md", "content": "c"},
        hints=[{"text": "valid hint"}],
        prefix=redis_bus.prefix,
    )

    try:
        consumer.start()
        _wait_until(lambda: _row_exists(conn_factory(), "valid.md"))
        write_thread = consumer.threads[0]
        assert write_thread.is_alive()
    finally:
        consumer.stop()

    restarted = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    try:
        restarted.start()
        _wait_until(lambda: restarted.threads[0].is_alive(), timeout=1.0)
        pend = redis_bus.xpending(writes_stream(redis_bus.prefix), GROUP)
        assert pend["pending"] == 0
    finally:
        restarted.stop()


def test_poison_write_missing_hint_text_is_acked_across_restart(redis_bus, conn_factory, fake_embed, monkeypatch):
    # A handle-stage poison entry only deadletters once it exhausts the retry
    # budget, and that budget is per consumer instance (reset by the restart
    # below). Pin it to 1 so this asserts the ack, not the retry pacing.
    monkeypatch.setenv("ARB_CONSUMER_POISON_RETRY_LIMIT", "1")
    consumer = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    redis_bus.xadd(
        writes_stream(redis_bus.prefix),
        {
            "ulid": "poison-missing-text",
            "payload": json.dumps(
                {
                    "ulid": "poison-missing-text",
                    "kind": "hints",
                    "artefact": None,
                    "hints": [{"source": "seat"}],
                }
            ),
        },
    )
    memory_write(
        redis_bus,
        artefact={"artefact_id": "after-poison.md", "content": "c"},
        hints=[{"text": "valid after poison"}],
        prefix=redis_bus.prefix,
    )

    try:
        consumer.start()
        _wait_until(lambda: _row_exists(conn_factory(), "after-poison.md"))
        write_thread = consumer.threads[0]
        assert write_thread.is_alive()
    finally:
        consumer.stop()

    restarted = MemoryConsumer(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix)
    try:
        restarted.start()
        _wait_until(
            lambda: redis_bus.xpending(writes_stream(redis_bus.prefix), GROUP)["pending"] == 0,
            timeout=5.0,
        )
    finally:
        restarted.stop()

    assert _deadlettered(conn_factory(), "poison-missing-text")


def test_read_pending_cleanup_acks_without_reply(redis_bus, conn_factory, fake_embed):
    stream = reads_stream(redis_bus.prefix)
    loop = ReadLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="r1")
    cid = "dead-reader"
    reply = reply_key(cid, prefix=redis_bus.prefix)
    redis_bus.xadd(stream, {"cid": cid, "reply": reply, "query": "anything", "k": "1"})

    rows = redis_bus.xreadgroup(GROUP, "r1", {stream: ">"}, count=1)
    assert rows
    assert redis_bus.xpending(stream, GROUP)["pending"] == 1

    restarted = ReadLoop(redis_bus, conn_factory, embed=fake_embed, prefix=redis_bus.prefix, consumer="r1")
    restarted.drain_pending()

    assert redis_bus.xpending(stream, GROUP)["pending"] == 0
    assert redis_bus.llen(reply) == 0
