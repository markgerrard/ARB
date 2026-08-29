import importlib


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import arb_memory.eval_config as ec
    return importlib.reload(ec)


def test_pinned_constants(monkeypatch):
    ec = _reload(monkeypatch)
    assert ec.EVAL_STREAM == "eval:events"
    assert ec.EVAL_GROUP == "arbmem-eval"


def test_prefix_and_stream(monkeypatch):
    ec = _reload(monkeypatch, ARB_EVAL_PREFIX="t:")
    assert ec.eval_prefix() == "t:"
    assert ec.eval_stream() == "t:eval:events"


def test_db_defaults_to_4(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    ec = _reload(monkeypatch)
    assert ec.eval_redis_db() == 4


def test_db_distinct_from_reserved(monkeypatch):
    # the pinned default must never collide with live(12)/memory-audit(3)/tests(15)
    ec = _reload(monkeypatch)
    assert ec.eval_redis_db() not in (3, 12, 15)
