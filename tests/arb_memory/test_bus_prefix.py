import importlib

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

import arb_memory.bus as bus


def test_default_prefix_comes_from_env(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_PREFIX", "seat-test:")
    reloaded = importlib.reload(bus)
    try:
        assert reloaded.PREFIX == "seat-test:"
        assert reloaded.writes_stream() == "seat-test:arbmem:writes"
        assert reloaded.reads_stream() == "seat-test:arbmem:reads"
        assert reloaded.reply_key("cid") == "seat-test:arbmem:reply:cid"
    finally:
        monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
        importlib.reload(bus)
