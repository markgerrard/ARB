from __future__ import annotations

from pathlib import Path

import pytest
from agent_redis_bridge.local_memory_mcp import _server_command

from agent_redis_bridge.engines.codex import CodexEngine


def _codex_engine() -> CodexEngine:
    return CodexEngine(
        cwd="/tmp/project",
        model=None,
        approval_policy="never",
        sandbox="danger-full-access",
    )


def _set_local_mcp_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@localhost/arb_memory",
        "OPENAI_API_KEY": "sk-task7-test",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONPATH": "/repo:/repo/src",
    }
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@localhost/arb_memory")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


_BASE_ARGS = ["codex", "app-server", "--listen", "stdio://"]

_OVERRIDE_PREFIX = "mcp_servers.arb-memory-local="


def _arb_memory_override(args: list[str]) -> str | None:
    """Locate the arb-memory MCP override by name, never by position.

    `command_args()` appends `-c features.auto_memory=false` after this override
    when AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY=1, which reviewer seats set as a
    matter of hygiene. Indexing args[-1] silently reads that unrelated flag
    instead, so these tests match on the override's own prefix.
    """
    for arg in args:
        if arg.startswith(_OVERRIDE_PREFIX):
            return arg
    return None


def test_local_memory_mcp_flag_unset_leaves_codex_args_byte_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    # This test's claim is about the *whole* argv being untouched, so it owns
    # every input that can append to it — including the reviewer-seat flag.
    monkeypatch.delenv("AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY", raising=False)

    assert _codex_engine().command_args() == _BASE_ARGS


def test_local_memory_mcp_flag_appends_codex_config_override_without_toml_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_local_mcp_env(monkeypatch)
    home = tmp_path / "home"
    config_dir = home / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    original_config = '[profiles.default]\nmodel = "unchanged"\n'
    config_path.write_text(original_config, encoding="utf-8")
    original_mtime = config_path.stat().st_mtime_ns
    monkeypatch.setenv("HOME", str(home))

    args = _codex_engine().command_args()

    env_file = home / ".cache" / "agent-redis-bridge" / "arb-memory-local-mcp.env"
    expected_override = (
        f'mcp_servers.arb-memory-local={{command={_q(_server_command())}, args=[], '
        f'env={{ARB_MEMORY_LOCAL_ENV_FILE={_q(env_file)}, PATH="/opt/homebrew/bin:/usr/bin", '
        'PYTHONPATH="/repo:/repo/src"}}'
    )
    assert args[:4] == _BASE_ARGS
    assert _arb_memory_override(args) == expected_override
    assert config_path.read_text(encoding="utf-8") == original_config
    assert config_path.stat().st_mtime_ns == original_mtime


def _q(value: object) -> str:
    import json

    return json.dumps(str(value))


def test_local_memory_mcp_codex_override_keeps_secrets_out_of_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_local_mcp_env(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    args = _codex_engine().command_args()
    override = _arb_memory_override(args)
    assert override is not None

    # The claim is "out of argv", so check every argument, not just the override.
    argv = " ".join(args)
    assert "arbmem_local_reader" not in argv
    assert "sk-task7-test" not in argv
    assert "ARB_MEMORY_LOCAL_DSN" not in argv
    assert "OPENAI_API_KEY" not in argv

    env_file = home / ".cache" / "agent-redis-bridge" / "arb-memory-local-mcp.env"
    assert f"ARB_MEMORY_LOCAL_ENV_FILE={_q(env_file)}" in override
    assert env_file.is_file()
    assert (env_file.stat().st_mode & 0o777) == 0o600
    content = env_file.read_text(encoding="utf-8")
    assert "ARB_MEMORY_LOCAL_DSN=postgresql://arbmem_local_reader@localhost/arb_memory\n" in content
    assert "OPENAI_API_KEY=sk-task7-test\n" in content
    assert "PATH=" not in content


def test_local_memory_mcp_codex_override_omits_absent_optional_env_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "postgresql://arbmem_local_reader@localhost/arb_memory")
    monkeypatch.delenv("ARB_MEMORY_DSN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/repo:/repo/src")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    args = _codex_engine().command_args()

    env_file = home / ".cache" / "agent-redis-bridge" / "arb-memory-local-mcp.env"
    assert args[:4] == _BASE_ARGS
    assert _arb_memory_override(args) == (
        f'mcp_servers.arb-memory-local={{command={_q(_server_command())}, args=[], '
        f'env={{ARB_MEMORY_LOCAL_ENV_FILE={_q(env_file)}, PATH="/opt/homebrew/bin:/usr/bin", '
        'PYTHONPATH="/repo:/repo/src"}}'
    )
    assert "OPENAI_API_KEY" not in " ".join(args)
    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in content
    assert "ARB_MEMORY_LOCAL_DSN=" in content


def test_local_memory_mcp_codex_flag_set_without_dsn_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)

    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        _codex_engine().command_args()
