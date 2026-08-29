from __future__ import annotations

import asyncio
from agent_redis_bridge.local_memory_mcp import _server_command
from pathlib import Path

import pytest
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine


LOCAL_MEMORY_SEARCH = "mcp__arb-memory-local__memory_search"


def _agent_sdk_engine(session_root: Path) -> AgentSdkEngine:
    return AgentSdkEngine(
        cwd="/tmp/project",
        model="minimax-m3",
        tool_ceiling="Read",
        key="K",
        session_root=session_root,
        startup_probe=False,
    )


def _set_local_mcp_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@localhost/arb_memory",
        "OPENAI_API_KEY": "sk-task8-test",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONPATH": "/repo:/repo/src",
    }
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@localhost/arb_memory")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def _gate_result(engine: AgentSdkEngine, tool_name: str):
    options = engine._build_options()
    assert options.can_use_tool is not None
    return asyncio.run(options.can_use_tool(tool_name, {}, ToolPermissionContext(tool_use_id="task8-gate")))


def test_local_memory_mcp_flag_set_adds_agent_sdk_mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_env = _set_local_mcp_env(monkeypatch)
    engine = _agent_sdk_engine(tmp_path)

    options = engine._build_options()

    assert options.mcp_servers == {
        "arb-memory-local": {
            "command": _server_command(),
            "args": [],
            "env": expected_env,
        }
    }


def test_local_memory_mcp_flag_unset_leaves_agent_sdk_mcp_servers_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    engine = _agent_sdk_engine(tmp_path)

    options = engine._build_options()

    assert options.mcp_servers == {}


def test_local_memory_mcp_flag_set_allows_real_agent_sdk_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_local_mcp_env(monkeypatch)
    engine = _agent_sdk_engine(tmp_path)

    result = _gate_result(engine, LOCAL_MEMORY_SEARCH)

    assert isinstance(result, PermissionResultAllow)
    assert (LOCAL_MEMORY_SEARCH, True, "allowed") in engine._gate_records


def test_local_memory_mcp_flag_unset_denies_real_agent_sdk_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    engine = _agent_sdk_engine(tmp_path)

    result = _gate_result(engine, LOCAL_MEMORY_SEARCH)

    assert isinstance(result, PermissionResultDeny)
    assert engine._gate_records[-1] == (LOCAL_MEMORY_SEARCH, False, f"{LOCAL_MEMORY_SEARCH} outside ceiling")


def test_local_memory_mcp_agent_sdk_flag_set_without_dsn_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)

    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        _agent_sdk_engine(tmp_path)._build_options()
