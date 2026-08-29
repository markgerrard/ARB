# SPEC — pi ARB dispatch monitor native widget (Phase 1)

Date: 2026-07-05
Author: pi orchestrator (inline)
Status: draft for panel review
Supersedes design artefacts:

- `/opt/example-app/.arb/arb-visibility-pi-widget-design-v2-20260705.md`
- `/opt/example-app/.arb/arb-visibility-pi-widget-panel-synthesis-v2-20260705.md`

Target file:

- `/home/<user>/AgentRedisBridge/pi-extensions/arb-dispatch-monitor.ts`

## 1. Purpose

Refactor the existing pi ARB dispatch monitor extension so `/arb-watch` becomes a native compact below-editor pi widget that shows ARB dispatch progress without taking over the screen and without streaming Redis/log events into model context.

This spec covers **Phase 1 only**:

- correct resource/lifecycle bugs in the current extension;
- render a safe compact widget from cached state;
- poll Redis/log/process status asynchronously outside render;
- classify queued/running/quiet-alive/stale/completed/failed/timeout/killed states conservatively;
- demote the current full-screen console to debug.

Later phases (durable sidecar restore, full live-stream integration, audit DB lookup, bridge-layer vote-status fields) are out of scope except where Phase 1 code must leave seams for them.

## 2. Non-goals

Phase 1 must **not**:

- change bridge protocol or daemon code;
- add npm dependencies;
- query ARB Memory/audit Postgres directly;
- make `events:live` the authoritative source of run membership;
- stream task events into the LLM conversation by default;
- implement a full two-pane console or replacement-screen UI;
- remove existing `arb_dispatch`, `/arb-status`, `/arb-collect`, `/arb-adopt`, or `/arb-adopt-run` functionality.

## 3. Existing behavior to preserve

The extension currently provides:

- tool `arb_dispatch`;
- commands `/arb-dispatch`, `/arb-status`, `/arb-collect`, `/arb-watch`, `/arb-hide`, `/arb-console`, `/arb-adopt`, `/arb-adopt-run`;
- background `scripts/agent-dispatch` launcher with stdout/stderr logs under `.arb/logs`;
- notification when the dispatcher exits;
- task id parsing from stderr line `task-id: <uuid>`;
- manual reattachment via `/arb-adopt` and `/arb-adopt-run`.

Phase 1 must preserve those external entry points, except `/arb-console` may become an alias to `/arb-debug-console` or may warn that it is debug-only.

## 4. Required fixes

### 4.1 Parent file descriptor leak

`launchDispatch()` opens `stdoutPath` and `stderrPath` with `openSync()` and passes the fds to `spawn()`. Phase 1 must close the parent copies immediately after `spawn()` returns.

Acceptance:

- parent `outFd` and `errFd` are closed in all spawn-success paths;
- if `spawn()` throws synchronously, any opened fds are closed before rethrow/notification;
- child logging still works.

### 4.2 Reload/timer cleanup

Extension reload can leave old timers alive. Add a module-level `globalThis` cleanup registry.

Required pattern:

```ts
const CLEANUP_KEY = "__arbDispatchMonitorCleanup";
const previousCleanup = (globalThis as Record<string, unknown>)[CLEANUP_KEY];
if (typeof previousCleanup === "function") previousCleanup();
(globalThis as Record<string, unknown>)[CLEANUP_KEY] = () => {
  // clear timers, hide widget if safe, clear stale context references
};
```

Acceptance:

- `/reload` cannot create duplicate poll timers from old extension instances;
- `session_shutdown` clears active timers and widget state;
- timers call `unref?.()` where available.

### 4.3 No synchronous polling I/O

Phase 1 must not use `spawnSync()` or other synchronous subprocess/network I/O in either:

- TUI component `render(width)`; or
- the background poll timer.

Reason: Node's single event loop drives pi UI. Synchronous `redis-cli` calls in a timer still freeze the editor while they run.

Acceptance:

- polling uses async `spawn`/`execFile` or another asynchronous Redis access path;
- a slow Redis command cannot overlap unboundedly with the next poll tick;
- there is a single-in-flight guard (`pollInFlight` or equivalent);
- timeout/cancellation of individual poll subprocesses is bounded;
- each poll tick caps Redis subprocess fan-out/concurrency; `pollInFlight` prevents overlapping ticks, but it is not sufficient by itself if one tick can spawn unbounded per-job/per-field commands.

### 4.4 Safe Redis parsing

Do not parse arbitrary Redis values with newline-split `HGETALL` alignment.

Phase 1 minimum:

