from __future__ import annotations

from pathlib import Path

import pytest

from agent_redis_bridge import local_memory_mcp
from agent_redis_bridge.local_memory_mcp import local_memory_mcp_config


DEV_DSN = "postgresql://reader:p=a?ss@dev-host:25060/arbmem?sslmode=require"
PROD_DSN = "postgresql://reader:secret@prod-host:25060/arbmem?sslmode=require"


def write_readers(home: Path, body: str) -> None:
    d = home / ".arb-memory-local"
    d.mkdir(parents=True, exist_ok=True)
    (d / "readers.env").write_text(body, encoding="utf-8")


@pytest.fixture()
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("ARB_MEMORY_LOCAL_MCP", "ARB_MEMORY_LOCAL_DSN", "ARB_MEMORY_DSN", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_dev_tier_selects_dev_dsn(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f'export ARB_MEMORY_LOCAL_DSN_DEV="{DEV_DSN}"\n'
        f'export ARB_MEMORY_LOCAL_DSN_PROD="{PROD_DSN}"\n'
        'export OPENAI_API_KEY="sk-readers"\n',
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    config = local_memory_mcp_config()
    assert config is not None
    # value split on FIRST '=' only; quotes stripped; export prefix handled
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == DEV_DSN
    assert config["env"]["OPENAI_API_KEY"] == "sk-readers"


def test_prod_tier_selects_prod_dsn(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n"
        f"export ARB_MEMORY_LOCAL_DSN_PROD={PROD_DSN}\n",
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "prod")
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == PROD_DSN


def test_missing_readers_file_is_feature_absent(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    assert local_memory_mcp_config() is None


def test_empty_tier_dsn_is_feature_absent(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(clean_env, "export ARB_MEMORY_LOCAL_DSN_DEV=\n")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    assert local_memory_mcp_config() is None


def test_readers_key_wins_over_process_env_openai_key(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n"
        "export OPENAI_API_KEY=sk-from-readers\n",
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-process")
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["OPENAI_API_KEY"] == "sk-from-readers"


def test_tier_cross_store_mismatch_still_raises(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(clean_env, f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    monkeypatch.setenv(
        "ARB_MEMORY_DSN", "postgresql://writer:pw@other-host:25060/arbmem?sslmode=require"
    )
    with pytest.raises(RuntimeError, match="does not match"):
        local_memory_mcp_config()


def test_legacy_flag_with_env_dsn_unchanged(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == DEV_DSN


def test_legacy_flag_without_dsn_still_raises(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN"):
        local_memory_mcp_config()


def test_flag_absent_is_none(clean_env: Path) -> None:
    assert local_memory_mcp_config() is None


def test_command_is_venv_anchored_when_sibling_binary_exists(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "venv-bin"
    bin_dir.mkdir()
    (bin_dir / "arb-memory-local-mcp").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(local_memory_mcp.sys, "executable", str(bin_dir / "python3"))
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["command"] == str(bin_dir / "arb-memory-local-mcp")


def test_command_falls_back_to_bare_name(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_bin = tmp_path / "no-binary-here"
    empty_bin.mkdir()
    monkeypatch.setattr(local_memory_mcp.sys, "executable", str(empty_bin / "python3"))
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["command"] == "arb-memory-local-mcp"
