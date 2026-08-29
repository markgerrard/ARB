# Bridge parallelism: `--max-parallel`

The bridge accepts up to `--max-parallel` concurrent turns per agent instance.
Each in-flight turn owns its own engine process (e.g. Codex App Server)
because these engines hold a single in-flight turn per CLI session. (The
`gemini-acp` engine is deprecated as of 2026-07-03 — Google killed the
`gemini` CLI it drove, and `agent-dispatch`/`agent-bridge-ping` now reject
`--engine gemini-acp`. Use `agy-print` or another canonical-quorum seat
instead.)

## Defaults & flags

| Source | Value |
|---|---|
| CLI flag | `--max-parallel N` |
| Env var | `BRIDGE_MAX_PARALLEL=N` |
| Default | `1` (current pre-refactor behaviour) |

Resolution order: CLI flag wins, env var next, default `1` otherwise. An
invalid env value (`BRIDGE_MAX_PARALLEL=banana`) falls back to `1`.

## How it works

`EnginePool(factory, max_size)` in `src/agent_redis_bridge/engine_pool.py`
spawns engines lazily up to `max_size` and recycles released engines:

- `acquire(task_id)` → returns a started engine reserved to that task, or
  `None` when at capacity. A `None` return triggers the existing "bridge busy"
  reply so callers see no behaviour change at `max_parallel=1`.
- `release(task_id)` → returns the engine to the idle pool.
- `get(task_id)` → looks up the engine owning a task (used by `steer`/`cancel`
  control envelopes).
- `stop_all()` → terminates every spawned engine on shutdown.

The pool is engine-agnostic — it works with any object exposing `start()` and
`stop()`. Codex satisfies the `AgentEngine` protocol (as did Gemini ACP,
since deprecated — see above).

## Control envelopes with multiple in-flight tasks

When `max_parallel > 1` and multiple tasks are running, `steer`/`cancel`
envelopes **must** include `payload.task_id`. An ambiguous control envelope
(no `task_id`, multiple active tasks) is rejected with
`<kind>_rejected reason="task_id required (multiple active)"`.

When only one task is active the `task_id` field stays optional, matching the
previous semantics.

## Operational notes

- **Resource cost.** Each parallel slot = one extra `codex app-server` child
  process (previously also `gemini --acp`, since deprecated). Memory/CPU
  scale linearly. Sensible cap is 2–4.
- **Per-engine override.** If a specific engine turns out unsafe to run in
  parallel (e.g. a future engine that shares state across CLI sessions),
  override `max_parallel` in its systemd unit instead of globally.
- **Workspace isolation.** Each turn runs in the same `cwd` the bridge was
  launched with. Cross-task file conflicts are the caller's responsibility,
  same as today — typically arranged by giving each task its own git worktree.

## See also

- `docs/orchestrator-patterns.md` — how an orchestrator (Claude Code or
  similar) should *use* parallelism: git-worktree-per-task, zero-poll
  completion monitoring, dual-reviewer pattern, recurring-gotcha briefing.

## Enabling parallelism

For a specific systemd unit:

```ini
# /etc/systemd/system/agent-bridge-codex@dev.service
[Service]
Environment="BRIDGE_MAX_PARALLEL=2"
```

Or via the dispatcher / launcher script:

```bash
agent-redis-bridge --max-parallel 2 ...
```

Reload + restart the unit and verify with a quick concurrent dispatch — a
second `agent-dispatch` while the first is mid-turn should now be accepted
instead of returning `bridge busy with task <uuid>`.

## Confirming parallelism is actually working

The first instinct is to dispatch two tasks and snapshot
`LLEN agent_scratch:agent:<id>:inbox`. **Don't.** This will read `0` whether
parallelism is on or off, because `BLPOP` is the consumer side — the bridge
is blocked-waiting on the inbox, so Redis pops the message back to the
bridge atomically the instant an `LPUSH` lands. You'd only see a non-zero
length if (i) every engine slot is full *and* a third dispatch is queued
behind them, or (ii) the bridge has crashed mid-`BLPOP`.

The load-bearing signal is the **bridge's own log**. With two parallel
dispatches in flight you should see:

```
[turn-start] <task-a-uuid>
[turn-start] <task-b-uuid>      ← *before* any [turn-end]
[turn-end]   <task-a-uuid> ok ...
[turn-end]   <task-b-uuid> ok ...
```

Two `[turn-start]` lines back-to-back before either `[turn-end]` is what
parallelism looks like at the application layer. With `--max-parallel 1`
the second `[turn-start]` only appears after the first `[reply-sent]`,
because the second dispatch sits in the inbox waiting for the engine pool
to free up.
