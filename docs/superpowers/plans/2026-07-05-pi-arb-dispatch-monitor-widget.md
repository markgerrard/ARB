# pi ARB dispatch monitor native widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the pi ARB dispatch monitor so `/arb-watch` is a compact below-editor widget fed by an async cached snapshot, while preserving existing dispatch/adopt/status/collect entry points.

**Architecture:** Keep Phase 1 production code in `pi-extensions/arb-dispatch-monitor.ts`, but split responsibilities inside that file: typed cached job/snapshot model, pure classification/render/vote helpers, bounded async Redis/subprocess readers, and command/UI wiring. Add a zero-dependency Node smoke/unit test file that loads the TypeScript extension through pi's bundled `jiti` and tests exported pure helpers plus registration behavior. Renderers read only `WatchSnapshot`; all Redis/log/process work happens in poll/command handlers outside render.

**Tech Stack:** TypeScript extension for `@earendil-works/pi-coding-agent`, Node `child_process.spawn`, Redis via bounded async `redis-cli` subprocesses, no new npm dependencies. Tests use Node built-ins (`assert`, `fs`, `os`, `path`, `child_process`) plus pi's already-installed `jiti`.

## Global Constraints

- Source spec: `docs/superpowers/specs/2026-07-05-pi-arb-dispatch-monitor-widget-SPEC.md` is authoritative.
- Production implementation stays in `pi-extensions/arb-dispatch-monitor.ts`; test code may live beside it.
- Do not add npm dependencies or change bridge daemon/protocol code.
- No `spawnSync()` or synchronous subprocess/network I/O in widget render or poll timer.
- Render functions must perform no Redis, process, file, or network I/O.
- Polling must have both a single-in-flight tick guard and an intra-tick Redis subprocess fan-out cap.
- Redis task status reads must use a fixed field list and preserve multiline values. Preferred production strategy: async `redis-cli EVAL <lua> 1 <statusKey> ...fields` returning `cjson.encode(...)`. Do **not** pass inline Lua to `redis-cli --eval`; `--eval` expects a script file path.
- Parse Redis `ok` strings exactly: `"true"` -> `true`, `"false"` -> `false`, absent/other -> `undefined`; never use `Boolean(status.ok)`.
- `stale` is warning-only and requires multiple absent/old liveness signals. Quiet live seats must classify as `quiet-alive`.
- Existing public commands/tool remain registered: `arb_dispatch`, `/arb-dispatch`, `/arb-status`, `/arb-collect`, `/arb-watch`, `/arb-hide`, `/arb-console`, `/arb-adopt`, `/arb-adopt-run`; if the current checkout contains `/arb-auto-synthesize` / auto-synthesis helpers, preserve and test them as adjacent existing behavior.
- `/arb-console` must not retain the current sync render-time Redis/log refresh path. Either demote it to a warning/alias or render only cached state.
- Never render or notify env-file contents, Redis passwords, API tokens, or full task text.
- The current working checkout contains adjacent `autoSynthesize` / `/arb-auto-synthesize` behavior. Treat it as existing behavior to preserve while implementing this widget plan; do not leave implicit or undefined references to auto-synthesis helpers. If implementing from a clean base without those helpers, omit auto-synthesis calls entirely rather than referencing missing globals.

---

## File Structure

| File | Responsibility |
|---|---|
| `pi-extensions/arb-dispatch-monitor.ts` | All Phase 1 production implementation: data model, async Redis helpers, poller, cached render component, commands, fd cleanup, vote parsing. |
| `pi-extensions/arb-dispatch-monitor.test.mjs` | Zero-dependency Node tests/smokes for exported helper functions and extension registration. |
| `pi-extensions/README.md` | Document `/arb-watch` as compact cached widget and `/arb-console` as debug/demoted behavior. |

## Test command used throughout

Run focused extension tests after every task:

```bash
NODE_PATH=/home/<user>/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/node_modules \
  node pi-extensions/arb-dispatch-monitor.test.mjs
```

Expected on red steps: command exits non-zero with the named assertion failure. Expected on green steps: `arb-dispatch-monitor tests passed`.

Run build/load smoke after TypeScript changes:

```bash
bun build pi-extensions/arb-dispatch-monitor.ts --target=node --packages=external --outfile=/tmp/arb-dispatch-monitor.js
NODE_PATH=/home/<user>/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/node_modules node - <<'NODE'
const { createJiti } = require('jiti');
const mod = createJiti(process.cwd())('./pi-extensions/arb-dispatch-monitor.ts');
if (typeof mod.default !== 'function') throw new Error('default export missing');
console.log('loaded');
NODE
```

Expected: bun build succeeds and prints `loaded`.

Type-checking is not currently available in this repo environment (`tsc` is not installed and no `tsconfig.json` is present). To compensate, tasks that remove/migrate command helpers must add focused runtime command-handler smokes that execute the affected handlers, not just registration/build/load checks. If a future worker has `tsc` available, also run `tsc --noEmit` and record the command/result in the final report.

---

### Task 1: Add test harness and stable helper export seam

**Files:**
- Create: `pi-extensions/arb-dispatch-monitor.test.mjs`
- Modify: `pi-extensions/arb-dispatch-monitor.ts`

**Interfaces:**
- Consumes: existing extension default export.
- Produces: `export const __test` object that later tasks extend. At this task it exposes `short`, `stripAnsi`, `fit`, and `listJobStatesForTests()`.

- [ ] **Step 1: Write the failing test**

Create `pi-extensions/arb-dispatch-monitor.test.mjs`:

```js
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { createJiti } = require('jiti');
const jiti = createJiti(process.cwd());
const mod = jiti('./pi-extensions/arb-dispatch-monitor.ts');

function loadExtension() {
  const tools = [];
  const commands = new Map();
  const events = [];
  const sent = [];
  const pi = {
    registerTool(tool) { tools.push(tool); },
    registerCommand(name, options) { commands.set(name, options); },
    on(event, handler) { events.push({ event, handler }); },
    sendUserMessage(content, options) { sent.push({ content, options }); },
  };
  mod.default(pi);
  return { tools, commands, events, sent };
}

function testRegistration() {
  const { tools, commands } = loadExtension();
  assert.equal(tools[0]?.name, 'arb_dispatch');
  for (const name of ['arb-dispatch', 'arb-status', 'arb-collect', 'arb-watch', 'arb-hide', 'arb-console', 'arb-adopt', 'arb-adopt-run', 'arb-auto-synthesize']) {
    assert.ok(commands.has(name), `missing command ${name}`);
  }
}

function testHelperSeam() {
  assert.equal(typeof mod.__test.short, 'function');
  assert.equal(mod.__test.short('abcdefghijkl', 5), 'abcde…');
  assert.equal(mod.__test.fit('abcdef', 4), 'abc…');
}

async function main() {
  testRegistration();
  testHelperSeam();
  console.log('arb-dispatch-monitor tests passed');
}

main().catch((err) => {
  console.error(err.stack || err);
  process.exit(1);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run the focused command above.

Expected: FAIL with `Cannot read properties of undefined` or equivalent because `mod.__test` is not exported.

- [ ] **Step 3: Write minimal implementation**

At the bottom of `pi-extensions/arb-dispatch-monitor.ts`, after the default export function, add:

```ts
export const __test = {
  short,
  stripAnsi,
  fit,
  listJobStatesForTests: () => listJobs().map((job) => ({ id: job.id, runId: job.runId, targetId: job.targetId, state: job.state })),
};
```

If `stripAnsi`, `fit`, or `listJobs` are declared after this export in the current file, move the export to remain physically after those declarations.

- [ ] **Step 4: Run test/build to verify pass**

Run focused test and build/load smoke.

Expected: test prints `arb-dispatch-monitor tests passed`; build/load prints `loaded`.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "test: add ARB pi monitor extension harness"
```

