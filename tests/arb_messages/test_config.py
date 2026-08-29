import pytest
from arb_messages.config import load_settings
BASE = {
    "ARB_MESSAGES_POSTGRES_DSN": "postgresql://x",
    "ARB_MESSAGES_ALLOWED_AGENTS": "agent-a,agent-b",
    "ARB_MESSAGES_ALLOWED_PROVIDERS": "cloudflare,azure",
}
def test_loads_required_fields():
    s = load_settings(BASE)
    assert s.postgres_dsn == "postgresql://x"
    assert s.allowed_agents == frozenset({"agent-a", "agent-b"})
    assert s.allowed_providers == frozenset({"cloudflare", "azure"})
    assert s.messages_enabled is True

def test_missing_required_vars_lists_all_in_one_error():
    with pytest.raises(ValueError) as exc:
        load_settings({})
    msg = str(exc.value)
    for var in ("ARB_MESSAGES_POSTGRES_DSN", "ARB_MESSAGES_ALLOWED_AGENTS", "ARB_MESSAGES_ALLOWED_PROVIDERS"):
        assert var in msg

def test_empty_allowlist_after_strip_fails():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_MESSAGES_ALLOWED_AGENTS": " , "})

def test_empty_provider_allowlist_after_strip_fails():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_MESSAGES_ALLOWED_PROVIDERS": " , "})

def test_kill_switch_string_one_comparison():
    assert load_settings({**BASE, "ARB_MESSAGES_ENABLED": "0"}).messages_enabled is False
    assert load_settings({**BASE, "ARB_MESSAGES_ENABLED": "1"}).messages_enabled is True

def test_agent_ids_lowercased():
    s = load_settings({**BASE, "ARB_MESSAGES_ALLOWED_AGENTS": "Agent-A"})
    assert s.allowed_agents == frozenset({"agent-a"})

def test_provider_ids_lowercased():
    s = load_settings({**BASE, "ARB_MESSAGES_ALLOWED_PROVIDERS": "Cloudflare"})
    assert s.allowed_providers == frozenset({"cloudflare"})

def test_defaults():
    s = load_settings(BASE)
    assert s.lease_seconds == 300
