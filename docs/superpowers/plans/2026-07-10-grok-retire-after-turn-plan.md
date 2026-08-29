# Grok-acp retire-after-turn — implementation plan

Date: 2026-07-10 · Workflow A · Spec: `docs/superpowers/specs/2026-07-10-grok-retire-after-turn-design.md`

Smallest change in the family: one env-default + three config tests + changelog. Do NOT touch
`engine_pool.py` or grok's turn/stop paths. Implemented in a bridge-created worktree off `dev`.

## Task 1 — TDD

**Step 1 — failing tests.** New file `tests/test_grok_retire.py`:

```python
"""Grok engines retire after each dispatch by default (session-accumulation fix)."""

from __future__ import annotations

from agent_redis_bridge.engines.grok_acp import GrokAcpEngine


def _engine() -> GrokAcpEngine:
    return GrokAcpEngine(cwd="/tmp", model=None)


def test_retire_after_turn_defaults_on(monkeypatch):
    monkeypatch.delenv("BRIDGE_GROK_RETIRE_AFTER_TURN", raising=False)
    assert _engine().retire_after_turn is True


def test_retire_after_turn_zero_disables(monkeypatch):
    monkeypatch.setenv("BRIDGE_GROK_RETIRE_AFTER_TURN", "0")
    assert _engine().retire_after_turn is False


def test_retire_after_turn_false_disables_case_insensitive(monkeypatch):
    monkeypatch.setenv("BRIDGE_GROK_RETIRE_AFTER_TURN", "False")
    assert _engine().retire_after_turn is False
```

Run `pytest tests/test_grok_retire.py` — all three must FAIL (`AttributeError`).

**Step 2 — implementation.** In `GrokAcpEngine.__init__` (src/agent_redis_bridge/engines/grok_acp.py,
end of the attribute block, ~line 60). Confirm `os` is imported; add if not.

```python
        # Retire the engine after every dispatch so the pool never re-serves the
        # accumulating ACP session (live-proven leak 2026-07-10: a self-contained
        # probe recalled a prior dispatch's review-brief title). grok-acp has no
        # session resume, so explicit thread continuations keep failing legibly
        # (bridge replies thread-continuation-unsupported). Long-lived seats opt
        # out with BRIDGE_GROK_RETIRE_AFTER_TURN=0 (launchd plist, not env file).
        raw_retire = os.environ.get("BRIDGE_GROK_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

Run: `pytest tests/test_grok_retire.py` → 3 pass. Then
`pytest tests/ --ignore=tests/arb_messages --ignore=tests/e2e -q` (per today's run policy —
do NOT run tests/e2e) → failures must be none.

**Step 3 — commit** on the worktree branch:
`fix(grok-acp): retire engines after each dispatch (BRIDGE_GROK_RETIRE_AFTER_TURN=0 opts out)`

## Task 2 — CHANGELOG entry (what AND why; may share the commit)

## Orchestrator-owned (NOT the implementor's)

1. agy-print certifying review; grok-bridge-dev non-certifying contributor voice (inline reply,
   NO file writes — the out-of-cwd permission bug).
2. Merge dev; wait for the in-flight e2e suite; push; fleet pull.
3. Idle-check + restart only the grok seat; live gates per spec.