---

### Task 2: Introduce Phase 1 typed snapshot model and status parsing helpers

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: raw Redis status object `{[field: string]: string}`.
- Produces: `parseRedisOk(value: string | undefined): boolean | undefined`, `normalizeStatus(raw): NormalizedStatus`, `STATUS_FIELDS`, and TypeScript types `ArbTaskState`, `ArbJob`, `WatchSnapshot`.

- [ ] **Step 1: Write the failing test**

Append to the test file:

```js
function testStatusNormalization() {
  const raw = {
    task_id: 'task-1',
    state: 'completed',
    phase: 'responding',
    last_summary: 'line 1\nline 2',
    updated_at: '2026-07-05T18:00:00Z',
    ok: 'false',
    error: 'boom',
    queue_depth: '7',
    enqueued_at: '2026-07-05T17:59:00Z',
  };
  const st = mod.__test.normalizeStatus(raw);
  assert.equal(st.taskId, 'task-1');
  assert.equal(st.summary, 'line 1\nline 2');
  assert.equal(st.ok, false);
  assert.equal(st.queueDepth, 7);
  assert.equal(mod.__test.parseRedisOk('true'), true);
  assert.equal(mod.__test.parseRedisOk('false'), false);
  assert.equal(mod.__test.parseRedisOk(undefined), undefined);
  assert.equal(mod.__test.parseRedisOk(''), undefined);
  assert.deepEqual(mod.__test.STATUS_FIELDS, ['task_id', 'state', 'phase', 'last_summary', 'updated_at', 'ok', 'error', 'queue_depth', 'enqueued_at']);
}
```

Call `testStatusNormalization()` from `main()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because `normalizeStatus`/`parseRedisOk`/`STATUS_FIELDS` are missing.

- [ ] **Step 3: Write minimal implementation**

Near the existing `JobState` type, replace/extend types with:

```ts
type ArbTaskState =
  | 'launching'
  | 'queued'
  | 'running'
  | 'quiet-alive'
  | 'stale'
  | 'completed'
  | 'failed'
  | 'timeout'
  | 'killed'
  | 'unknown';

type ArbVote = {
  stance?: string;
  severity?: string;
  refs?: string[];
  note?: string;
  source: 'live' | 'stdout' | 'audit';
  seenAt?: string;
};

type NormalizedStatus = {
  taskId?: string;
  rawState?: string;
  phase?: string;
  summary?: string;
  updatedAt?: string;
  ok?: boolean;
  error?: string;
  queueDepth?: number;
  enqueuedAt?: string;
};

const STATUS_FIELDS = ['task_id', 'state', 'phase', 'last_summary', 'updated_at', 'ok', 'error', 'queue_depth', 'enqueued_at'] as const;

const parseRedisOk = (value: string | undefined): boolean | undefined => {
  if (value === 'true') return true;
  if (value === 'false') return false;
  return undefined;
};

const parseOptionalNumber = (value: string | undefined): number | undefined => {
  if (value === undefined || value === '') return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
};

const normalizeStatus = (raw: Record<string, string>): NormalizedStatus => ({
  taskId: raw.task_id || undefined,
  rawState: raw.state || undefined,
  phase: raw.phase || undefined,
  summary: raw.last_summary || undefined,
  updatedAt: raw.updated_at || undefined,
  ok: parseRedisOk(raw.ok),
  error: raw.error || undefined,
  queueDepth: parseOptionalNumber(raw.queue_depth),
  enqueuedAt: raw.enqueued_at || undefined,
});

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
  voteChecked?: boolean;
  taskIdChecked?: boolean;
  taskIdPollAttempts?: number;
  taskIdLastReadAt?: number;
  taskIdReadDeadlineMs?: number;
  recentEvents: string[];
  exitCode?: number | null;
  signal?: NodeJS.Signals | null;
  lastStatus?: Record<string, string>;
  process?: ChildProcessWithoutNullStreams;
  autoSynthesize?: boolean;
  expectedTargets?: string[];
  synthesisPrompt?: string;
  synthesisOutputPath?: string;
};

type WatchSnapshot = {
  jobs: ArbJob[];
  generatedAt: number;
  pollError?: string;
  filterRunId?: string;
};
```

Then make the existing jobs map use `ArbJob`:

```ts
const jobs = new Map<string, ArbJob>();
```

Update all `Job` annotations to `ArbJob`. Keep a compatibility alias only if needed:

```ts
type Job = ArbJob;
```

Add these names to `__test`:

```ts
STATUS_FIELDS,
parseRedisOk,
normalizeStatus,
```

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused test passes; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: add ARB monitor snapshot status model"
```

---

### Task 3: Implement conservative state classification

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: `ArbJob`, `NormalizedStatus`, `nowMs`, constants `STALE_GRACE_MS`, `PROCESS_STALE_GRACE_MS`.
- Produces: `classifyJob(job, status, nowMs): ArbTaskState` and `applyStatusToJob(job, status, nowMs): void`.

- [ ] **Step 1: Write the failing test**

Append:

```js
function baseJob(overrides = {}) {
  return {
    id: 'j1', runId: 'r1', targetId: 'seat-a', engine: 'codex', task: 'x',
    state: 'running', startedAt: Date.now() - 1000, stdoutPath: '/tmp/out', stderrPath: '/tmp/err',
    envFile: '/tmp/env', redisPrefix: 'agent_scratch:', recentEvents: [], processAlive: true,
    ...overrides,
  };
}

function testClassification() {
  const now = Date.parse('2026-07-05T18:10:00Z');
  const recent = '2026-07-05T18:09:30Z';
  const old = '2026-07-05T18:00:00Z';
  assert.equal(mod.__test.classifyJob(baseJob(), { rawState: 'queued', queueDepth: 3 }, now), 'queued');
  assert.equal(mod.__test.classifyJob(baseJob(), { rawState: 'running', updatedAt: recent }, now), 'running');
  assert.equal(mod.__test.classifyJob(baseJob({ seatHeartbeatTtl: 20 }), { rawState: 'running', updatedAt: old }, now), 'quiet-alive');
  assert.equal(mod.__test.classifyJob(baseJob({ processAlive: false, seatHeartbeatTtl: -2 }), { rawState: 'running', updatedAt: old }, now), 'stale');
  assert.equal(mod.__test.classifyJob(baseJob(), { rawState: 'completed', ok: true }, now), 'completed');
  assert.equal(mod.__test.classifyJob(baseJob({ state: 'timeout' }), {}, now), 'timeout');
  assert.equal(mod.__test.classifyJob(baseJob({ rawState: 'running' }), {}, now), 'running');

  const job = baseJob();
  mod.__test.applyStatusToJob(job, { rawState: 'completed', ok: false, summary: 'done', updatedAt: recent }, now);
  assert.equal(job.state, 'completed');
  assert.equal(job.ok, false);
  assert.equal(job.summary, 'done');
}
```

Call `testClassification()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because classifier helpers are missing.

- [ ] **Step 3: Write minimal implementation**

Add near status helpers:

```ts
const STALE_GRACE_MS = 180_000;
const PROCESS_STALE_GRACE_MS = 600_000;

const parseTimeMs = (value: string | undefined): number | undefined => {
  if (!value) return undefined;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : undefined;
};

const isTerminalState = (state: ArbTaskState) => state === 'completed' || state === 'failed' || state === 'timeout' || state === 'killed';

