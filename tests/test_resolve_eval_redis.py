import importlib

import pytest

bridge = importlib.import_module("agent_redis_bridge.bridge")


def test_env_file_arms_tee_when_process_env_absent(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    env = {"ARB_EVAL_REDIS_URL": "redis://prod:6379", "ARB_EVAL_REDIS_DB": "6", "ARB_EVAL_PREFIX": "p:"}
    url, db, prefix = bridge.resolve_eval_redis(env)
    assert url == "redis://prod:6379" and db == 6 and prefix == "p:"


def test_process_env_wins_over_env_file(monkeypatch):
    monkeypatch.setenv("ARB_EVAL_REDIS_URL", "redis://exported:6379")
    monkeypatch.setenv("ARB_EVAL_REDIS_DB", "6")
    env = {"ARB_EVAL_REDIS_URL": "redis://file:6379", "ARB_EVAL_REDIS_DB": "4"}
    url, db, _ = bridge.resolve_eval_redis(env)
    assert url == "redis://exported:6379" and db == 6


def test_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("ARB_EVAL_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_EVAL_REDIS_DB", raising=False)
    monkeypatch.delenv("ARB_EVAL_PREFIX", raising=False)
    url, db, prefix = bridge.resolve_eval_redis({})
    assert not url and db == 4 and prefix == ""


def test_non_numeric_db_raises_clear_error(monkeypatch):
    monkeypatch.setenv("ARB_EVAL_REDIS_DB", "notanint")
    with pytest.raises(ValueError, match="ARB_EVAL_REDIS_DB must be an integer"):
        bridge.resolve_eval_redis({})
