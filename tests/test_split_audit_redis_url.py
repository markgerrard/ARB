"""ARB_AUDIT_REDIS_URL split from ARB_MEMORY_REDIS_URL.

Guards the security-boundary invariant of the split: the long-lived bridge
audit-emitter reads its own ARB_AUDIT_REDIS_URL (falling back to the historical
shared ARB_MEMORY_REDIS_URL), and BOTH publish-class credentials are stripped
from a non-FABA enqueue child env — while the coordination-bus creds
(AGENT_REDIS_*) that the enqueue child needs to LPUSH are preserved.

Each assertion is written to FAIL if the boundary regresses (e.g. if a future
credential var is added to PUBLISH_CREDENTIAL_ENV but a scrub site keeps a
literal, or if the resolver precedence flips).
"""

import os

import pytest

from agent_redis_bridge.dispatch_authority import (
    AUDIT_EMITTER_ENV,
    HARNESS_PUBLISH_ENV,
    PUBLISH_CREDENTIAL_ENV,
    filter_publish_env,
    pop_publish_env,
)
from agent_redis_bridge.engines._stdio import is_bus_credential
from agent_redis_bridge.bridge import resolve_audit_redis


def test_publish_credential_set_is_exactly_the_two_publish_vars():
    assert PUBLISH_CREDENTIAL_ENV == {HARNESS_PUBLISH_ENV, AUDIT_EMITTER_ENV}
    assert HARNESS_PUBLISH_ENV == "ARB_MEMORY_REDIS_URL"
    assert AUDIT_EMITTER_ENV == "ARB_AUDIT_REDIS_URL"


def test_filter_publish_env_drops_both_creds_but_keeps_coordination_creds():
    env = {
        "ARB_MEMORY_REDIS_URL": "rediss://mem/5",
        "ARB_AUDIT_REDIS_URL": "rediss://audit/5",
        "AGENT_REDIS_PASSWORD": "pw",
        "AGENT_REDIS_HOST": "h",
        "PATH": "/usr/bin",
    }
    out = filter_publish_env(env)
    # both publish creds gone
    assert "ARB_MEMORY_REDIS_URL" not in out
    assert "ARB_AUDIT_REDIS_URL" not in out
    # coordination creds preserved (the enqueue child still LPUSHes)
    assert out["AGENT_REDIS_PASSWORD"] == "pw"
    assert out["AGENT_REDIS_HOST"] == "h"
    assert out["PATH"] == "/usr/bin"
    # original mapping untouched (returns a copy)
    assert env["ARB_MEMORY_REDIS_URL"] == "rediss://mem/5"


def test_pop_publish_env_returns_memory_value_and_removes_both():
    d = {
        "ARB_MEMORY_REDIS_URL": "rediss://mem/5",
        "ARB_AUDIT_REDIS_URL": "rediss://audit/5",
        "KEEP": "1",
    }
    popped = pop_publish_env(d)
    assert popped["ARB_MEMORY_REDIS_URL"] == "rediss://mem/5"
    # both stripped from the mapping the child would inherit
    assert "ARB_MEMORY_REDIS_URL" not in d
    assert "ARB_AUDIT_REDIS_URL" not in d
    assert d == {"KEEP": "1"}


def test_pop_publish_env_absent_keys_is_noop():
    d = {"KEEP": "1"}
    assert pop_publish_env(d) == {}
    assert d == {"KEEP": "1"}


def test_engine_child_scrub_already_covers_audit_var():
    # The engine-child scrub (scrub_env_dict -> is_bus_credential) covers the new
    # audit var via its "_REDIS_URL" rule with no code change; assert it stays so.
    assert is_bus_credential("ARB_AUDIT_REDIS_URL") is True
    assert is_bus_credential("ARB_MEMORY_REDIS_URL") is True
    assert is_bus_credential("AGENT_REDIS_PASSWORD") is True


@pytest.fixture
def clean_audit_env(monkeypatch):
    for k in (
        "ARB_AUDIT_REDIS_URL",
        "ARB_MEMORY_REDIS_URL",
        "ARB_AUDIT_PREFIX",
        "ARB_MEMORY_PREFIX",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_resolver_prefers_audit_url_over_memory(clean_audit_env):
    clean_audit_env.setenv("ARB_AUDIT_REDIS_URL", "rediss://audit/5")
    clean_audit_env.setenv("ARB_MEMORY_REDIS_URL", "rediss://mem/5")
    url, _ = resolve_audit_redis({})
    assert url == "rediss://audit/5"


def test_resolver_falls_back_to_memory_url(clean_audit_env):
    clean_audit_env.setenv("ARB_MEMORY_REDIS_URL", "rediss://mem/5")
    url, _ = resolve_audit_redis({})
    assert url == "rediss://mem/5"


def test_resolver_env_file_fallback_for_both_vars(clean_audit_env):
    # URL present only in the parsed .env file must still arm vote emission.
    url, _ = resolve_audit_redis({"ARB_MEMORY_REDIS_URL": "rediss://file/3"})
    assert url == "rediss://file/3"
    url, _ = resolve_audit_redis(
        {"ARB_AUDIT_REDIS_URL": "rediss://afile/5", "ARB_MEMORY_REDIS_URL": "rediss://file/3"}
    )
    assert url == "rediss://afile/5"


def test_resolver_prefix_precedence(clean_audit_env):
    clean_audit_env.setenv("ARB_MEMORY_PREFIX", "mem:")
    _, prefix = resolve_audit_redis({})
    assert prefix == "mem:"
    clean_audit_env.setenv("ARB_AUDIT_PREFIX", "aud:")
    _, prefix = resolve_audit_redis({})
    assert prefix == "aud:"