- status reads use a fixed field list:
  - `task_id`
  - `state`
  - `phase`
  - `last_summary`
  - `updated_at`
  - `ok`
  - `error`
  - `queue_depth`
  - `enqueued_at`
- reads must preserve multiline field values or avoid reading fields that can break alignment.

Acceptable implementations:

1. async `redis-cli --eval` Lua that returns `cjson.encode(...)` for the fixed fields; or
2. async `redis-cli HMGET key field...` plus a RESP/JSON/CSV-safe output mode; or
3. async invocation of a tiny Python helper using `redis` if available; or
4. individual async `HGET` calls for each field, batched/concurrency-limited.

If using per-field `HGET`, cap intra-tick concurrency so a panel with many jobs cannot spawn dozens of `redis-cli` processes at once.

Task event reads may be reduced in Phase 1. If event stream parsing remains line-based, treat event `data` as opaque and fail soft rather than corrupting status.

## 5. State model

Implement/maintain an in-memory snapshot separate from render.

```ts
type ArbTaskState =
  | "launching"
  | "queued"
  | "running"
  | "quiet-alive"
  | "stale"
  | "completed"
  | "failed"
  | "timeout"
  | "killed"
  | "unknown";

type ArbVote = {
  stance?: string;
  severity?: string;
  refs?: string[];
  note?: string;
  source: "live" | "stdout" | "audit";
  seenAt?: string;
};

type ArbJob = {
  id: string;
  runId: string;
  targetId: string;
  engine: string;
  task: string;
  taskId?: string;
  state: ArbTaskState;
  rawState?: string;
  phase?: string;
  ok?: boolean;
  summary?: string;
  error?: string;
  startedAt: number;
  finishedAt?: number;
  updatedAt?: string;
  lastTaskEventAt?: string;
  queueDepth?: number;
  enqueuedAt?: string;
  stdoutPath: string;
  stderrPath: string;
  envFile: string;
  redisPrefix: string;
  pid?: number;
  processAlive?: boolean;
  seatHeartbeatTtl?: number;
  vote?: ArbVote;
  recentEvents: string[];
};

type WatchSnapshot = {
  jobs: ArbJob[];
  generatedAt: number;
  pollError?: string;
};
```

Existing fields may be adapted; the important boundary is that render reads a snapshot and never performs I/O.

## 6. Polling rules

### 6.1 Poll lifecycle

The poller runs when either condition is true:

- `/arb-watch` is visible; or
- at least one known local/adopted job has a non-terminal state.

It may stop after all known jobs are terminal and the widget is hidden.

### 6.2 Poll interval

Default interval: 3000 ms.

If a poll takes longer than the interval:

- do not start a second poll;
- render a stale-but-not-failed snapshot with a small `poll delayed`/`poll slow` indicator only in expanded/debug detail.

### 6.3 Task id refresh

Each poll may parse stderr for missing task IDs using existing `parseTaskId(stderrPath)`. This is local file I/O; for Phase 1 it may remain synchronous **only outside render**, but prefer keeping it small and bounded.

### 6.4 Status refresh

For each known task id:

- read fixed status fields from `${prefix}task:<task-id>:status`;
- map status fields onto the cached job;
- terminal Redis states (`completed`, `failed`) override local process state;
- dispatcher process exit states (`timeout`, `killed`) fill gaps when Redis status is absent or stale.

### 6.5 Seat bridge heartbeat

For quiet-alive/stale classification, Phase 1 should read the seat heartbeat TTL from task Redis:

- key: `${prefix}agent:<targetId>:status`
- signal: TTL > 0 means bridge seat is alive.

This is separate from `events:live` `turn_heartbeat`, and the UI must not conflate them.

### 6.6 Queue metadata

`queue_depth` and `enqueued_at` are written before enqueue and persist in the merged status hash after the bridge changes `state` to running/completed.

Display rule:

- if effective state is `queued`, show queue depth/enqueued time as active facts;
- otherwise show them only in expanded/debug metadata or omit them.

## 7. State classification

Effective state is derived from Redis status + dispatcher process + seat heartbeat.

Terminal precedence:

1. If child exit set `state` to `timeout` or `killed`, keep that unless Redis says `completed`/`failed` with newer evidence.
2. If Redis `state=completed`, effective state is `completed` and `ok` is parsed from status if present. `ok` is a Redis string; parse only `"true"` as true and `"false"` as false. Do not use `Boolean(status.ok)`, because `Boolean("false") === true`.
3. If Redis `state=failed`, effective state is `failed`.

Running/quiet/stale classification for non-terminal tasks:

