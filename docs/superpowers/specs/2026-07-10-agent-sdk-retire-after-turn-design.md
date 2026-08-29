# Agent-sdk retire-after-turn — design/spec

Date: 2026-07-10 · Workflow A (Mark: "build it") · Author: warm orchestrator (inline)
Status: spec — third engine in the session-accumulation family (pi `9837803`, codex `2a1f198`)

## Problem (triaged CONFIRMED 2026-07-10)

`AgentSdkEngine` connects ONE `ClaudeSDKClient` at `start()` (agent_sdk.py:236) and every
dispatch `query()`s the same live client (`_run_turn`, agent_sdk.py:568). It never sets
`retire_after_turn`, so `engine_pool.release()` recycles it. The conversation stacks across
unrelated dispatches.

Empirical: the sonnet-5 WIKI REVIEW GATE seat (`asdk-bridge-dev-sonnet5`, launched
`--agent-sdk-oneshot`) served **15 dispatches over 5 hours in one 1.76 MB session** on
2026-07-07 — wiki reviews for six different projects stacked into one conversation
(session store `~/.local/state/agent-redis-bridge/agent-sdk-sessions/asdk-bridge-dev-sonnet5/
-Users-<user>/f6db3918-*.jsonl`). Contamination inside a production review gate + plan burn.

Exposure map:
- **Trusted stateful dispatches** (haiku45, project-e-opus48): CLEAN already — the bridge
  forces `payload.worktree` (bridge.py:975-984) and worktree dispatches build a fresh
  single-use engine (bridge.py:1053-1059) that is stopped after the task (bridge.py:1147).
- **`--agent-sdk-oneshot` seats** (sonnet5 wiki gate, bridge-opus48): the flag only waives the
  worktree rule and disables `resume_thread` — every dispatch rides the pooled client.
  ACCUMULATES. (The flag's name promises one-shot semantics it does not deliver.)
- **API-key (non-subscription) seats**: worst variant — `_build_options` connects with
  `resume=<persisted last-session-id>` (agent_sdk.py:278), so accumulation survives daemon
  restarts. No such seat live today, but the code path exists.

## Fix (three parts, all small)

### 1. `retire_after_turn` default ON (mirror pi/codex)

In `AgentSdkEngine.__init__`:

```python
raw_retire = os.environ.get("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN")
self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}
```

Env-var name follows the existing `BRIDGE_AGENT_SDK_*` family (`_SESSION_ROOT`, `_ONESHOT`).
Opt-out `=0`/`false` (case-insensitive) restores warm pooling; must be set in the launchd
plist, not the seat env file. No `engine_pool.py` changes (retire hook shipped `672fef9`).

### 2. Gate resume-at-connect on retire being OFF (API-key path)

`_build_options` currently always passes `resume=self._last_session_id`. With retirement, a
fresh engine per dispatch would auto-resume the accumulated conversation — silently defeating
the fix — and exercises the resume-at-connect crash the subscription comment documents
(agent_sdk.py:314-324) on every dispatch instead of rarely. Change to:

```python
resume=None if self.retire_after_turn else self._last_session_id,
```

Load/persist of `last-session-id` stays (observability, turn labels). `resume_thread()`
(explicit `--thread-id` continuation) is UNCHANGED — it reconnects with an explicit resume id
exactly as today. The subscription path already hardcodes `resume=None`; unchanged.

### 3. Live startup smoke test once per daemon

`build_engine` sets `live_smoke_test=True` for every primary-cwd engine (bridge.py:2750), and
the smoke test is a REAL model turn (`_run_startup_probe` — the "Startup self-test: call the LS
tool" message visible at the top of every session file). With retirement, that would add one
model call + seconds of latency per dispatch on plan-billed seats. Make it once per daemon
process: in `build_engine`'s agent-sdk branch, additionally require
`not getattr(args, "_agent_sdk_smoke_test_done", False)`, and set
`args._agent_sdk_smoke_test_done = True` when building with it enabled (same args-stashing
idiom as `_agent_sdk_primary_cwd`, bridge.py:273). The warmup engine keeps the smoke test;
retire-replacement engines skip it. The deterministic gate checks (`assert_serveable`,
`startup_probe=True`) stay on every engine — they're cheap and load-bearing (fail-closed gate
proof).

## Explicit non-goals

- Not fixing the subscription `--thread-id` silent-fresh-context latent bug (recorded in
  memory; no known consumer; separate decision).
- Not renaming/removing `--agent-sdk-oneshot` (its worktree-waiver role remains).
- Not touching the worktree dispatch path (already clean by construction).

## Accepted residuals (same class the pi/codex fixes accepted)

- Warmup engine retired immediately after release; per-dispatch cold start = CLI spawn +
  connect + deterministic gate checks (a few seconds; no model turn after part 3).
- One session-store JSONL per dispatch accrues under the seat's session root (small; they
  already accrue per daemon restart).

## Tests (new file `tests/test_agent_sdk_retire.py`, mirror `tests/test_codex_retire.py`)

1. default (env unset) → `retire_after_turn is True`
2. `BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN=0` → `False`
3. `"False"` (case-insensitive) → `False`
4. API-key engine with a persisted `last-session-id`, retire ON → `_build_options().resume is None`
5. same, retire OFF (env `=0`) → `_build_options().resume == persisted id`
6. `build_engine` twice on one args namespace → first engine `live_smoke_test=True`, second `False`

Follow the construction patterns already used in `tests/test_agent_sdk*.py` (the suite has ~80
tests; do not invent new fixtures where one exists).

## Live gate (post-deploy)

On `asdk-bridge-dev-sonnet5` (oneshot seat — the pooled path): nonce dispatch → self-contained
"recall any nonce" dispatch must answer NONE with a DIFFERENT `thread_id`. Seat restarts follow
fleet-restart discipline (idle-check first; wiki gate must not be mid-run).

## Deployment

Merge dev → fleet clone pull → restart the 4 asdk seats (`com.example.asdk-sonnet-bridge.bridge-dev`,
`com.example.arbseat.asdk-bridge-dev-opus48`, `com.example.arbseat.asdk-bridge-dev-haiku45`,
`com.example.arbseat.asdk-project-e-dev-opus48`).