const classifyJob = (job: ArbJob, status: NormalizedStatus, nowMs = Date.now()): ArbTaskState => {
  const rawState = status.rawState ?? job.rawState;
  if ((job.state === 'timeout' || job.state === 'killed') && rawState !== 'completed' && rawState !== 'failed') return job.state;
  if (rawState === 'completed') return 'completed';
  if (rawState === 'failed') return 'failed';
  if (rawState === 'queued') return 'queued';
  if (rawState === 'running') {
    const updatedMs = parseTimeMs(status.updatedAt) ?? parseTimeMs(job.updatedAt);
    const ageMs = updatedMs === undefined ? Number.POSITIVE_INFINITY : nowMs - updatedMs;
    if (ageMs <= STALE_GRACE_MS) return 'running';
    const processRecentlyPlausible = job.processAlive === true && ageMs <= PROCESS_STALE_GRACE_MS;
    const heartbeatAlive = typeof job.seatHeartbeatTtl === 'number' && job.seatHeartbeatTtl > 0;
    if (processRecentlyPlausible || heartbeatAlive) return 'quiet-alive';
    return 'stale';
  }
  if (isTerminalState(job.state)) return job.state;
  if (job.taskId) return 'unknown';
  return 'launching';
};

const applyStatusToJob = (job: ArbJob, status: NormalizedStatus, nowMs = Date.now()) => {
  job.rawState = status.rawState ?? job.rawState;
  job.phase = status.phase ?? job.phase;
  job.ok = status.ok ?? job.ok;
  job.summary = status.summary ?? job.summary;
  job.error = status.error ?? job.error;
  job.updatedAt = status.updatedAt ?? job.updatedAt;
  job.queueDepth = status.queueDepth ?? job.queueDepth;
  job.enqueuedAt = status.enqueuedAt ?? job.enqueuedAt;
  if (status.taskId) job.taskId = status.taskId;
  job.state = classifyJob(job, status, nowMs);
  if (job.state === 'completed' || job.state === 'failed') job.finishedAt = job.finishedAt ?? nowMs;
};
```

If an older `isTerminalState` helper exists, replace it with this one and update call sites.

Add to `__test`:

```ts
STALE_GRACE_MS,
PROCESS_STALE_GRACE_MS,
classifyJob,
applyStatusToJob,
```

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: classify ARB monitor liveness conservatively"
```

---

### Task 4: Add pure compact snapshot renderer

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: `WatchSnapshot`, width, expanded flag.
- Produces: `renderSnapshotLines(snapshot, width, expanded): string[]` that performs no I/O and bounds visible width.

- [ ] **Step 1: Write the failing test**

Append:

```js
function testSnapshotRender() {
  const snapshot = {
    generatedAt: Date.now(),
    jobs: [
      baseJob({ targetId: 'codex-example-app-dev', runId: 'panel-x', state: 'running', taskId: '12345678-aaaa', phase: 'responding', summary: 'Streaming response' }),
      baseJob({ targetId: 'asdk-example-app-dev-opus48', runId: 'panel-x', state: 'quiet-alive', taskId: 'abcdef12-bbbb', seatHeartbeatTtl: 22, summary: 'No recent status' }),
      baseJob({ targetId: 'agy-example-app-dev', runId: 'panel-x', state: 'completed', ok: false, taskId: '99999999-cccc' }),
    ],
  };
  const lines = mod.__test.renderSnapshotLines(snapshot, 72, false);
  assert.ok(lines[0].includes('ARB'));
  assert.ok(lines.some((line) => line.includes('quiet-alive')));
  assert.ok(lines.every((line) => mod.__test.stripAnsi(line).length <= 72), lines.join('\n'));
  const tiny = mod.__test.renderSnapshotLines(snapshot, 24, false);
  assert.equal(tiny.length, 1);
  assert.ok(mod.__test.stripAnsi(tiny[0]).length <= 24);
  const expanded = mod.__test.renderSnapshotLines(snapshot, 100, true);
  assert.ok(expanded.some((line) => line.includes('seat ttl')));
}
```

Call `testSnapshotRender()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because `renderSnapshotLines` is missing.

- [ ] **Step 3: Write minimal implementation**

Replace current `renderWatchLines()` with a wrapper around this pure helper:

```ts
type WidgetTheme = { fg?: (color: string, text: string) => string };

const color = (theme: WidgetTheme | undefined, name: string, text: string) => theme?.fg ? theme.fg(name, text) : text;

const stateGlyph = (job: ArbJob) => {
  if (job.state === 'completed') return job.ok === false ? '⚠' : '✓';
  if (job.state === 'failed' || job.state === 'timeout' || job.state === 'killed') return '✗';
  if (job.state === 'queued') return '◦';
  if (job.state === 'quiet-alive' || job.state === 'stale') return '⚠';
  return '●';
};

const stateColor = (job: ArbJob) => {
  if (job.state === 'completed') return job.ok === false ? 'warning' : 'success';
  if (job.state === 'failed' || job.state === 'timeout' || job.state === 'killed') return 'error';
  if (job.state === 'queued' || job.state === 'quiet-alive' || job.state === 'stale') return 'warning';
  return 'accent';
};

const visibleAge = (job: ArbJob, nowMs: number) => {
  const base = parseTimeMs(job.updatedAt) ?? job.startedAt;
  const seconds = Math.max(0, Math.round((nowMs - base) / 1000));
  if (seconds < 90) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
};

const prioritizeJobs = (jobs: ArbJob[]) => [...jobs].sort((a, b) => {
  const rank = (j: ArbJob) => (j.state === 'running' || j.state === 'quiet-alive' || j.state === 'stale' || j.state === 'queued') ? 0 : 1;
  return rank(a) - rank(b) || a.startedAt - b.startedAt;
});

const renderSnapshotLines = (snapshot: WatchSnapshot, width: number, expanded = false, theme?: WidgetTheme): string[] => {
  const safeWidth = Math.max(1, width || 80);
  const jobs = snapshot.filterRunId ? snapshot.jobs.filter((j) => j.runId === snapshot.filterRunId) : snapshot.jobs;
  if (safeWidth < 32) {
    const active = jobs.filter((j) => !isTerminalState(j.state)).length;
    return [fit(`ARB ${active} active / ${jobs.length - active} done`, safeWidth)];
  }
  if (jobs.length === 0) return [fit('ARB watch: no jobs started in this pi session', safeWidth)];
  const active = jobs.filter((j) => !isTerminalState(j.state)).length;
  const lines: string[] = [];
  const runLabel = snapshot.filterRunId || (new Set(jobs.map((j) => j.runId)).size === 1 ? jobs[0]?.runId : 'all runs');
  lines.push(`ARB ${runLabel} · ${active} active · ${jobs.length - active} done`);
  const shown = prioritizeJobs(jobs).slice(0, expanded ? 10 : 5);
  for (const job of shown) {
    const phase = job.phase ? `/${job.phase}` : '';
    const ok = job.state === 'completed' ? (job.ok === false ? ' not-ok' : job.ok === true ? ' ok' : '') : '';
    const task = job.taskId ? ` · task ${short(job.taskId, 8)}` : ' · task pending';
    lines.push(`${color(theme, stateColor(job), stateGlyph(job))} ${job.targetId} · ${color(theme, stateColor(job), job.state)}${phase}${ok}${task} · ${color(theme, 'dim', visibleAge(job, snapshot.generatedAt))}`);
    if (job.state === 'queued' && job.queueDepth !== undefined) lines.push(`  queue depth ${job.queueDepth}${job.enqueuedAt ? ` · enqueued ${job.enqueuedAt}` : ''}`);
    else if (job.summary && !isTerminalState(job.state)) lines.push(`  ${job.summary}`);
    if (expanded) {
      lines.push(color(theme, 'dim', `  run ${job.runId} · raw ${job.rawState || '-'} · seat ttl ${job.seatHeartbeatTtl ?? 'unknown'} · out ${job.stdoutPath}`));
      if (job.vote) lines.push(`  vote ${job.vote.stance || '?'} ${job.vote.severity || ''} (${job.vote.source})`);
    }
  }
  if (shown.length < jobs.length) lines.push(`+${jobs.length - shown.length} more`);
  if (snapshot.pollError) lines.push(`poll warning: ${snapshot.pollError}`);
  return lines.slice(0, expanded ? 24 : 10).map((line) => fit(line, safeWidth));
};

