# Codex retire-after-turn — implementation plan

Date: 2026-07-10 · Workflow A · Spec: `docs/superpowers/specs/2026-07-10-codex-retire-after-turn-design.md`

Small change: one env-default in `engines/codex.py` + three config tests + changelog. Engine-pool
retire mechanics are already shipped and tested (`672fef9`; `tests/test_engine_pool.py:128–200`) —
do NOT touch `engine_pool.py`.

## Task 1 — TDD: `retire_after_turn` config on CodexEngine

**Branch/worktree:** implemented in a bridge-created worktree (`--worktree codex-retire`) off `dev`.

**Step 1 — failing tests.** New file `tests/test_codex_retire.py`:

```python
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
```

Run: `pytest tests/test_codex_retire.py` — all three must FAIL (`AttributeError: retire_after_turn`).

**Step 2 — implementation.** In `CodexEngine.__init__` (src/agent_redis_bridge/engines/codex.py,
after `self.healthy = True` / `self._reasoning_effort = None` block, ~line 111):

```python
        # Retire the engine after every dispatch so the pool never re-serves the
        # ever-growing app-server thread (one 2026-07-08 thread served 24
        # unrelated dispatches, 22M cumulative tokens — cross-dispatch
        # contamination for panel seats). Explicit thread_id /
        # fork_from_thread_id continuations survive retirement: codex resumes
        # threads from ~/.codex/sessions rollouts on a fresh process
        # (live-proven 2026-07-10). Long-lived seats opt out with
        # BRIDGE_CODEX_RETIRE_AFTER_TURN=0.
        raw_retire = os.environ.get("BRIDGE_CODEX_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

Run: `pytest tests/test_codex_retire.py` → 3 pass. Then the full suite:
`pytest tests/` → no regressions (pay attention to `test_codex_io.py`, `test_bridge*.py`,
`test_engine_pool.py`).

**Step 3 — commit** on the worktree branch:
`fix(codex): retire engines after each dispatch (BRIDGE_CODEX_RETIRE_AFTER_TURN=0 opts out)`

## Task 2 — CHANGELOG entry (same commit or follow-up commit)

Add to `CHANGELOG.md` under today: what (codex engines now retire after every dispatch; opt-out
env var) AND why (one thread served 24 unrelated dispatches / 22M cumulative tokens — panel
cross-contamination, quota burn; continuity preserved via explicit `thread_id`, resume-across-
process live-proven).

## Orchestrator-owned (NOT the implementor's)

1. Review: agy-print reviews the commit (Workflow A serial review); verify from git, not reply prose.
2. Merge to `dev` (orchestrator integrates).
3. Deploy: fleet clone pull + restart 9 codex seats when idle (fleet-restart discipline).
4. Live gate per spec: distinct thread_ids across two self-contained dispatches + nonce
   continuation recall.
