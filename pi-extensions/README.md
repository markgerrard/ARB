# pi extensions for AgentRedisBridge orchestration

> **Operating discipline lives in [`docs/pi-orchestrator-operating-guide.md`](../docs/pi-orchestrator-operating-guide.md)** —
> panel roster rules, vote-gap handling, auto-synthesis caveats, integration discipline. This README
> covers the mechanics only; read the guide before running a panel with these tools.

## `arb-dispatch-monitor.ts`

A pi extension that gives a warm pi orchestrator the missing Claude Code-style background dispatch notification primitive for AgentRedisBridge.

It registers:

- Tool: `arb_dispatch` — start `scripts/agent-dispatch` in the background, return immediately, and notify when it exits.
- Command: `/arb-dispatch {json}` — manual command form of the same launcher.
- Command: `/arb-adopt <task-id> [target-id]` — reattach a Redis task to the extension after `/reload` or session restart.
- Command: `/arb-adopt-run <run-id> [log-dir]` — reattach all tasks for a run by scanning `.arb/logs/*.err` for that run id and task ids.
- Command: `/arb-auto-synthesize {json}` — arm an opt-in completion barrier for a run. When every expected target for that run reaches a terminal state, the extension queues a follow-up user message to synthesize/arbitrate the panel.
- Command: `/arb-console` — debug-only/demoted in Phase 1; use `/arb-watch` for native compact visibility and `/arb-status` or `/arb-collect` for explicit snapshots/log snippets.
- Command: `/arb-watch [run-id|all]` — toggle a compact below-editor widget rendered from cached async Redis/log status. The widget does not stream task events into model context and does not take over input.
- Command: `/arb-hide` — hide the compact widget. Active jobs may continue polling in the background until terminal.
- Command: `/arb-status` — show jobs started in this pi session, including a one-shot Redis status snapshot when task ids are known.
- Command: `/arb-collect [chars]` — show stdout/stderr snippets for jobs started in this pi session.

Install globally:

```bash
mkdir -p ~/.pi/agent/extensions
ln -sf /home/<user>/AgentRedisBridge/pi-extensions/arb-dispatch-monitor.ts \
  ~/.pi/agent/extensions/arb-dispatch-monitor.ts
# then /reload inside pi
```

Or install per project:

```bash
mkdir -p /path/to/project/.pi/extensions
ln -sf /home/<user>/AgentRedisBridge/pi-extensions/arb-dispatch-monitor.ts \
  /path/to/project/.pi/extensions/arb-dispatch-monitor.ts
# then /reload inside pi from that trusted project
```

Example tool params:

```json
{
  "targetId": "codex-example-app-dev",
  "task": "Read /opt/example-app/.arb/patch-review-brief.md and review the current uncommitted patch.",
  "runId": "panel-example-app-p0-patch-...",
  "envFile": "/opt/example-app/.arb/envs/bridge-common.env",
  "fromAgentId": "pi-example-app-dev",
  "branch": "main",
  "cwd": "/opt/example-app",
  "autoSynthesize": true,
  "expectedTargets": [
    "codex-example-app-dev",
    "agy-example-app-dev",
    "asdk-example-app-dev-opus48"
  ],
  "deadlineMinutes": 45,
  "synthesisOutputPath": "/opt/example-app/.arb/panel-synthesis.md",
  "synthesisPrompt": "All seats for this run are terminal. Read every seat's stdout and stderr. Produce: (1) the dispatched roster vs the seats that actually returned a vote, naming any gap; (2) a per-seat stance table (seat -> verdict -> lead findings), facts separated from severity; (3) hinge-claim verification for each P0/P1/P2 before proposing remediation; (4) an arbitrated verdict you own, with vote gaps stated, never averaged away."
}
```

Further `arb_dispatch` parameters:

- **Authority path (Slice 1d-iv):** ordinary dispatches pre-mint via
  `scripts/arb-memory-harness-publish`, then enqueue with the quartet
  `--artefact-id` / `--version` / `--receipt` / `--brief` (no free-form
  positional task). Publish requires `ARB_MEMORY_REDIS_URL`; the enqueue
  spawn strips that credential.
- `worktree` / `worktreeBase` / `worktreeCleanup` — pass through `agent-dispatch --worktree` for
  hard isolation (the engine's cwd IS the worktree). Prefer for file-mutating dispatches and for
  per-reviewer isolation on concurrent panels (`worktree: "review-<engine>"`). These remain
  ordinary-path flags; the dispatcher translates them to `--worktree-json` after the quartet gate.
- `deadlineMinutes` — auto-synthesis barrier deadline. If all expected seats are not terminal by
  then, the extension queues a vote-gap follow-up (naming absent/non-terminal seats, with re-fire
  instructions) instead of waiting forever. Without it, one stuck seat means the barrier never fires.
- If `synthesisPrompt` is omitted, the default prompt enforces the anti-laundering contract
  (roster-vs-returned accounting, per-seat stance table, hinge-claim verification, named vote gaps).

If a dispatch produces no `task-id` within 60s, the extension warns about a possible misroute and
the widget shows `no task-id (misroute?)` — check the bridge log for `[turn-start]` before trusting
the wait.

The extension deliberately leaves running dispatchers alive on `session_shutdown`; the `agent-dispatch` process is the durable waiter and writes stdout/stderr under the selected log directory.

After `/reload`, in-memory job state is lost but Redis/log state is not. Reattach a running panel with:

```text
/arb-adopt-run panel-example-app-followup-review-20260705T1634
/arb-watch
```

or a single task with:

```text
/arb-adopt 8b50b7b2-84e4-49be-8bff-964aabbc5475 codex-example-app-dev
```

`/arb-watch` polls Redis asynchronously every ~3 seconds with a single-in-flight guard and bounded per-tick Redis subprocess fan-out, then renders from a cached snapshot. TUI render functions do not perform Redis, process, file, or network I/O.

The cached widget reads:

- `agent_scratch:task:<task-id>:status`
- `agent_scratch:agent:<target-id>:status`

This is UI-only observability: it does not add the Redis event stream to the model conversation unless you explicitly run `/arb-status`, `/arb-collect`, or ask the model to read logs.