let watchSnapshot: WatchSnapshot = { jobs: [], generatedAt: Date.now() };
let watchFilterRunId: string | undefined;

const makeSnapshot = (): WatchSnapshot => ({
  jobs: listJobs().map((job) => ({ ...job, process: undefined })),
  generatedAt: Date.now(),
  filterRunId: watchFilterRunId,
});

// Temporary compatibility wrapper for existing refreshWatch()/command callers.
// Task 7 replaces those callers with cached widget updates; remove this wrapper only after no references remain.
const renderWatchLines = (expanded = false, width = 100): string[] => renderSnapshotLines(makeSnapshot(), width, expanded);

const makeWatchComponent = (getExpanded: () => boolean, theme?: WidgetTheme) => ({
  render(width: number) {
    return renderSnapshotLines(watchSnapshot, width, getExpanded(), theme);
  },
  invalidate() {},
});
```

Update `__test`:

```ts
renderSnapshotLines,
```

Do not add any file/Redis/process reads to `renderSnapshotLines`. Keep the temporary `renderWatchLines()` wrapper until Task 7 replaces the remaining callers; do not delete it in Task 4.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: render compact ARB watch snapshots"
```

---

### Task 5: Add stdout vote parsing from dispatcher JSON `.result`

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: stdout text string or stdout path after terminal jobs.
- Produces: `parseVoteFromDispatcherStdoutText(text): ArbVote | undefined` and `tryRefreshVoteFromStdout(job): void`.

- [ ] **Step 1: Write the failing test**

Append:

```js
function testVoteParsing() {
  const text = JSON.stringify({ result: 'Verdict\n```vote\n{"stance":"accept","severity":"P2","refs":["a"],"note":"ok"}\n```' });
  const vote = mod.__test.parseVoteFromDispatcherStdoutText(text);
  assert.equal(vote.stance, 'accept');
  assert.equal(vote.severity, 'P2');
  assert.deepEqual(vote.refs, ['a']);
  assert.equal(vote.source, 'stdout');
  assert.equal(mod.__test.parseVoteFromDispatcherStdoutText('not json'), undefined);
  assert.equal(mod.__test.parseVoteFromDispatcherStdoutText(JSON.stringify({ result: 'no fence' })), undefined);
  assert.equal(mod.__test.parseVoteFromDispatcherStdoutText(JSON.stringify({ result: '```vote\nnot-json\n```' })), undefined);
}
```

Call `testVoteParsing()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because parser is missing.

- [ ] **Step 3: Write minimal implementation**

First update the existing `node:fs` import in this task to include `promises as fsPromises`. For the current import shape, the target line becomes:

```ts
import { existsSync, mkdirSync, openSync, promises as fsPromises, readFileSync, readdirSync } from 'node:fs';
```

Then add:

```ts
const parseVoteFromDispatcherStdoutText = (text: string): ArbVote | undefined => {
  try {
    const outer = JSON.parse(text) as { result?: unknown };
    if (typeof outer.result !== 'string') return undefined;
    const match = outer.result.match(/```vote\s*\n([\s\S]*?)\n```/);
    if (!match) return undefined;
    const parsed = JSON.parse(match[1]) as { stance?: unknown; severity?: unknown; refs?: unknown; note?: unknown };
    return {
      stance: typeof parsed.stance === 'string' ? parsed.stance : undefined,
      severity: typeof parsed.severity === 'string' ? parsed.severity : undefined,
      refs: Array.isArray(parsed.refs) ? parsed.refs.filter((r): r is string => typeof r === 'string') : undefined,
      note: typeof parsed.note === 'string' ? parsed.note : undefined,
      source: 'stdout',
      seenAt: new Date().toISOString(),
    };
  } catch {
    return undefined;
  }
};

const MAX_VOTE_STDOUT_BYTES = 256_000;

const readTextFileCapped = async (path: string, maxBytes: number): Promise<string | undefined> => {
  try {
    const handle = await fsPromises.open(path, 'r');
    try {
      const buffer = Buffer.alloc(maxBytes);
      const { bytesRead } = await handle.read(buffer, 0, maxBytes, 0);
      return buffer.subarray(0, bytesRead).toString('utf8');
    } finally {
      await handle.close();
    }
  } catch {
    return undefined;
  }
};

const tryRefreshVoteFromStdout = async (job: ArbJob) => {
  if (job.vote || job.voteChecked || !isTerminalState(job.state)) return;
  job.voteChecked = true;
  const text = await readTextFileCapped(job.stdoutPath, MAX_VOTE_STDOUT_BYTES);
  if (!text) return;
  const vote = parseVoteFromDispatcherStdoutText(text);
  if (vote) job.vote = vote;
};
```

Call `await tryRefreshVoteFromStdout(job)` for terminal jobs after child exit and during poll when a job is terminal. Because `voteChecked` is set on first attempt, the poller never repeatedly reads whole stdout for vote-less terminal jobs. Add parser and capped-read helper to `__test`.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: parse ARB panel votes from dispatcher stdout"
```

---

### Task 6: Add bounded async subprocess and Redis status helpers

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: command, args, timeout/cap options.
- Produces: `execFileText`, `redisStatusJsonArgs`, `readTaskStatus`, `readSeatHeartbeatTtl`, `createLimit`.

- [ ] **Step 1: Write the failing test**

Append:

```js
async function testAsyncSubprocessAndLimiter() {
  const ok = await mod.__test.execFileText(process.execPath, ['-e', 'process.stdout.write("hello")'], { timeoutMs: 1000, maxBytes: 1000 });
  assert.equal(ok.ok, true);
  assert.equal(ok.stdout, 'hello');

  const timed = await mod.__test.execFileText(process.execPath, ['-e', 'setTimeout(()=>{}, 5000)'], { timeoutMs: 50, maxBytes: 1000 });
  assert.equal(timed.ok, false);
  assert.equal(timed.timedOut, true);

  let active = 0;
  let maxActive = 0;
  const limit = mod.__test.createLimit(2);
  await Promise.all(Array.from({ length: 6 }, (_, i) => limit(async () => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((resolve) => setTimeout(resolve, 20));
    active -= 1;
    return i;
  })));
  assert.equal(maxActive, 2);

  const args = mod.__test.redisStatusJsonArgs('/tmp/env', 'agent_scratch:task:t1:status');
  assert.ok(args.includes('EVAL'));
  assert.equal(args[args.indexOf('EVAL') + 2], '1');
  assert.ok(args.some((a) => a.includes('cjson.encode')));
  assert.equal(args.includes('--eval'), false);
}
```

Call `await testAsyncSubprocessAndLimiter()` from `main()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because helpers are missing.

- [ ] **Step 3: Write minimal implementation**

Add async helpers without removing the existing sync helpers yet. Keep the current `spawnSync` import until Task 9 removes the last `redisLines()`/`hgetall()`/`refreshJobRedis()` consumers; this preserves per-task executability for `/arb-status` and `/arb-adopt` while the migration is in progress.

Add:

```ts
type ExecTextResult = { ok: true; stdout: string; stderr: string } | { ok: false; stdout: string; stderr: string; timedOut?: boolean; error?: string };

