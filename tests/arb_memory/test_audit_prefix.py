import importlib

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

import arb_memory.audit as audit


def test_audit_default_prefix_comes_from_env(monkeypatch):
    # The audit consumer must be prefix-isolatable like the bus (env-configurable PREFIX), so an
    # isolated AuditRun(prefix=...) and the audit consumer agree on the stream. Was hardcoded "".
    monkeypatch.setenv("ARB_MEMORY_PREFIX", "audit-test:")
    reloaded = importlib.reload(audit)
    try:
        assert reloaded.PREFIX == "audit-test:"
        assert reloaded.audit_stream() == "audit-test:arbmem:audit"
    finally:
        monkeypatch.delenv("ARB_MEMORY_PREFIX", raising=False)
        importlib.reload(audit)
