"""Per-dispatch reasoning-effort knob (codex engine + bridge routing)."""

from __future__ import annotations

import pytest

from agent_redis_bridge.engines.codex import (
    CODEX_EFFORT_LEVELS,
    CodexEngine,
    normalize_reasoning_effort,
)


def _engine(default_effort: str | None = None) -> CodexEngine:
    return CodexEngine(
        cwd="/tmp",
        model="gpt-5.6-sol",
        approval_policy="never",
        sandbox="danger-full-access",
        default_effort=default_effort,
    )


def test_turn_always_carries_explicit_effort_defaulting_to_medium():
    # codex STICKS the last-set effort on a warm thread, so every turn must carry an
    # explicit value or a prior --effort leaks into later dispatches. Default = medium.
    eng = _engine()
    eng.thread_id = "t1"
    assert eng.thread_start_params()["effort"] == "medium"
    assert eng.turn_params("do the thing", policy="trusted")["effort"] == "medium"


def test_seat_default_effort_is_configurable():
    eng = _engine(default_effort="high")
    eng.thread_id = "t1"
    assert eng.turn_params("x", policy="trusted")["effort"] == "high"


def test_per_dispatch_effort_overrides_seat_default():
    eng = _engine(default_effort="medium")
    eng.thread_id = "t1"
    eng.set_turn_reasoning_effort("xhigh")
    assert eng.thread_start_params()["effort"] == "xhigh"
    assert eng.turn_params("do the thing", policy="trusted")["effort"] == "xhigh"


def test_clearing_override_reverts_to_seat_default_no_leak():
    eng = _engine(default_effort="medium")
    eng.thread_id = "t1"
    eng.set_turn_reasoning_effort("xhigh")
    eng.set_turn_reasoning_effort(None)  # a later no-effort dispatch
    assert eng.turn_params("x", policy="trusted")["effort"] == "medium"


@pytest.mark.parametrize("level", sorted(CODEX_EFFORT_LEVELS))
def test_all_documented_levels_accepted(level):
    assert normalize_reasoning_effort(level) == level


def test_normalize_is_case_insensitive_and_trims():
    assert normalize_reasoning_effort("  High ") == "high"


def test_unknown_effort_rejected_loud():
    with pytest.raises(ValueError, match="reasoning effort"):
        normalize_reasoning_effort("turbo")


def test_none_and_empty_normalize_to_none():
    assert normalize_reasoning_effort(None) is None
    assert normalize_reasoning_effort("") is None