const execFileText = (cmd: string, args: string[], opts: { env?: NodeJS.ProcessEnv; timeoutMs: number; maxBytes: number; cwd?: string }): Promise<ExecTextResult> => new Promise((resolve) => {
  let stdout = '';
  let stderr = '';
  let settled = false;
  const child = spawn(cmd, args, { env: opts.env, cwd: opts.cwd, stdio: ['ignore', 'pipe', 'pipe'] });
  const finish = (result: ExecTextResult) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    resolve(result);
  };
  const append = (kind: 'stdout' | 'stderr', chunk: Buffer) => {
    const next = (kind === 'stdout' ? stdout : stderr) + chunk.toString('utf8');
    const capped = next.slice(0, opts.maxBytes);
    if (kind === 'stdout') stdout = capped;
    else stderr = capped;
  };
  child.stdout.on('data', (chunk) => append('stdout', chunk));
  child.stderr.on('data', (chunk) => append('stderr', chunk));
  child.on('error', (err) => finish({ ok: false, stdout, stderr, error: err.message }));
  child.on('close', (code) => finish(code === 0 ? { ok: true, stdout, stderr } : { ok: false, stdout, stderr, error: `exit ${code}` }));
  const timer = setTimeout(() => {
    child.kill('SIGTERM');
    setTimeout(() => child.kill('SIGKILL'), 250).unref?.();
    finish({ ok: false, stdout, stderr, timedOut: true, error: 'timeout' });
  }, opts.timeoutMs);
  timer.unref?.();
});

const createLimit = (max: number) => {
  let active = 0;
  const queue: Array<() => void> = [];
  const runNext = () => {
    if (active >= max) return;
    const next = queue.shift();
    if (next) next();
  };
  return async <T>(fn: () => Promise<T>): Promise<T> => new Promise<T>((resolve, reject) => {
    const start = () => {
      active += 1;
      fn().then(resolve, reject).finally(() => {
        active -= 1;
        runNext();
      });
    };
    queue.push(start);
    runNext();
  });
};

const STATUS_LUA = "local val = redis.call('HMGET', KEYS[1], unpack(ARGV)); local out = {}; for i, k in ipairs(ARGV) do out[k] = val[i] end; return cjson.encode(out)";

const redisBaseArgs = (envFile: string): string[] => {
  const env = readEnvFile(envFile);
  const out = ['-h', env.AGENT_REDIS_HOST || process.env.AGENT_REDIS_HOST || '127.0.0.1'];
  out.push('-p', env.AGENT_REDIS_PORT || process.env.AGENT_REDIS_PORT || '6390');
  out.push('-n', env.AGENT_REDIS_DB || process.env.AGENT_REDIS_DB || '12');
  out.push('--no-auth-warning');
  if ((env.AGENT_REDIS_TLS || process.env.AGENT_REDIS_TLS || '0').match(/^(1|true|yes)$/i)) out.push('--tls');
  const user = env.AGENT_REDIS_USER || process.env.AGENT_REDIS_USER;
  if (user) out.push('--user', user);
  return out;
};

const redisStatusJsonArgs = (envFile: string, key: string): string[] => [...redisBaseArgs(envFile), 'EVAL', STATUS_LUA, '1', key, ...STATUS_FIELDS];

const readTaskStatus = async (envFile: string, key: string): Promise<Record<string, string>> => {
  const res = await execFileText('redis-cli', redisStatusJsonArgs(envFile, key), { env: redisEnv(envFile), timeoutMs: 2500, maxBytes: 32_000 });
  if (!res.ok) return {};
  try {
    const parsed = JSON.parse(res.stdout) as Record<string, string | null>;
    return Object.fromEntries(Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
  } catch {
    return {};
  }
};

const readSeatHeartbeatTtl = async (envFile: string, key: string): Promise<number | undefined> => {
  const res = await execFileText('redis-cli', [...redisBaseArgs(envFile), 'TTL', key], { env: redisEnv(envFile), timeoutMs: 1500, maxBytes: 1000 });
  if (!res.ok) return undefined;
  const n = Number(res.stdout.trim());
  return Number.isFinite(n) ? n : undefined;
};
```

Leave existing `redisLines()`/`hgetall()` consumers untouched in this task; this task only adds the async helpers and tests their argument/timeout/limiter behavior. Consumers migrate in Task 7, and the old sync helpers are removed in Task 9. Add helpers to `__test`.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes. Do **not** run the sync-helper removal grep yet; old consumers are intentionally still present until Tasks 7-9.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: add async bounded Redis status helpers"
```

---

### Task 7: Implement async poller with cached snapshot and concurrency guard

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: `jobs`, `readTaskStatus`, `readSeatHeartbeatTtl`, `parseTaskId`.
- Produces: `pollOnce(ctx?, deps?)`, `ensurePoller(ctx)`, `stopPoller(ctx?)`, cached `watchSnapshot`.

- [ ] **Step 1: Write the failing test**

Append:

```js
async function testPollOnceGuardAndSnapshot() {
  const jobs = [baseJob({ id: 'a', targetId: 'seat-a', taskId: 'task-a' }), baseJob({ id: 'b', targetId: 'seat-b', taskId: 'task-b' })];
  let statusCalls = 0;
  let ttlCalls = 0;
  const snapshot = await mod.__test.pollJobsForTests(jobs, {
    readStatus: async (job) => {
      statusCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { rawState: 'running', updatedAt: new Date().toISOString(), phase: job.targetId };
    },
    readTtl: async () => {
      ttlCalls += 1;
      return 30;
    },
    nowMs: Date.now(),
    concurrency: 1,
  });
  assert.equal(statusCalls, 2);
  assert.equal(ttlCalls, 2);
  assert.equal(snapshot.jobs.length, 2);
  assert.ok(snapshot.jobs.every((job) => job.state === 'running'));

  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-taskid-'));
  const stderrPath = path.join(tmp, 'seat.err');
  fs.writeFileSync(stderrPath, 'dispatch starting\n');
  const pending = baseJob({ id: 'pending', taskId: undefined, stderrPath, state: 'running', startedAt: Date.now() });
  await mod.__test.pollJobsForTests([pending], { readStatus: async () => ({}), readTtl: async () => undefined, nowMs: Date.now() });
  assert.equal(pending.taskId, undefined);
  assert.equal(pending.taskIdChecked, undefined, 'first missing task-id parse must not permanently disable startup retries');
  fs.appendFileSync(stderrPath, 'task-id: 00000000-0000-4000-8000-000000000001\n');
  await mod.__test.pollJobsForTests([pending], { readStatus: async () => ({ rawState: 'running' }), readTtl: async () => 30, nowMs: Date.now() + 1000 });
  assert.equal(pending.taskId, '00000000-0000-4000-8000-000000000001');
}
```

Call `await testPollOnceGuardAndSnapshot()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because `pollJobsForTests` is missing.

- [ ] **Step 3: Write minimal implementation**

Add:

```ts
type PollDeps = {
  readStatus?: (job: ArbJob) => Promise<NormalizedStatus>;
  readTtl?: (job: ArbJob) => Promise<number | undefined>;
  nowMs?: number;
  concurrency?: number;
};

const TASK_ID_RETRY_INTERVAL_MS = 750;
const TASK_ID_RETRY_WINDOW_MS = 60_000;

const shouldTryTaskIdRead = (job: ArbJob, nowMs: number): boolean => {
  if (job.taskId || job.taskIdChecked || isTerminalState(job.state)) return false;
  if (!job.taskIdReadDeadlineMs) job.taskIdReadDeadlineMs = job.startedAt + TASK_ID_RETRY_WINDOW_MS;
  if (nowMs > job.taskIdReadDeadlineMs) {
    job.taskIdChecked = true;
    return false;
  }
  if (job.taskIdLastReadAt && nowMs - job.taskIdLastReadAt < TASK_ID_RETRY_INTERVAL_MS) return false;
  return true;
};

const refreshTaskIdFromStderr = (job: ArbJob, nowMs: number) => {
  if (!shouldTryTaskIdRead(job, nowMs)) return;
  job.taskIdLastReadAt = nowMs;
  job.taskIdPollAttempts = (job.taskIdPollAttempts ?? 0) + 1;
  job.taskId = parseTaskId(job.stderrPath);
  if (job.taskId) job.taskIdChecked = true;
};

let pollInFlight = false;
// Reuse the module-level watchTimer already declared near watchEnabled; do not redeclare it here.

const pollJobs = async (inputJobs: ArbJob[], deps: PollDeps = {}): Promise<WatchSnapshot> => {
  const limit = createLimit(deps.concurrency ?? 4);
  const nowMs = deps.nowMs ?? Date.now();
  await Promise.all(inputJobs.map((job) => limit(async () => {
    refreshTaskIdFromStderr(job, nowMs);
    job.processAlive = job.process ? (job.process.exitCode === null && job.process.signalCode === null) : job.processAlive ?? false;
    let status: NormalizedStatus = {};
    if (job.taskId) {
      status = deps.readStatus
        ? await deps.readStatus(job)
        : normalizeStatus(await readTaskStatus(job.envFile, `${job.redisPrefix}task:${job.taskId}:status`));
      job.seatHeartbeatTtl = deps.readTtl
        ? await deps.readTtl(job)
        : await readSeatHeartbeatTtl(job.envFile, `${job.redisPrefix}agent:${job.targetId}:status`);
    }
    applyStatusToJob(job, status, nowMs);
    if (isTerminalState(job.state)) await tryRefreshVoteFromStdout(job);
  })));
  return { jobs: inputJobs.map((job) => ({ ...job, process: undefined })), generatedAt: nowMs, filterRunId: watchFilterRunId };
};

const pollJobsForTests = pollJobs;

const pollOnce = async (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    watchSnapshot = await pollJobs(listJobs());
    // Preserve existing auto-synthesis behavior present in this checkout. If implementing from a clean base without auto-synthesis helpers, omit this call rather than referencing an undefined symbol.
    checkAutoSynthesisRuns(ctx ?? lastCtx);
  } catch (err) {
    watchSnapshot = { ...watchSnapshot, generatedAt: Date.now(), pollError: err instanceof Error ? err.message : String(err) };
  } finally {
    pollInFlight = false;
  }
};

