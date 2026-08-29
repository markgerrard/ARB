from __future__ import annotations

import json
from agent_redis_bridge.local_memory_mcp import _server_command
from typing import Any, Callable

import pytest

from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine
from agent_redis_bridge.engines.gemini_acp import GeminiAcpEngine
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine
from agent_redis_bridge.engines.kimi_code_acp import KimiCodeAcpEngine
from agent_redis_bridge.engines.mini_agent_acp import MiniAgentAcpEngine
from agent_redis_bridge.local_memory_mcp import local_memory_mcp_config


class FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)

    def flush(self) -> None:
        pass


class FakeStdout:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.lines = [json.dumps(message) + "\n" for message in messages]

    def __iter__(self):
        return iter(self.lines)


class FakeProcess:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(messages)
        self.stderr = FakeStdout([])
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminated = True


def _engine_cases() -> list[tuple[str, Callable[..., object], list[dict[str, Any]]]]:
    return [
        (
            "gemini",
            GeminiAcpEngine,
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "gemini-session"}},
            ],
        ),
        (
            "grok",
            GrokAcpEngine,
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "grok-session"}},
            ],
        ),
        (
            "cursor",
            CursorAcpEngine,
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {}},
                {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "cursor-session"}},
            ],
        ),
        (
            "kimi",
            KimiCodeAcpEngine,
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "kimi-session"}},
            ],
        ),
        (
            "mini",
            MiniAgentAcpEngine,
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "agentCapabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "mini-session"}},
            ],
        ),
    ]


def _session_new_params(fake: FakeProcess) -> dict[str, Any]:
    sent = [json.loads(line) for line in fake.stdin.lines]
    request = next(message for message in sent if message["method"] == "session/new")
    return request["params"]


def _set_local_mcp_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@localhost/arb_memory",
        "OPENAI_API_KEY": "sk-task6-test",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONPATH": "/repo:/repo/src",
    }
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@localhost/arb_memory")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.mark.parametrize(("name", "engine_cls", "messages"), _engine_cases())
def test_local_memory_mcp_flag_injects_launch_spec_for_all_acp_engines(
    name: str,
    engine_cls: Callable[..., object],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_env = _set_local_mcp_env(monkeypatch)
    fake = FakeProcess(messages)
    engine = engine_cls(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

    engine.start()  # type: ignore[attr-defined]

    assert _session_new_params(fake) == {
        "cwd": "/tmp/project",
        "mcpServers": [
            {
                "command": _server_command(),
                "args": [],
                "env": expected_env,
            }
        ],
    }, name


@pytest.mark.parametrize(("name", "engine_cls", "messages"), _engine_cases())
def test_local_memory_mcp_flag_unset_leaves_acp_mcp_servers_empty(
    name: str,
    engine_cls: Callable[..., object],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    fake = FakeProcess(messages)
    engine = engine_cls(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

    engine.start()  # type: ignore[attr-defined]

    assert _session_new_params(fake) == {"cwd": "/tmp/project", "mcpServers": []}, name


def test_local_memory_mcp_flag_requires_non_empty_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)

    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        local_memory_mcp_config()

    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "")
    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        local_memory_mcp_config()


@pytest.mark.parametrize(("name", "engine_cls", "messages"), _engine_cases())
def test_local_memory_mcp_missing_dsn_prevents_acp_session_start(
    name: str,
    engine_cls: Callable[..., object],
    messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)
    fake = FakeProcess(messages)
    engine = engine_cls(cwd="/tmp/project", model=None, popen_factory=lambda *args, **kwargs: fake)

    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        engine.start()  # type: ignore[attr-defined]

    assert not any(json.loads(line).get("method") == "session/new" for line in fake.stdin.lines), name


def test_local_memory_mcp_omits_absent_optional_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "postgresql://arbmem_local_reader@localhost/arb_memory")
    monkeypatch.delenv("ARB_MEMORY_DSN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/repo:/repo/src")

    config = local_memory_mcp_config()

    assert config is not None
    assert config["env"] == {
        "ARB_MEMORY_LOCAL_DSN": "postgresql://arbmem_local_reader@localhost/arb_memory",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONPATH": "/repo:/repo/src",
    }
    assert "OPENAI_API_KEY" not in config["env"]


def test_kimi_and_mini_inherit_gemini_acp_session_injection() -> None:
    assert KimiCodeAcpEngine.start_session is GeminiAcpEngine.start_session
    assert MiniAgentAcpEngine.start_session is GeminiAcpEngine.start_session
