import importlib
bridge = importlib.import_module("agent_redis_bridge.bridge")

def test_audit_env_file_arms_when_process_env_absent(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    url, prefix = bridge.resolve_audit_redis({"ARB_MEMORY_REDIS_URL": "redis://prod:6379/5", "ARB_MEMORY_PREFIX": "p:"})
    assert url == "redis://prod:6379/5" and prefix == "p:"

def test_audit_process_env_wins(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://exported:6379/5")
    url, prefix = bridge.resolve_audit_redis({"ARB_MEMORY_REDIS_URL": "redis://file:6379/3"})
    assert url == "redis://exported:6379/5"

def test_audit_unset_is_none(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
    url, prefix = bridge.resolve_audit_redis({})
    assert not url and prefix == ""
