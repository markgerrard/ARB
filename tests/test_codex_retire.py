"""Codex engines retire after each dispatch by default (session-accumulation fix)."""

from __future__ import annotations

from agent_redis_bridge.engines.codex import CodexEngine


def _engine() -> CodexEngine:
    return CodexEngine(
        cwd="/tmp",
        model="gpt-5.6-sol",
        approval_policy="never",
        sandbox="danger-full-access",
    )


def test_retire_after_turn_defaults_on(monkeypatch):
    monkeypatch.delenv("BRIDGE_CODEX_RETIRE_AFTER_TURN", raising=False)
    assert _engine().retire_after_turn is True


def test_retire_after_turn_zero_disables(monkeypatch):
    monkeypatch.setenv("BRIDGE_CODEX_RETIRE_AFTER_TURN", "0")
    assert _engine().retire_after_turn is False


def test_retire_after_turn_false_disables_case_insensitive(monkeypatch):
    monkeypatch.setenv("BRIDGE_CODEX_RETIRE_AFTER_TURN", "False")
    assert _engine().retire_after_turn is False
