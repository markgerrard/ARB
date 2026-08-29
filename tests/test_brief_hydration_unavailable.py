"""Parse-only seat refuses object tasks before prompt construction (1d-iv Task 5)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import TurnResult


def _request_with_task(task):
    return Envelope(
        id="req-1",
        sender="claude-x",
        branch="dev",
        recipient="codex-x",
        kind="request",
        sent_at="2026-07-28T00:00:00+00:00",
        payload={"task": task},
        run_id="run-1",
    )


def test_ref_on_parse_only_seat_is_brief_hydration_unavailable(monkeypatch):
    """A valid ref must never reach build_task_prompt or the engine."""
    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30)
    bridge.brief_hydrate_ready = False
    engine = MagicMock()
    prompts: list[str] = []

    def boom(*args, **kwargs):
        raise AssertionError("build_task_prompt must not be called for ref tasks")

    monkeypatch.setattr(
        "agent_redis_bridge.bridge.build_task_prompt",
        boom,
    )
    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-1", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert isinstance(result, TurnResult)
    assert result.ok is False
    assert result.error == "brief_hydration_unavailable"
    engine.run_turn_with_progress.assert_not_called()
    # The dict string representation must never appear in a prompt.
    assert prompts == []
    assert "{'artefact_id'" not in (result.result or "")
    assert "art-1" not in (result.result or "")


def test_legacy_string_still_reaches_prompt_builder(monkeypatch):
    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30)
    bridge.brief_hydrate_ready = False
    engine = MagicMock()
    engine.run_turn_with_progress.return_value = TurnResult(ok=True, result="ok")
    seen: list[str] = []

    def capture(task, **kwargs):
        seen.append(task)
        return f"PROMPT:{task}"

    monkeypatch.setattr("agent_redis_bridge.bridge.build_task_prompt", capture)
    monkeypatch.setattr(
        Bridge,
        "role_profile_for_turn",
        lambda self, engine, task_override=None: None,
    )
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task("do the legacy work"),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is True
    assert seen == ["do the legacy work"]
    engine.run_turn_with_progress.assert_called_once()
    assert "PROMPT:do the legacy work" in engine.run_turn_with_progress.call_args[0][0]
