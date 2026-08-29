# Agent-sdk retire-after-turn — implementation plan

Date: 2026-07-10 · Workflow A · Spec: `docs/superpowers/specs/2026-07-10-agent-sdk-retire-after-turn-design.md`

Three small code changes + six tests + changelog. Do NOT touch `engine_pool.py` or the
worktree dispatch path. Implemented in a bridge-created worktree off `dev`.

## Task 1 — TDD: retire config + resume gating + smoke-once

**Step 1 — failing tests.** New file `tests/test_agent_sdk_retire.py`. Reuse the engine/args
construction patterns from the existing `tests/test_agent_sdk*.py` suite (do not invent new
fixtures where one exists). The six tests, exactly per the spec:

```python
"""Agent-sdk engines retire after each dispatch by default (session-accumulation fix)."""

from __future__ import annotations

from pathlib import Path

from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine


def _engine(tmp_path: Path, model: str = "haiku-4.5") -> AgentSdkEngine:
    return AgentSdkEngine(
        cwd="/tmp",
        model=model,
        tool_ceiling=None,
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
```

Tests 4–5 (API-key resume gating): use a non-subscription model (e.g. `"minimax-m3"`), write
`<session_root>/<agent_id>/last-session-id` with a known id BEFORE constructing the engine,
then assert on `engine._build_options().resume`:

- retire ON (env unset) → `resume is None`
- retire OFF (`=0`) → `resume == "<the persisted id>"`

If `_build_options()` needs env scaffolding for `isolated_env`, monkeypatch the minimum the
existing suite already monkeypatches — follow its pattern.

Test 6 (smoke-once at `build_engine` level): build an argparse-like namespace with
`engine="agent-sdk"`, `model="haiku-4.5"`, `_agent_sdk_key="dummy"`,
`agent_sdk_session_root=str(tmp_path)`, `_agent_sdk_primary_cwd="/tmp/x"`; call
`build_engine(args, cwd="/tmp/x")` twice → first engine `.live_smoke_test is True`, second
`.live_smoke_test is False`.

Run `pytest tests/test_agent_sdk_retire.py` — all must FAIL first (AttributeError /
resume mismatch / both-True).

**Step 2 — implementation.**

(a) `src/agent_redis_bridge/engines/agent_sdk.py`, end of `__init__` (~line 199):

```python
        # Retire the engine after every dispatch so the pool never re-serves the
        # accumulating ClaudeSDKClient conversation (the sonnet wiki-gate seat
        # stacked 15 unrelated dispatches into one 1.76MB session on 2026-07-07 —
        # cross-dispatch contamination inside a review gate). Long-lived seats
        # opt out with BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN=0 (launchd plist, not
        # the seat env file). Explicit resume_thread() continuation is unchanged.
        raw_retire = os.environ.get("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

(b) same file, `_build_options` (~line 278): replace `resume=self._last_session_id,` with

```python
            # Auto-resuming the persisted session on a fresh engine would silently
            # rebuild the accumulated conversation retirement exists to shed (and
            # exercises resume-at-connect, which the subscription comment below
            # documents as crash-prone, on every dispatch). Explicit resume_thread()
            # still resumes; the id is still loaded/persisted for observability.
            resume=None if self.retire_after_turn else self._last_session_id,
```

(c) `src/agent_redis_bridge/bridge.py`, agent-sdk branch of `build_engine` (~line 2740):
compute the smoke flag before constructing, mark it consumed:

```python
        live_smoke = (
            Path(cwd).resolve() == Path(getattr(args, "_agent_sdk_primary_cwd", cwd)).resolve()
            and not getattr(args, "_agent_sdk_smoke_test_done", False)
        )
        if live_smoke:
            # Once per daemon: the smoke test is a real model turn, and with
            # retire-after-turn a fresh engine spawns per dispatch — without this
            # sentinel every dispatch would pay a model call at engine start.
            args._agent_sdk_smoke_test_done = True
        return AgentSdkEngine(
            ...,
            live_smoke_test=live_smoke,
        )
```

Run: `pytest tests/test_agent_sdk_retire.py` → 6 pass. Then the full suite (`pytest tests/`)
— compare failures against base `dev` (6 known pre-existing diagnose-fixture failures); pay
attention to `tests/test_agent_sdk*.py` and `tests/test_engine_pool.py`.

**Step 3 — commit** on the worktree branch:
`fix(agent-sdk): retire engines after each dispatch (BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN=0 opts out)`

## Task 2 — CHANGELOG entry (what AND why; may share the commit)

## Orchestrator-owned (NOT the implementor's)

1. agy-print review → merge dev → push → fleet clone pull.
2. Restart 4 asdk seats after idle-check.
3. Live gate on sonnet5 per spec.