- `queued`: Redis `state=queued`.
- `running`: Redis `state=running` with recent `updated_at` (<= `STALE_GRACE_MS`, default 180000).
- `quiet-alive`: Redis says running but `updated_at` is older than `STALE_GRACE_MS`, while either:
  - dispatcher process is alive and age since last update <= `PROCESS_STALE_GRACE_MS` (default 600000), or
  - seat heartbeat TTL > 0.
- `stale`: Redis says running and all are true:
  - latest known task status/event update is older than `STALE_GRACE_MS`;
  - no known local dispatcher process is alive, or process is alive but Redis has not changed for more than `PROCESS_STALE_GRACE_MS`;
  - seat heartbeat TTL is missing/dead/unknown.

`stale` is warning-only. The widget must not suggest automatic cancellation.

## 8. Widget rendering

### 8.1 Placement

`/arb-watch` sets a widget below the editor:

```ts
ctx.ui.setWidget(WIDGET_KEY, (tui, theme) => component, { placement: "belowEditor" })
```

If the current pi version supports only array widgets, Phase 1 should prefer component form and fail soft to string array only if necessary.

### 8.2 Render purity

The component's `render(width)` must:

- read only `WatchSnapshot` and local cached render state;
- perform no Redis, process, file, or network I/O;
- return lines whose visible width does not exceed `width`.

### 8.3 Visual language

Use pi theme colors:

- `accent`: active/running;
- `success`: completed ok;
- `warning`: queued, quiet-alive, stale, vote-gap;
- `error`: failed, timeout, killed;
- `dim`/`muted`: metadata.

Suggested glyphs:

- running: spinner frame or `●`;
- queued: `◦`;
- completed: `✓`;
- warning/stale/vote-gap: `⚠` or `■`;
- failed/timeout/killed: `✗`.

### 8.4 Compact layout

Compact mode should show:

- one header with run/job counts;
- one row per visible job, bounded;
- short detail line for running/quiet/stale jobs;
- hidden count if many jobs exceed line budget.

Example:

```text
⠹ ARB panel panel-arb-visibility-design-v2 · 2 running · 1 done
├─ ⠹ codex-example-app-dev · running/responding · task 79980f71 · 1m
│  ⎿ Streaming response… · status 8s ago
└─ ⚠ asdk-example-app-dev-opus48 · quiet-alive/command · task c1f03dfb · 4m
   ⎿ no recent status · seat heartbeat alive
```

### 8.5 Expanded detail

If `ctx.ui.getToolsExpanded?.()` is true, include bounded details:

- stdout/stderr paths;
- task id;
- run id;
- raw Redis state/phase;
- status age;
- seat heartbeat TTL;
- recent event summaries if available;
- vote status if available.

Use optional chaining for `getToolsExpanded`.

### 8.6 Adaptive budget

The widget must not monopolize the screen.

- if terminal has very little room, render a single summary line;
- otherwise render a small bounded set of jobs;
- prefer active jobs before completed historical jobs;
- show `+N more` when hiding jobs.

## 9. Vote parsing in Phase 1

Phase 1 may parse final stdout for full fenced vote blocks after a job is terminal.

Important: dispatcher stdout is a JSON object whose `result` field contains the seat's textual reply. The parser must:

1. read stdout file;
2. JSON-decode it;
3. get `.result` as string;
4. scan that string for fenced block:

````markdown
```vote
{"stance":"accept","severity":"P2","refs":["..."],"note":"..."}
```
````

5. JSON-decode the block;
6. store `vote.source="stdout"` with stance/severity/refs/note.

If stdout is empty, invalid JSON, lacks `.result`, lacks the fence, or has invalid vote JSON, fail soft.

Vote gap display in Phase 1:

- only compute gaps for jobs known from local launch/adoption roster where `auditPanel` was true or inferred from stderr `panel: bridge will record vote...`;
- do not infer expected seats from bounded live stream alone.

## 10. Commands

### 10.1 `/arb-watch [run-id|all]`

- toggles widget visibility if called with no args;
- if a run id is supplied, filter/prioritize jobs for that run;
- start poller if needed;
- notify: `ARB watch visible (cached Redis/log status; no model context streaming)`.

### 10.2 `/arb-hide`

- hides widget;
- does not clear jobs;
- poller may continue while active jobs exist.

### 10.3 `/arb-console` and `/arb-debug-console`

- register `/arb-debug-console` only if it is changed to render from the same async cached snapshot as `/arb-watch`; the current console render path calls Redis refresh from `render()` and must not be preserved unchanged;
- `/arb-console` should either alias to `/arb-debug-console` with a warning or be updated to say debug-only;
- if converting the debug console to cached rendering would expand Phase 1 too much, disable/hide it rather than leaving synchronous render-time Redis I/O in a debug path.

