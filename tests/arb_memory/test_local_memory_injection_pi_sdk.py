from __future__ import annotations

import pytest
from agent_redis_bridge.local_memory_mcp import _server_command

from agent_redis_bridge.engines.pi_sdk import PiSdkEngine


def _pi_sdk_engine() -> PiSdkEngine:
    return PiSdkEngine(cwd="/tmp/project", model="minimax/MiniMax-M3")


def _set_local_mcp_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@localhost/arb_memory",
        "OPENAI_API_KEY": "sk-task5-test",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONPATH": "/repo:/repo/src",
    }
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@localhost/arb_memory")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_local_memory_mcp_flag_set_adds_pi_sdk_mcp_server_with_pi_side_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_env = _set_local_mcp_env(monkeypatch)

    params = _pi_sdk_engine().thread_start_params()

    assert params == {
        "cwd": "/tmp/project",
        "model": "minimax/MiniMax-M3",
        "mcpServers": [
            {
                "command": _server_command(),
                "args": [],
                "env": expected_env,
                "name": "arb-memory-local",
            }
        ],
    }


def test_local_memory_mcp_flag_unset_leaves_pi_sdk_mcp_servers_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)

    params = _pi_sdk_engine().thread_start_params()

    assert params == {
        "cwd": "/tmp/project",
        "model": "minimax/MiniMax-M3",
    }
    assert "mcpServers" not in params