const shouldPoll = () => watchEnabled || listJobs().some((job) => !isTerminalState(job.state));

const ensurePoller = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (watchTimer) return;
  watchTimer = setInterval(() => {
    if (!shouldPoll()) return stopPoller(lastCtx);
    void pollOnce(lastCtx).then(() => updateWatchWidget(lastCtx));
  }, 3000);
  watchTimer.unref?.();
};

const stopPoller = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = undefined;
  if (!watchEnabled) (ctx?.ui ?? lastCtx?.ui)?.setWidget('arb-watch', undefined);
};
```

Replace `refreshWatch()` with:

```ts
const updateWatchWidget = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  const ui = ctx?.ui ?? lastCtx?.ui;
  if (!ui || !watchEnabled) return;
  const expanded = Boolean((ui as { getToolsExpanded?: () => boolean }).getToolsExpanded?.());
  ui.setWidget('arb-watch', (_tui, theme) => makeWatchComponent(() => Boolean((ui as { getToolsExpanded?: () => boolean }).getToolsExpanded?.()), theme), { placement: 'belowEditor' });
  updateStatus(ctx ?? lastCtx);
};

const refreshWatch = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  void pollOnce(ctx ?? lastCtx).then(() => updateWatchWidget(ctx ?? lastCtx));
};
```

Update `startWatch()` to call `ensurePoller(ctx); refreshWatch(ctx);`. Update launch/adopt paths to call `ensurePoller(ctx)` when active jobs exist.

Add `pollJobsForTests` to `__test`.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes. `rg "renderSnapshotLines\(watchSnapshot, 100|setWidget\('arb-watch', renderSnapshotLines" pi-extensions/arb-dispatch-monitor.ts` shows no matches. The old sync Redis helper grep is deferred until Task 9 after all consumers migrate.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: poll ARB task snapshots asynchronously"
```

---

### Task 8: Replace `/arb-watch` widget with below-editor cached rendering and add reload cleanup

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: `watchSnapshot`, `renderSnapshotLines`, pi UI context.
- Produces: below-editor widget registration, `/arb-watch [run-id|all]`, `/arb-hide`, global cleanup registry.

- [ ] **Step 1: Write the failing test**

Append:

```js
function testCleanupAndWatchCommandShape() {
  const { commands } = loadExtension();
  const watch = commands.get('arb-watch');
  assert.ok(watch.description.includes('below-editor') || watch.description.includes('compact'));
  assert.equal(typeof mod.__test.cleanupForTests, 'function');
}
```

Call `testCleanupAndWatchCommandShape()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL because description/cleanup helper are not updated.

- [ ] **Step 3: Write minimal implementation**

At module top after globals:

```ts
const CLEANUP_KEY = '__arbDispatchMonitorCleanup';
const previousCleanup = (globalThis as Record<string, unknown>)[CLEANUP_KEY];
if (typeof previousCleanup === 'function') {
  try { previousCleanup(); } catch { /* stale extension cleanup is best-effort */ }
}

const cleanupRuntime = () => {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = undefined;
  watchEnabled = false;
  lastCtx = undefined;
};
(globalThis as Record<string, unknown>)[CLEANUP_KEY] = cleanupRuntime;
```

Update `/arb-watch` handler:

```ts
pi.registerCommand('arb-watch', {
  description: 'Toggle compact below-editor ARB watch widget for cached Redis/log status',
  handler: async (args, ctx) => {
    lastCtx = ctx;
    const filter = (args || '').trim();
    if (filter && filter !== 'all') watchFilterRunId = filter;
    if (filter === 'all') watchFilterRunId = undefined;
    if (watchEnabled && !filter) {
      watchEnabled = false;
      updateWatchWidget(ctx);
      stopPoller(ctx);
      ctx.ui.notify('ARB watch hidden', 'info');
      return;
    }
    watchEnabled = true;
    ensurePoller(ctx);
    await pollOnce(ctx);
    updateWatchWidget(ctx);
    ctx.ui.notify('ARB watch visible (cached Redis/log status; no model context streaming)', 'info');
  },
});
```

Update `/arb-hide` to set `watchEnabled = false; updateWatchWidget(ctx); stopPoller(ctx);`.

Update `session_shutdown` to call `cleanupRuntime()` and clear status/widget.

Add to `__test`:

```ts
cleanupForTests: cleanupRuntime,
```

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "feat: render ARB watch as compact cached widget"
```

---

### Task 9: Demote or cache `/arb-console`; preserve one-shot commands safely

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: cached `watchSnapshot` only.
- Produces: `/arb-console` with no render-time Redis/file/process I/O; `/arb-status` refresh uses async poll helper instead of sync `refreshJobRedis`.

- [ ] **Step 1: Write the failing test**

Append:

