from __future__ import annotations

import pytest

from arb_memory.local_read_policy import local_read_dsn


def test_local_read_dsn_requires_explicit_local_dsn() -> None:
    # ARB_MEMORY_LOCAL_MCP is NOT set here; the DSN gate fires regardless of it,
    # so the prose must state only that the DSN is missing and must not overclaim
    # that MCP was set.
    with pytest.raises(RuntimeError) as exc_info:
        local_read_dsn({})
    assert "ARB_MEMORY_LOCAL_DSN is missing/empty" in str(exc_info.value)
    assert "ARB_MEMORY_LOCAL_MCP is set" not in str(exc_info.value)


def test_local_read_dsn_allows_reader_for_same_writer_store() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql://arb_memory@127.0.0.1:5544/arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@127.0.0.1:5544/arb_memory",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]


def test_local_read_dsn_allows_same_store_with_different_role() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql://arb_memory@same-db:5432/arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@same-db:5432/arb_memory",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]


def test_local_read_dsn_rejects_cross_store_without_explicit_opt_in() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql://arb_memory@dev-db:5544/arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@prod-db:25060/arb_memory",
    }

    with pytest.raises(RuntimeError, match="does not match ARB_MEMORY_DSN"):
        local_read_dsn(env)


def test_local_read_dsn_allows_cross_store_only_with_explicit_opt_in() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql://arb_memory@dev-db:5544/arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@prod-db:25060/arb_memory",
        "ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE": "1",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]


def test_local_read_dsn_rejects_query_param_host_bypass_without_opt_in() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql:///arb_memory?host=dev-db&port=5432",
        "ARB_MEMORY_LOCAL_DSN": "postgresql:///arb_memory?host=prod-db&port=5432",
    }

    with pytest.raises(RuntimeError, match="does not match ARB_MEMORY_DSN"):
        local_read_dsn(env)


def test_local_read_dsn_allows_query_param_host_cross_store_with_opt_in() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql:///arb_memory?host=dev-db&port=5432",
        "ARB_MEMORY_LOCAL_DSN": "postgresql:///arb_memory?host=prod-db&port=5432",
        "ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE": "1",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]


def test_local_read_dsn_rejects_key_value_cross_store_without_opt_in() -> None:
    env = {
        "ARB_MEMORY_DSN": "host=dev-db port=5432 dbname=arb_memory user=arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "host=prod-db port=5432 dbname=arb_memory user=arbmem_local_reader",
    }

    with pytest.raises(RuntimeError, match="does not match ARB_MEMORY_DSN"):
        local_read_dsn(env)


def test_local_read_dsn_treats_default_port_spelling_as_same_store() -> None:
    env = {
        "ARB_MEMORY_DSN": "postgresql://arb_memory@same-db/arb_memory",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@same-db:5432/arb_memory",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]


def test_local_read_dsn_has_no_implicit_default_when_writer_dsn_absent() -> None:
    env = {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@127.0.0.1:5544/arb_memory",
    }

    assert local_read_dsn(env) == env["ARB_MEMORY_LOCAL_DSN"]
