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

function baseJob(overrides = {}) {
  return {
    id: 'j1', runId: 'r1', targetId: 'seat-a', engine: 'codex', task: 'x',
    state: 'running', startedAt: Date.now() - 1000, stdoutPath: '/tmp/out', stderrPath: '/tmp/err',
    envFile: '/tmp/env', redisPrefix: 'agent_scratch:', processAlive: true,
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
  assert.equal(mod.__test.classifyJob(baseJob({ state: 'failed', rawState: 'running', processAlive: false, seatHeartbeatTtl: -2, updatedAt: old }), { rawState: 'running', updatedAt: old }, now), 'failed');
  assert.equal(mod.__test.classifyJob(baseJob({ state: 'completed', rawState: 'running', processAlive: false, updatedAt: old }), {}, now), 'completed');

  const job = baseJob();
  mod.__test.applyStatusToJob(job, { rawState: 'completed', ok: false, summary: 'done', updatedAt: recent }, now);
  assert.equal(job.state, 'completed');
  assert.equal(job.ok, false);
  assert.equal(job.summary, 'done');

  const locallyDone = baseJob({ state: 'completed', rawState: 'running', processAlive: false, updatedAt: old });
  mod.__test.applyStatusToJob(locallyDone, { rawState: 'running', updatedAt: old }, now);
  assert.equal(locallyDone.state, 'completed');
  assert.equal(locallyDone.finishedAt, now);
}

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

function testCleanupAndWatchCommandShape() {
  const { commands } = loadExtension();
  const watch = commands.get('arb-watch');
  assert.ok(watch.description.includes('below-editor') || watch.description.includes('compact'));
  assert.equal(typeof mod.__test.cleanupForTests, 'function');
}

async function testHideKeepsPollingActiveJobs() {
  const { commands } = loadExtension();
  const widgets = [];
  const ctx = { cwd: process.cwd(), mode: 'tui', ui: { notify() {}, setStatus() {}, setWidget: (...a) => widgets.push(a) } };
  await commands.get('arb-adopt').handler('00000000-0000-4000-8000-000000000002 seat-a', ctx);
  assert.equal(mod.__test.isPollerActiveForTests(), true, 'adopting an active job should start the poller');
  await commands.get('arb-hide').handler('', ctx);
  assert.equal(mod.__test.isPollerActiveForTests(), true, 'hiding the widget must not stop polling active jobs');
  assert.ok(widgets.some((args) => args[0] === 'arb-watch' && args[1] === undefined), 'hide should clear the widget');
  mod.__test.cleanupForTests();
}

function testNoSyncRedisNamesRemain() {
  // Guard intent: Redis/status polling and render must not block the TUI event loop
  // via spawnSync. rem5 F4 — execFileSync for the pre-spawn harness-publish mint is a
  // deliberate accepted trade (do not redesign the process model). Strength enforced:
  //   - spawnSync banned
  //   - bare execSync banned (word-boundary; does not match execFileSync)
  //   - execFileSync allowed only for the single harness-publish call site
  const fs = require('node:fs');
  const src = fs.readFileSync('pi-extensions/arb-dispatch-monitor.ts', 'utf8');
  assert.equal(src.includes('spawnSync'), false, 'spawnSync must not remain');
  assert.equal(/\bexecSync\b/.test(src), false, 'execSync must not remain');
  const syncCalls = [...src.matchAll(/\bexecFileSync\s*\(/g)];
  assert.equal(syncCalls.length, 1, 'exactly one execFileSync call (harness-publish mint)');
  assert.ok(
    /execFileSync\s*\(\s*join\(\s*bridgeRoot,\s*["']scripts\/arb-memory-harness-publish["']/.test(src)
    || /execFileSync[\s\S]{0,200}arb-memory-harness-publish/.test(src),
    'execFileSync must target arb-memory-harness-publish only',
  );
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
  mod.__test.cleanupForTests();
}

function installAuthorityDispatchStubs(bridgeScriptsDir, { agentDispatchBody } = {}) {
  const fs = require('node:fs');
  const path = require('node:path');
  fs.mkdirSync(bridgeScriptsDir, { recursive: true });
  // Publish stub: production shape = multi-line indent=2 JSON with ALL SIX
  // HarnessPublishReceipt fields (dispatch_authority.py). Last-line-only parse
  // must RED against this stub (rem5 F1 acceptance mutation).
  // Records argv so F2 can assert --env-file reaches the publish subprocess.
  const pubStub = [
    '#!/usr/bin/env bash',
    `echo "$*" > "${path.join(bridgeScriptsDir, 'publish-argv.txt')}"`,
    "cat <<'EOF'",
    '{',
    '  "artefact_id": "art-stub-1",',
    '  "version": 1,',
    '  "target_agent_id": "seat",',
    '  "registration_generation": "gen-stub-1",',
    '  "worker_vantage": "test-vantage",',
    '  "content_hash": "sha256:stub-content-hash"',
    '}',
    'EOF',
    'exit 0',
    '',
  ].join('\n');
  fs.writeFileSync(
    path.join(bridgeScriptsDir, 'arb-memory-harness-publish'),
    pubStub,
    { mode: 0o755 },
  );
  // Dispatch stub: require quartet flags so the OLD positional shape fails (acceptance mut 3).
  const body = agentDispatchBody || [
    '#!/usr/bin/env bash',
    'has_art=0',
    'for a in "$@"; do [ "$a" = "--artefact-id" ] && has_art=1; done',
    'if [ "$has_art" -eq 0 ]; then echo "missing quartet" >&2; exit 2; fi',
    'echo "ARGS: $*" >&2',
    'echo "task-id: 00000000-0000-4000-8000-000000000001" >&2',
    'echo "{\\"result\\":\\"ok\\"}"',
    'exit 0',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(bridgeScriptsDir, 'agent-dispatch'), body, { mode: 0o755 });
  if (!process.env.ARB_MEMORY_REDIS_URL) {
    process.env.ARB_MEMORY_REDIS_URL = 'redis://test-stub-publish';
  }
}

async function testFakeDispatchCompletesAndQueuesLogs() {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-pi-monitor-'));
  fs.mkdirSync(path.join(tmp, 'proj/.arb/envs'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'proj/.arb/envs/bridge-common.env'), 'AGENT_REDIS_PREFIX=agent_scratch:\n');
  installAuthorityDispatchStubs(path.join(tmp, 'bridge/scripts'));
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
  mod.__test.cleanupForTests();
}

function testSynthesisPromptContracts() {
  const run = { runId: 'run-p', expectedTargets: ['seat-a', 'seat-b'], fired: false, createdAt: Date.now() };
  const jobs = [baseJob({ targetId: 'seat-a', state: 'completed' }), baseJob({ targetId: 'seat-b', state: 'failed' })];
  const prompt = mod.__test.buildDefaultSynthesisPrompt(run, jobs);
  for (const needle of ['roster', 'stance table', 'hinge-claim', 'vote gaps', 'facts separated from severity']) {
    assert.ok(prompt.includes(needle), `default synthesis prompt must require: ${needle}`);
  }
  const deadline = mod.__test.buildDeadlinePrompt(run, ['seat-missing'], [jobs[0]], jobs);
  assert.ok(deadline.includes('DEADLINE'));
  assert.ok(deadline.includes('seat-missing'));
  assert.ok(deadline.includes('NAMED absent vote'));
  assert.ok(deadline.includes('re-fire'));
}

async function testBarrierDeadlineFiresVoteGap() {
  const { commands, sent } = loadExtension();
  const ctx = { cwd: process.cwd(), mode: 'tui', ui: { notify() {}, setStatus() {}, setWidget() {} } };
  await commands.get('arb-auto-synthesize').handler(JSON.stringify({
    runId: 'run-deadline-test', expectedTargets: ['seat-never-dispatched'], deadlineMinutes: 0.001,
  }), ctx);
  assert.equal(sent.length, 0, 'barrier must not fire before the deadline');
  mod.__test.checkAutoSynthesisRunsForTests(Date.now() + 10_000);
  assert.equal(sent.length, 1, 'deadline expiry must queue exactly one vote-gap follow-up');
  assert.ok(sent[0].content.includes('DEADLINE'));
  assert.ok(sent[0].content.includes('seat-never-dispatched'));
  assert.ok(sent[0].content.includes('re-fire'));
  mod.__test.checkAutoSynthesisRunsForTests(Date.now() + 20_000);
  assert.equal(sent.length, 1, 'a fired deadline must not re-fire');
  mod.__test.cleanupForTests();
}

async function testWorktreeArgsPassthrough() {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-worktree-'));
  fs.mkdirSync(path.join(tmp, 'proj/.arb/envs'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'proj/.arb/envs/bridge-common.env'), 'AGENT_REDIS_PREFIX=agent_scratch:\n');
  // Stub requires --artefact-id: OLD positional argv (task trailing, no quartet) exits 2.
  installAuthorityDispatchStubs(path.join(tmp, 'bridge/scripts'), {
    agentDispatchBody: [
      '#!/usr/bin/env bash',
      'has_art=0',
      'for a in "$@"; do [ "$a" = "--artefact-id" ] && has_art=1; done',
      'if [ "$has_art" -eq 0 ]; then echo "missing quartet" >&2; exit 2; fi',
      'echo "ARGS: $*" >&2',
      'echo "task-id: 00000000-0000-4000-8000-000000000003" >&2',
      'echo "{\\"result\\":\\"ok\\"}"',
      'exit 0',
      '',
    ].join('\n'),
  });
  const { tools } = loadExtension();
  const ctx = { cwd: path.join(tmp, 'proj'), mode: 'tui', ui: { notify() {}, setStatus() {}, setWidget() {} } };
  const taskText = 'review-the-diff-fake-task';
  await tools[0].execute('tc', {
    targetId: 'seat-wt', task: taskText, runId: 'run-wt', bridgeRoot: path.join(tmp, 'bridge'), cwd: path.join(tmp, 'proj'),
    envFile: path.join(tmp, 'proj/.arb/envs/bridge-common.env'), logDir: path.join(tmp, 'proj/.arb/logs'),
    worktree: 'review-codex', worktreeBase: 'HEAD', worktreeCleanup: 'keep',
  }, undefined, undefined, ctx);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const errName = fs.readdirSync(path.join(tmp, 'proj/.arb/logs')).find((n) => n.endsWith('.err'));
  const err = fs.readFileSync(path.join(tmp, 'proj/.arb/logs', errName), 'utf8');
  // NEW shape: quartet flags + worktree flags; no raw task as trailing argv.
  assert.ok(err.includes('--artefact-id'), `quartet --artefact-id missing: ${err}`);
  assert.ok(err.includes('--version'), `quartet --version missing: ${err}`);
  assert.ok(err.includes('--receipt'), `quartet --receipt missing: ${err}`);
  assert.ok(err.includes('--brief'), `quartet --brief missing: ${err}`);
  assert.ok(err.includes('--worktree review-codex'), err);
  assert.ok(err.includes('--worktree-base HEAD'), err);
  assert.ok(err.includes('--worktree-cleanup keep'), err);
  // Raw task must not appear as a trailing free-form argv element.
  const argsLine = err.split('\n').find((l) => l.startsWith('ARGS: ')) || '';
  const argvTail = argsLine.replace(/^ARGS:\s*/, '').trim().split(/\s+/);
  assert.ok(!argvTail.includes(taskText), `positional task must be absent from argv; got: ${argsLine}`);
  assert.ok(!err.includes('missing quartet'), `stub rejected argv (old shape?): ${err}`);
  mod.__test.cleanupForTests();
}

async function testPublishPassesEnvFile() {
  // rem5 F2: publisher consumes --env-file, not AGENT_ENV_FILE alone.
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-envfile-'));
  fs.mkdirSync(path.join(tmp, 'proj/.arb/envs'), { recursive: true });
  const envPath = path.join(tmp, 'proj/.arb/envs/bridge-common.env');
  fs.writeFileSync(envPath, 'AGENT_REDIS_PREFIX=agent_scratch:\n');
  installAuthorityDispatchStubs(path.join(tmp, 'bridge/scripts'));
  const { tools } = loadExtension();
  const ctx = { cwd: path.join(tmp, 'proj'), mode: 'tui', ui: { notify() {}, setStatus() {}, setWidget() {} } };
  await tools[0].execute('tc', {
    targetId: 'seat-ef', task: 'fake', runId: 'run-ef', bridgeRoot: path.join(tmp, 'bridge'),
    cwd: path.join(tmp, 'proj'), envFile: envPath, logDir: path.join(tmp, 'proj/.arb/logs'),
  }, undefined, undefined, ctx);
  const pubArgvPath = path.join(tmp, 'bridge/scripts/publish-argv.txt');
  assert.ok(fs.existsSync(pubArgvPath), 'publish stub must record argv');
  const pubArgv = fs.readFileSync(pubArgvPath, 'utf8');
  assert.ok(pubArgv.includes('--env-file'), `publish argv missing --env-file: ${pubArgv}`);
  assert.ok(pubArgv.includes(envPath), `publish argv missing env path: ${pubArgv}`);
  // Receipt on disk must carry all six HarnessPublishReceipt fields (faithful stub).
  const receiptName = fs.readdirSync(path.join(tmp, 'proj/.arb/logs')).find((n) => n.endsWith('.receipt.json'));
  assert.ok(receiptName, 'receipt file missing');
  const receipt = JSON.parse(fs.readFileSync(path.join(tmp, 'proj/.arb/logs', receiptName), 'utf8'));
  for (const k of ['artefact_id', 'version', 'target_agent_id', 'registration_generation', 'worker_vantage', 'content_hash']) {
    assert.ok(k in receipt, `receipt missing field ${k}: ${JSON.stringify(receipt)}`);
  }
  mod.__test.cleanupForTests();
}

async function testMisrouteWindowExpiry() {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-misroute-'));
  const stderrPath = path.join(tmp, 'seat.err');
  fs.writeFileSync(stderrPath, 'dispatch starting, no task id line\n');
  const now = Date.now();
  const job = baseJob({ id: 'mr', taskId: undefined, stderrPath, state: 'running', startedAt: now - 70_000 });
  await mod.__test.pollJobsForTests([job], { readStatus: async () => ({}), readTtl: async () => undefined, nowMs: now });
  assert.equal(job.taskIdChecked, true, 'expired window must stop retries');
  assert.equal(job.taskIdMissedWindow, true, 'expired window must be flagged as a possible misroute');
  const lines = mod.__test.renderSnapshotLines({ generatedAt: now, jobs: [job] }, 100, false);
  assert.ok(lines.some((line) => line.includes('misroute')), lines.join('\n'));
}

async function testVoteTailRecoveryOnLargeStdout() {
  assert.equal(mod.__test.decodeJsonStringEscapes('{\\"a\\":1}\\nx'), '{"a":1}\nx');
  assert.equal(mod.__test.decodeJsonStringEscapes('a\\\\nb'), 'a\\nb', 'escaped backslash must not become a newline');
  const plain = mod.__test.parseVoteFenceFromText('text\n```vote\n{"stance":"reject"}\n```\n');
  assert.equal(plain.stance, 'reject');

  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-vote-tail-'));
  const stdoutPath = path.join(tmp, 'seat.out');
  const bigReply = `${'x'.repeat(400_000)}\nVerdict prose\n\`\`\`vote\n{"stance":"approve","severity":"P3","note":"tail"}\n\`\`\``;
  fs.writeFileSync(stdoutPath, JSON.stringify({ result: bigReply }));
  const job = baseJob({ id: 'vt', state: 'completed', stdoutPath });
  await mod.__test.tryRefreshVoteFromStdout(job);
  assert.equal(job.vote?.stance, 'approve', 'vote fence beyond the head cap must be recovered from the tail');
  assert.equal(job.vote?.severity, 'P3');
}

async function testLastFenceWinsOverQuotedExample() {
  const twoFences = 'the expected format is\n```vote\n{"stance":"reject","note":"example"}\n```\nVerdict prose\n```vote\n{"stance":"approve","severity":"P3","note":"real"}\n```';
  const fromText = mod.__test.parseVoteFenceFromText(twoFences);
  assert.equal(fromText.stance, 'approve', 'raw-text parser must take the LAST fence');
  assert.equal(fromText.note, 'real');
  const fromJson = mod.__test.parseVoteFromDispatcherStdoutText(JSON.stringify({ result: twoFences }));
  assert.equal(fromJson.stance, 'approve', 'whole-JSON parser must take the LAST fence');

  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-vote-last-'));
  const stdoutPath = path.join(tmp, 'seat.out');
  fs.writeFileSync(stdoutPath, JSON.stringify({ result: `${'x'.repeat(400_000)}\n${twoFences}` }));
  const job = baseJob({ id: 'vl', state: 'completed', stdoutPath });
  await mod.__test.tryRefreshVoteFromStdout(job);
  assert.equal(job.vote?.stance, 'approve', 'tail recovery must take the LAST fence, not a quoted example');
}

async function testDeadlineWithoutAutoSynthesizeWarns() {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'arb-noarm-'));
  fs.mkdirSync(path.join(tmp, 'proj/.arb/envs'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'proj/.arb/envs/bridge-common.env'), 'AGENT_REDIS_PREFIX=agent_scratch:\n');
  installAuthorityDispatchStubs(path.join(tmp, 'bridge/scripts'), {
    agentDispatchBody: '#!/usr/bin/env bash\nexit 0\n',
  });
  const { tools } = loadExtension();
  const notices = [];
  const ctx = { cwd: path.join(tmp, 'proj'), mode: 'tui', ui: { notify: (...a) => notices.push(a), setStatus() {}, setWidget() {} } };
  await tools[0].execute('tc', {
    targetId: 'seat-na', task: 'fake', runId: 'run-na', bridgeRoot: path.join(tmp, 'bridge'), cwd: path.join(tmp, 'proj'),
    envFile: path.join(tmp, 'proj/.arb/envs/bridge-common.env'), logDir: path.join(tmp, 'proj/.arb/logs'),
    deadlineMinutes: 30,
  }, undefined, undefined, ctx);
  assert.ok(notices.some((n) => String(n[0]).includes('autoSynthesize')), 'deadlineMinutes without autoSynthesize must warn, not silently no-op');
  mod.__test.cleanupForTests();
}

function testActiveStateCounting() {
  const counts = mod.__test.countJobStates([
    baseJob({ state: 'queued' }), baseJob({ state: 'quiet-alive' }), baseJob({ state: 'stale' }),
    baseJob({ state: 'running' }), baseJob({ state: 'completed' }), baseJob({ state: 'timeout' }),
  ]);
  assert.equal(counts.active, 4, 'queued/quiet-alive/stale/running are all active');
  assert.equal(counts.done, 2);
}

function testReadmeMentionsCachedWidget() {
  const fs = require('node:fs');
  const text = fs.readFileSync('pi-extensions/README.md', 'utf8');
  assert.ok(text.includes('below-editor') || text.includes('below editor'));
  assert.ok(text.includes('cached'));
  assert.ok(text.includes('/arb-console'));
  assert.ok(text.includes('debug'));
}

async function main() {
  testRegistration();
  testHelperSeam();
  testStatusNormalization();
  testClassification();
  testSnapshotRender();
  testVoteParsing();
  await testAsyncSubprocessAndLimiter();
  await testPollOnceGuardAndSnapshot();
  testCleanupAndWatchCommandShape();
  await testHideKeepsPollingActiveJobs();
  testNoSyncRedisNamesRemain();
  await testMigratedCommandHandlersExecute();
  await testFakeDispatchCompletesAndQueuesLogs();
  testSynthesisPromptContracts();
  await testBarrierDeadlineFiresVoteGap();
  await testWorktreeArgsPassthrough();
  await testPublishPassesEnvFile();
  await testMisrouteWindowExpiry();
  await testVoteTailRecoveryOnLargeStdout();
  await testLastFenceWinsOverQuotedExample();
  await testDeadlineWithoutAutoSynthesizeWarns();
  testActiveStateCounting();
  testReadmeMentionsCachedWidget();
  console.log('arb-dispatch-monitor tests passed');
}

main().catch((err) => {
  console.error(err.stack || err);
  process.exit(1);
});