```js
function testNoSyncRedisNamesRemain() {
  const fs = require('node:fs');
  const src = fs.readFileSync('pi-extensions/arb-dispatch-monitor.ts', 'utf8');
  assert.equal(src.includes('spawnSync'), false, 'spawnSync must not remain');
  assert.equal(src.includes('HGETALL'), false, 'HGETALL parser must not remain');
  assert.equal(src.includes('refreshAll();'), false, 'console render must not refresh Redis inside render');
}

async function testMigratedCommandHandlersExecute() {
  const { commands } = loadExtension();
  const notices = [];
  const widgets = [];
  const ctx = { cwd: process.cwd(), mode: 'tui', ui: { notify: (...a) => notices.push(a), setStatus() {}, setWidget: (...a) => widgets.push(a) } };
  await commands.get('arb-status').handler('', ctx);
  await commands.get('arb-console').handler('', ctx);
  await commands.get('arb-debug-console').handler('', ctx);
  await commands.get('arb-adopt').handler('not-a-task-id', ctx);
  assert.ok(notices.length >= 3, 'migrated handlers should execute and notify without undefined-symbol failures');
}
```

Call `testNoSyncRedisNamesRemain()` and `await testMigratedCommandHandlersExecute()`. This runtime smoke compensates for the current repo environment lacking `tsc` and catches deleted-helper references inside command handlers.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL if old console/sync helpers remain.

- [ ] **Step 3: Write minimal implementation**

Simplest acceptable Phase 1 demotion: remove `makeConsoleComponent` or stop using it, and register both commands as debug warnings:

```ts
const notifyDebugConsoleDemoted = (ctx: ExtensionCommandContext) => {
  ctx.ui.notify('/arb-console is debug-only in this Phase 1 build. Use /arb-watch for the native compact cached widget, /arb-status for a one-shot snapshot, or /arb-collect for explicit logs.', 'warning');
};

pi.registerCommand('arb-console', {
  description: 'Debug-only placeholder; use /arb-watch for native compact ARB visibility',
  handler: async (_args, ctx) => notifyDebugConsoleDemoted(ctx),
});

pi.registerCommand('arb-debug-console', {
  description: 'Debug-only placeholder; disabled until it renders from cached async state',
  handler: async (_args, ctx) => notifyDebugConsoleDemoted(ctx),
});
```

Update `/arb-status` handler to:

```ts
handler: async (_args, ctx) => {
  lastCtx = ctx;
  await pollOnce(ctx);
  const lines = listJobs().map((job) => {
    const suffix = job.rawState ? `\n  redis: state=${job.rawState} phase=${job.phase || ''} ok=${job.ok === undefined ? '' : String(job.ok)} summary=${(job.summary || '').slice(0, 140)}` : '';
    return summarizeJob(job) + suffix;
  });
  ctx.ui.notify(lines.length ? lines.join('\n') : 'No ARB dispatch jobs in this session', 'info');
  updateStatus(ctx);
},
```

Before deleting `refreshJobRedis`, migrate `adoptJob()` in this same task so `/arb-adopt` has no reference to the removed helper. Update `adoptJob()` to create jobs with `recentEvents: []`, call `ensurePoller(ctx)` if the job is non-terminal, and call `void pollOnce(ctx).then(() => updateWatchWidget(ctx))` instead of sync Redis refresh. Then remove old `refreshJobRedis`, `makeConsoleComponent`, `redisLines`, `hgetall`, and `spawnSync` code. Keep `/arb-collect` bounded and explicit.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; build/load passes; grep assertions pass.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "fix: remove sync Redis I/O from ARB monitor UI paths"
```

---

### Task 10: Fix parent fd lifecycle and launch/adopt integration

**Files:**
- Modify: `pi-extensions/arb-dispatch-monitor.ts`
- Modify: `pi-extensions/arb-dispatch-monitor.test.mjs`

**Interfaces:**
- Consumes: existing `launchDispatch`, `adoptJob`, `adoptRunFromLogs`.
- Produces: closed parent fds on spawn success/throw; adopted jobs populate snapshot; child exit updates terminal state, vote, auto-synthesis, and widget.

- [ ] **Step 1: Write the failing smoke test**

Append:

```js
async function testFakeDispatchCompletesAndQueuesLogs() {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-pi-monitor-'));
  fs.mkdirSync(path.join(tmp, 'bridge/scripts'), { recursive: true });
  fs.mkdirSync(path.join(tmp, 'proj/.arb/envs'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'proj/.arb/envs/bridge-common.env'), 'AGENT_REDIS_PREFIX=agent_scratch:\n');
  fs.writeFileSync(path.join(tmp, 'bridge/scripts/agent-dispatch'), '#!/usr/bin/env bash\necho "task-id: 00000000-0000-4000-8000-000000000001" >&2\necho "{\\"result\\":\\"ok\\"}"\nexit 0\n', { mode: 0o755 });
  const { tools } = loadExtension();
  const notices = [];
  const ctx = { cwd: path.join(tmp, 'proj'), mode: 'tui', ui: { notify: (...a) => notices.push(a), setStatus() {}, setWidget() {} } };
  const result = await tools[0].execute('tc', {
    targetId: 'seat-a', task: 'fake', runId: 'run-a', bridgeRoot: path.join(tmp, 'bridge'), cwd: path.join(tmp, 'proj'), envFile: path.join(tmp, 'proj/.arb/envs/bridge-common.env'), logDir: path.join(tmp, 'proj/.arb/logs'),
  }, undefined, undefined, ctx);
  assert.ok(result.content[0].text.includes('seat-a'));
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.ok(notices.some((n) => String(n[0]).includes('completed')));
  const outs = fs.readdirSync(path.join(tmp, 'proj/.arb/logs')).filter((n) => n.endsWith('.out'));
  const errs = fs.readdirSync(path.join(tmp, 'proj/.arb/logs')).filter((n) => n.endsWith('.err'));
  assert.equal(outs.length, 1);
  assert.equal(errs.length, 1);
}
```

Call `await testFakeDispatchCompletesAndQueuesLogs()`.

- [ ] **Step 2: Run launch lifecycle smoke**

This is a regression smoke, not a red/green unit test: fd cleanup may already be present in the current checkout. Expected before the Task 10 implementation is either a meaningful failure that Step 3 fixes, or a pass that becomes permanent regression coverage for dispatch/log/exit behavior.

- [ ] **Step 3: Write/confirm implementation**

First update the existing `node:fs` import in this task to include `closeSync` alongside `openSync`. With Task 5's import already applied, the target line is:

```ts
import { closeSync, existsSync, mkdirSync, openSync, promises as fsPromises, readFileSync, readdirSync } from 'node:fs';
```

Then ensure `launchDispatch()` uses this exact fd pattern:

```ts
let outFd: number | undefined;
let errFd: number | undefined;
let child: ChildProcessWithoutNullStreams;
try {
  outFd = openSync(stdoutPath, 'a');
  errFd = openSync(stderrPath, 'a');
  child = spawn('bash', args, { cwd: bridgeRoot, env: { ...process.env, PYTHONPATH: join(bridgeRoot, 'src'), AGENT_ENV_FILE: envFile, FROM_AGENT_ID: fromAgentId, BRANCH: branch }, stdio: ['ignore', outFd, errFd] }) as ChildProcessWithoutNullStreams;
} catch (err) {
  if (outFd !== undefined) try { closeSync(outFd); } catch { /* ignore */ }
  if (errFd !== undefined) try { closeSync(errFd); } catch { /* ignore */ }
  throw err;
}
if (outFd !== undefined) closeSync(outFd);
if (errFd !== undefined) closeSync(errFd);
```

In child `exit`/`error` handlers, set `processAlive=false`, update terminal state, call `tryRefreshVoteFromStdout(job)`, `maybeAutoSynthesize(job.runId, ...)` if that feature exists, `void pollOnce(...).then(() => updateWatchWidget(...))`, and `updateStatus(...)`.

Confirm `adoptJob()` remains on the async path introduced in Task 9: jobs include `recentEvents: []`, non-terminal adopted jobs call `ensurePoller(ctx)`, and the handler calls `void pollOnce(ctx).then(() => updateWatchWidget(ctx))`. Do not reintroduce `refreshJobRedis`.

- [ ] **Step 4: Run test/build to verify pass**

Expected: focused tests pass; fake dispatch produces one `.out` and one `.err`; build/load passes.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "fix: harden ARB dispatch launch lifecycle"
```

