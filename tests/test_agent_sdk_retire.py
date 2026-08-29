"""Agent-sdk engines retire after each dispatch by default (session-accumulation fix)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_redis_bridge.bridge import build_engine
from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine


def _engine(tmp_path: Path, model: str = "haiku-4.5") -> AgentSdkEngine:
    return AgentSdkEngine(
        cwd="/tmp",
        model=model,
        tool_ceiling="Read",
        key="dummy-key",
        session_root=tmp_path,
        startup_probe=False,
    )


def test_retire_after_turn_defaults_on(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN", raising=False)
    assert _engine(tmp_path).retire_after_turn is True


def test_retire_after_turn_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN", "0")
    assert _engine(tmp_path).retire_after_turn is False


def test_retire_after_turn_false_disables_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN", "False")
    assert _engine(tmp_path).retire_after_turn is False


def test_retire_after_turn_disables_vendor_session_resume(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN", raising=False)
    session_file = tmp_path / "agent-sdk-m3" / "last-session-id"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("sid-persist\n", encoding="utf-8")

    assert _engine(tmp_path, model="minimax-m3")._build_options().resume is None


def test_retire_after_turn_opt_out_preserves_vendor_session_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN", "0")
    session_file = tmp_path / "agent-sdk-m3" / "last-session-id"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("sid-persist\n", encoding="utf-8")

    assert _engine(tmp_path, model="minimax-m3")._build_options().resume == "sid-persist"


def test_build_engine_runs_live_smoke_only_once(tmp_path):
    args = SimpleNamespace(
        engine="agent-sdk",
        model="haiku-4.5",
        _agent_sdk_key="dummy",
        agent_sdk_tools="Read",
        agent_sdk_session_root=str(tmp_path),
        _agent_sdk_primary_cwd="/tmp/x",
    )

    first = build_engine(args, cwd="/tmp/x")
    second = build_engine(args, cwd="/tmp/x")

    assert first.live_smoke_test is True
    assert second.live_smoke_test is False