### 10.4 Existing commands

`/arb-status`, `/arb-collect`, `/arb-adopt`, `/arb-adopt-run`, `/arb-dispatch` must continue to work.

Any Redis reads in these preserved commands may be one-shot command-handler work, but they must not reuse brittle multiline `HGETALL` parsing where the output is used for structured state. Prefer the same safe status helper used by the poller.

`/arb-collect` remains explicit and bounded. Do not expand its default output as part of Phase 1.

## 11. Security and file hygiene

- Never render or notify env file contents, Redis passwords, API tokens, or full task text unless explicitly requested.
- Do not write sidecar files in Phase 1 unless needed. If a sidecar is introduced, create it mode `0600`, store no secrets, and put it under `.arb/`.
- `.arb/` stays gitignored in adopter repos.

## 12. Implementation notes

### 12.1 Single-file constraint

Phase 1 should stay in `pi-extensions/arb-dispatch-monitor.ts` to preserve existing symlink installation. Internal helper functions/types are acceptable.

### 12.2 Async command helper

Add a helper for bounded async subprocess execution, e.g.:

```ts
async function execFileLines(cmd: string, args: string[], opts: { env?: NodeJS.ProcessEnv; timeoutMs: number }): Promise<string[]>;
```

It must:

- use `spawn` or `execFile`, not `spawnSync`;
- collect stdout/stderr up to a small cap;
- kill on timeout;
- resolve with structured failure rather than throwing through the poll loop.

### 12.3 Poll concurrency guard

```ts
let pollInFlight = false;
async function pollOnce(ctx) {
  if (pollInFlight) return;
  pollInFlight = true;
  try { ... } finally { pollInFlight = false; }
}
```

Also cap work *inside* one poll tick. For example, read at most N task statuses concurrently, or prefer one Lua/Python helper call per task/run. The guard above prevents overlapping ticks; it does not by itself prevent one tick from spawning too many subprocesses.

### 12.4 Width-safe truncation

Use `truncateToWidth` / `visibleWidth` from `@earendil-works/pi-tui` if imported. If avoiding new imports beyond current dependencies, implement conservative ANSI-stripping truncation as current extension does, but ensure rendered visible width <= `width`.

## 13. Tests / validation

Minimum validation before review:

1. TypeScript loads under pi extension runtime or `tsx`/`tsc` equivalent if available.
2. Existing tool registration still exposes `arb_dispatch`.
3. Start a short dispatch and verify:
   - stdout/stderr logs are written;
   - parent fds do not remain open in the pi process after spawn;
   - task id is parsed;
   - completion notification still fires.
4. `/arb-watch` renders a below-editor widget and does not take over input.
5. Render lines fit a narrow width in a unit/pure-function test or manual harness.
6. Simulated status cases classify correctly:
   - queued with queue depth;
   - running recent update;
   - quiet-alive with old update + seat heartbeat alive;
   - stale with old update + no process + dead/unknown seat heartbeat;
   - completed ok with `ok="true"`;
   - completed/failed with `ok="false"` does not render as ok;
   - failed/error.
7. Multiline `last_summary` does not corrupt status parsing.
8. Invalid/missing Redis does not freeze pi and surfaces a bounded warning.
9. `/reload` does not leave duplicate timers.

## 14. Acceptance criteria

The Phase 1 patch is acceptable when:

- `/arb-watch` is compact, below-editor, themed, and input-safe;
- TUI render performs no I/O;
- poller performs no synchronous subprocess/network I/O;
- parent fd leak is fixed;
- Redis status parsing is safe for multiline values;
- quiet but live seats show `quiet-alive`, not stale/hung;
- stale requires multiple missing/old liveness signals and is warning-only;
- queue metadata is displayed only as active data for queued tasks;
- stdout vote parsing, if implemented, decodes stdout JSON `.result` before fence scanning;
- existing dispatch/adopt/status/collect commands still work;
- reload/shutdown cleans timers and widget state.

## 15. Deferred follow-up specs

After Phase 1 ships, write separate specs for:

1. Durable restore: session entries and `.arb/pi-dispatch-jobs.jsonl` sidecar.
2. Live stream integration: `ARB_LIVE_REDIS_URL` parsing, bounded `events:live` reducer, live vote stance/dropped warnings.
3. Full vote/audit polish: audit API/table lookup and/or bridge status vote fields.
4. Test extraction: pure render/classifier/parser tests if the single-file extension becomes unwieldy.