---

### Task 11: Update README and run final validation

**Files:**
- Modify: `pi-extensions/README.md`

**Interfaces:**
- Consumes: final command behavior.
- Produces: user-facing docs for cached below-editor `/arb-watch` and debug `/arb-console`.

- [ ] **Step 1: Write the failing doc check**

Append to test file:

```js
function testReadmeMentionsCachedWidget() {
  const fs = require('node:fs');
  const text = fs.readFileSync('pi-extensions/README.md', 'utf8');
  assert.ok(text.includes('below-editor') || text.includes('below editor'));
  assert.ok(text.includes('cached'));
  assert.ok(text.includes('/arb-console'));
  assert.ok(text.includes('debug'));
}
```

Call `testReadmeMentionsCachedWidget()`.

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL until README mentions new behavior.

- [ ] **Step 3: Write minimal documentation update**

In `pi-extensions/README.md`, update the command bullets to include:

```markdown
- Command: `/arb-watch [run-id|all]` — toggle a compact below-editor widget rendered from cached async Redis/log status. The widget does not stream task events into model context and does not take over input.
- Command: `/arb-hide` — hide the compact widget. Active jobs may continue polling in the background until terminal.
- Command: `/arb-console` — debug-only/demoted in Phase 1; use `/arb-watch` for native compact visibility and `/arb-status` or `/arb-collect` for explicit snapshots/log snippets.
```

Also update the observability paragraph to state:

```markdown
`/arb-watch` polls Redis asynchronously every ~3 seconds with a single-in-flight guard and bounded per-tick Redis subprocess fan-out, then renders from a cached snapshot. TUI render functions do not perform Redis, process, file, or network I/O.
```

- [ ] **Step 4: Run full validation**

```bash
NODE_PATH=/home/<user>/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/node_modules \
  node pi-extensions/arb-dispatch-monitor.test.mjs
bun build pi-extensions/arb-dispatch-monitor.ts --target=node --packages=external --outfile=/tmp/arb-dispatch-monitor.js
if command -v tsc >/dev/null 2>&1; then tsc --noEmit; else echo "tsc unavailable; relying on focused command-handler smokes"; fi
git diff --check -- pi-extensions/arb-dispatch-monitor.ts pi-extensions/arb-dispatch-monitor.test.mjs pi-extensions/README.md
rg "spawnSync|HGETALL|refreshAll\(\)|renderSnapshotLines\(watchSnapshot, 100|setWidget\('arb-watch', renderSnapshotLines" pi-extensions/arb-dispatch-monitor.ts
```

Expected:
- test prints `arb-dispatch-monitor tests passed`;
- bun build succeeds;
- `tsc --noEmit` runs if available; otherwise the test suite's focused command-handler smokes are the required undefined-symbol guard;
- `git diff --check` prints nothing;
- final `rg` exits non-zero with no matches, or only matches `readFileSync` in explicit `/arb-collect`/vote parsing paths outside render/poll.

- [ ] **Step 5: Commit**

```bash
git add pi-extensions/README.md pi-extensions/arb-dispatch-monitor.test.mjs
git commit -m "docs: document ARB watch cached widget behavior"
```

---

### Task 12: Manual pi validation and implementation report

**Files:**
- Create: `docs/superpowers/reviews/2026-07-05-pi-arb-dispatch-monitor-widget-tdd-report.md`

**Interfaces:**
- Consumes: final patch and pi runtime.
- Produces: validation report for patch-review panel.

- [ ] **Step 1: Run automated final checks**

```bash
NODE_PATH=/home/<user>/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/node_modules \
  node pi-extensions/arb-dispatch-monitor.test.mjs
bun build pi-extensions/arb-dispatch-monitor.ts --target=node --packages=external --outfile=/tmp/arb-dispatch-monitor.js
if command -v tsc >/dev/null 2>&1; then tsc --noEmit; else echo "tsc unavailable; relying on focused command-handler smokes"; fi
git diff --check
```

Expected: all available checks pass; if `tsc` is unavailable, record that fact and the command-handler smoke results in the report.

- [ ] **Step 2: Run short real or fake dispatch smoke in pi**

In pi after `/reload`, run a short fake or real dispatch with `/arb-watch` visible. Verify and record:

- `/arb-watch` appears below editor, not as full-screen replacement.
- Editor remains usable while poll timer runs.
- Completion notification fires.
- `.arb/logs/arb-dispatch-*.out/.err` are written.
- Terminal job renders completed/failed without mis-parsing `ok="false"` as ok.

- [ ] **Step 3: Verify reload cleanup manually**

With `/arb-watch` visible, run `/reload`, then `/arb-watch` again. Verify there is one widget and no duplicate rapid status updates. If possible, inspect notifications/logs for duplicate timer symptoms.

- [ ] **Step 4: Write report**

Create `docs/superpowers/reviews/2026-07-05-pi-arb-dispatch-monitor-widget-tdd-report.md`:

```markdown
# pi ARB dispatch monitor widget TDD report

## Summary
Implemented `docs/superpowers/plans/2026-07-05-pi-arb-dispatch-monitor-widget.md` task by task.

## Commits
- `<sha>` — `<subject>`

## Automated validation
- `NODE_PATH=... node pi-extensions/arb-dispatch-monitor.test.mjs` — PASS
- `bun build pi-extensions/arb-dispatch-monitor.ts --target=node --packages=external --outfile=/tmp/arb-dispatch-monitor.js` — PASS
- `git diff --check` — PASS

## Manual pi validation
- `/reload` — PASS/FAIL with notes
- `/arb-watch` below-editor widget — PASS/FAIL with notes
- short dispatch logging/completion — PASS/FAIL with paths
- reload duplicate timer check — PASS/FAIL with notes

## Deviations from plan/spec
- None, or list exact deviations and rationale.
```

- [ ] **Step 5: Commit report**

```bash
git add docs/superpowers/reviews/2026-07-05-pi-arb-dispatch-monitor-widget-tdd-report.md
git commit -m "docs: report ARB pi widget TDD validation"
```

---

## Self-Review Checklist

- Spec coverage: Tasks 2-4 cover model/classification/render; Tasks 6-8 cover async polling, cache, lifecycle; Task 5 covers votes; Task 9 covers debug console and one-shot commands; Task 10 covers fd leak/dispatch/adopt; Task 11 covers docs; Task 12 covers validation.
- Placeholder scan: no placeholder markers or undefined future function names are intended. Every helper referenced by a later task is introduced by an earlier task, with auto-synthesis explicitly treated as existing behavior to preserve in this checkout.
- Type consistency: `ArbJob`, `WatchSnapshot`, `NormalizedStatus`, `ArbVote`, `parseRedisOk`, `normalizeStatus`, `classifyJob`, `applyStatusToJob`, `renderSnapshotLines`, `makeWatchComponent`, `execFileText`, `createLimit`, `pollJobs`, and `pollOnce` names are used consistently.
- Auto-synthesis handling: the current checkout contains `checkAutoSynthesisRuns`; the plan now makes preservation explicit. If a clean-base implementer lacks that helper, they must omit the call rather than introduce an undefined reference.
