import { spawn, execFileSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { closeSync, mkdirSync, readFileSync, existsSync, openSync, writeFileSync, promises as fsPromises, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@earendil-works/pi-coding-agent";

/** Mirror agent_redis_bridge.dispatch_cli.wrap_instructions_as_brief for harness-publish validation. */
const wrapInstructionsAsBrief = (instructions: string, title = "Dispatch brief"): string => {
  const assumptions = JSON.stringify({ items: [] }, null, 2);
  const body = instructions.trim();
  return (
    `# ${title}\n\n` +
    `## Assumptions\n\`\`\`json\n${assumptions}\n\`\`\`\n\n` +
    `## Instructions\n\n${body}\n`
  );
};

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

const STATUS_FIELDS = ["task_id", "state", "phase", "last_summary", "updated_at", "ok", "error", "queue_depth", "enqueued_at"] as const;

const parseRedisOk = (value: string | undefined): boolean | undefined => {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
};

const parseOptionalNumber = (value: string | undefined): number | undefined => {
  if (value === undefined || value === "") return undefined;
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

const parseVoteFromDispatcherStdoutText = (text: string): ArbVote | undefined => {
  try {
    const outer = JSON.parse(text) as { result?: unknown };
    if (typeof outer.result !== "string") return undefined;
    // The real vote is the LAST fence — an earlier match may be an example quoted in prose.
    const matches = [...outer.result.matchAll(/```vote\s*\n([\s\S]*?)\n```/g)];
    const match = matches.length ? matches[matches.length - 1] : undefined;
    if (!match) return undefined;
    const parsed = JSON.parse(match[1]) as { stance?: unknown; severity?: unknown; refs?: unknown; note?: unknown };
    return {
      stance: typeof parsed.stance === "string" ? parsed.stance : undefined,
      severity: typeof parsed.severity === "string" ? parsed.severity : undefined,
      refs: Array.isArray(parsed.refs) ? parsed.refs.filter((r): r is string => typeof r === "string") : undefined,
      note: typeof parsed.note === "string" ? parsed.note : undefined,
      source: "stdout",
      seenAt: new Date().toISOString(),
    };
  } catch {
    return undefined;
  }
};

const MAX_VOTE_STDOUT_BYTES = 256_000;

const readTextFileSlice = async (path: string, maxBytes: number, position: number): Promise<string | undefined> => {
  try {
    const handle = await fsPromises.open(path, "r");
    try {
      const buffer = Buffer.alloc(maxBytes);
      const { bytesRead } = await handle.read(buffer, 0, maxBytes, position);
      return buffer.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }
  } catch {
    return undefined;
  }
};

const readTextFileCapped = (path: string, maxBytes: number): Promise<string | undefined> => readTextFileSlice(path, maxBytes, 0);

const readTextFileTail = async (path: string, maxBytes: number): Promise<string | undefined> => {
  try {
    const { size } = await fsPromises.stat(path);
    return readTextFileSlice(path, maxBytes, Math.max(0, size - maxBytes));
  } catch {
    return undefined;
  }
};

// Undo JSON-string escaping so a vote fence embedded in the dispatcher's JSON payload
// becomes regex-matchable when we only have a raw byte slice, not a parseable document.
const decodeJsonStringEscapes = (s: string): string => s
  .replace(/\\\\/g, "\u0000")
  .replace(/\\n/g, "\n")
  .replace(/\\r/g, "\r")
  .replace(/\\t/g, "\t")
  .replace(/\\"/g, '"')
  .replace(/\u0000/g, "\\");

const parseVoteFenceFromText = (text: string): ArbVote | undefined => {
  // Take the LAST fence: the vote contract puts the real vote at the END of the reply,
  // and a large reply is exactly the kind likely to quote an example fence in its prose.
  const matches = [...text.matchAll(/```vote\s*\n([\s\S]*?)\n\s*```/g)];
  const match = matches.length ? matches[matches.length - 1] : undefined;
  if (!match) return undefined;
  try {
    const parsed = JSON.parse(match[1]) as { stance?: unknown; severity?: unknown; refs?: unknown; note?: unknown };
    return {
      stance: typeof parsed.stance === "string" ? parsed.stance : undefined,
      severity: typeof parsed.severity === "string" ? parsed.severity : undefined,
      refs: Array.isArray(parsed.refs) ? parsed.refs.filter((r): r is string => typeof r === "string") : undefined,
      note: typeof parsed.note === "string" ? parsed.note : undefined,
      source: "stdout",
      seenAt: new Date().toISOString(),
    };
  } catch {
    return undefined;
  }
};

const tryRefreshVoteFromStdout = async (job: ArbJob) => {
  if (job.vote || job.voteChecked || !isTerminalState(job.state)) return;
  job.voteChecked = true;
  const head = await readTextFileCapped(job.stdoutPath, MAX_VOTE_STDOUT_BYTES);
  if (!head) return;
  let vote = parseVoteFromDispatcherStdoutText(head);
  if (!vote) {
    // The vote fence sits at the END of the reply; a payload larger than the head cap
    // truncates mid-JSON and the whole-document parse fails. Fall back to a tail slice
    // and match the fence directly on unescaped text.
    const tail = await readTextFileTail(job.stdoutPath, MAX_VOTE_STDOUT_BYTES);
    const candidate = tail ?? head;
    vote = parseVoteFenceFromText(decodeJsonStringEscapes(candidate));
  }
  if (vote) job.vote = vote;
};

const STALE_GRACE_MS = 180_000;
const PROCESS_STALE_GRACE_MS = 600_000;

const parseTimeMs = (value: string | undefined): number | undefined => {
  if (!value) return undefined;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : undefined;
};

const isTerminalState = (state: ArbTaskState) => state === "completed" || state === "failed" || state === "timeout" || state === "killed";

const classifyJob = (job: ArbJob, status: NormalizedStatus, nowMs = Date.now()): ArbTaskState => {
  const rawState = status.rawState ?? job.rawState;
  if (rawState === "completed") return "completed";
  if (rawState === "failed") return "failed";
  if (isTerminalState(job.state)) return job.state;
  if (rawState === "queued") return "queued";
  if (rawState === "running") {
    const updatedMs = parseTimeMs(status.updatedAt) ?? parseTimeMs(job.updatedAt);
    if (updatedMs === undefined && job.processAlive === true) return "running";
    const ageMs = updatedMs === undefined ? Number.POSITIVE_INFINITY : nowMs - updatedMs;
    if (ageMs <= STALE_GRACE_MS) return "running";
    const processRecentlyPlausible = job.processAlive === true && ageMs <= PROCESS_STALE_GRACE_MS;
    const heartbeatAlive = typeof job.seatHeartbeatTtl === "number" && job.seatHeartbeatTtl > 0;
    if (processRecentlyPlausible || heartbeatAlive) return "quiet-alive";
    return "stale";
  }
  if (job.taskId) return "unknown";
  return "launching";
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
  if (job.state === "completed" || job.state === "failed") job.finishedAt = job.finishedAt ?? nowMs;
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
  voteChecked?: boolean;
  taskIdChecked?: boolean;
  taskIdPollAttempts?: number;
  taskIdLastReadAt?: number;
  taskIdReadDeadlineMs?: number;
  taskIdMissedWindow?: boolean;
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

type Job = ArbJob;

type DispatchParams = {
  targetId: string;
  task: string;
  runId?: string;
  engine?: string;
  timeout?: number;
  bridgeRoot?: string;
  envFile?: string;
  fromAgentId?: string;
  branch?: string;
  cwd?: string;
  logDir?: string;
  auditPanel?: boolean;
  freshContext?: boolean;
  worktree?: string;
  worktreeBase?: string;
  worktreeCleanup?: "keep" | "auto";
  autoSynthesize?: boolean;
  expectedTargets?: string[];
  synthesisPrompt?: string;
  synthesisOutputPath?: string;
  deadlineMinutes?: number;
};

type AutoSynthesisRun = {
  runId: string;
  expectedTargets: string[];
  synthesisPrompt?: string;
  synthesisOutputPath?: string;
  fired: boolean;
  createdAt: number;
  deadlineAtMs?: number;
};

const jobs = new Map<string, ArbJob>();
const autoSynthesisRuns = new Map<string, AutoSynthesisRun>();
let lastCtx: ExtensionContext | ExtensionCommandContext | undefined;
let queueUserMessage: ((content: string, options?: { deliverAs?: "steer" | "followUp" }) => void) | undefined;
let watchEnabled = false;
let watchTimer: NodeJS.Timeout | undefined;

const CLEANUP_KEY = "__arbDispatchMonitorCleanup";
const previousCleanup = (globalThis as Record<string, unknown>)[CLEANUP_KEY];
if (typeof previousCleanup === "function") {
  try { previousCleanup(); } catch { /* stale extension cleanup is best-effort */ }
}

const cleanupRuntime = () => {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = undefined;
  watchEnabled = false;
  lastCtx = undefined;
};
(globalThis as Record<string, unknown>)[CLEANUP_KEY] = cleanupRuntime;

const nowStamp = () => new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");

const short = (s: string, n = 10) => (s.length <= n ? s : `${s.slice(0, n)}…`);

const countJobStates = (inputJobs: ArbJob[]): { active: number; done: number } => {
  const active = inputJobs.filter((j) => !isTerminalState(j.state)).length;
  return { active, done: inputJobs.length - active };
};

const updateStatus = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  const ui = ctx?.ui ?? lastCtx?.ui;
  if (!ui) return;
  const { active, done } = countJobStates([...jobs.values()]);
  if (active === 0 && done === 0) ui.setStatus("arb", undefined);
  else ui.setStatus("arb", `ARB ${active} active / ${done} done`);
};

const envFileCache = new Map<string, Record<string, string>>();

const readEnvFile = (path: string): Record<string, string> => {
  const cached = envFileCache.get(path);
  if (cached) return cached;
  const out: Record<string, string> = {};
  if (!existsSync(path)) {
    envFileCache.set(path, out);
    return out;
  }
  const text = readFileSync(path, "utf8");
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx < 0) continue;
    const key = line.slice(0, idx).trim();
    let val = line.slice(idx + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  envFileCache.set(path, out);
  return out;
};

const defaultBridgeRoot = () => process.env.ARB_BRIDGE_ROOT || "/home/<user>/AgentRedisBridge";
const defaultEnvFile = (cwd: string) => process.env.AGENT_ENV_FILE || join(cwd, ".arb/envs/bridge-common.env");
const defaultFrom = (env: Record<string, string>) => process.env.FROM_AGENT_ID || env.AGENT_DISPATCH_FROM || "pi-arb-dev";
const defaultBranch = (env: Record<string, string>) => process.env.BRANCH || env.AGENT_DISPATCH_BRANCH || "dev";
const redisPrefix = (env: Record<string, string>) => env.AGENT_REDIS_PREFIX || process.env.AGENT_REDIS_PREFIX || "agent_scratch:";

const redisEnv = (envFile: string): NodeJS.ProcessEnv => {
  const env = readEnvFile(envFile);
  const password = env.AGENT_REDIS_PASSWORD || process.env.AGENT_REDIS_PASSWORD;
  return password ? { ...process.env, REDISCLI_AUTH: password } : process.env;
};

type ExecTextResult = { ok: true; stdout: string; stderr: string } | { ok: false; stdout: string; stderr: string; timedOut?: boolean; error?: string };

const execFileText = (cmd: string, args: string[], opts: { env?: NodeJS.ProcessEnv; timeoutMs: number; maxBytes: number; cwd?: string }): Promise<ExecTextResult> => new Promise((resolve) => {
  let stdout = "";
  let stderr = "";
  let settled = false;
  const child = spawn(cmd, args, { env: opts.env, cwd: opts.cwd, stdio: ["ignore", "pipe", "pipe"] });
  const finish = (result: ExecTextResult) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    resolve(result);
  };
  const append = (kind: "stdout" | "stderr", chunk: Buffer) => {
    const next = (kind === "stdout" ? stdout : stderr) + chunk.toString("utf8");
    const capped = next.slice(0, opts.maxBytes);
    if (kind === "stdout") stdout = capped;
    else stderr = capped;
  };
  child.stdout.on("data", (chunk) => append("stdout", chunk));
  child.stderr.on("data", (chunk) => append("stderr", chunk));
  child.on("error", (err) => finish({ ok: false, stdout, stderr, error: err.message }));
  child.on("close", (code) => finish(code === 0 ? { ok: true, stdout, stderr } : { ok: false, stdout, stderr, error: `exit ${code}` }));
  const timer = setTimeout(() => {
    child.kill("SIGTERM");
    setTimeout(() => child.kill("SIGKILL"), 250).unref?.();
    finish({ ok: false, stdout, stderr, timedOut: true, error: "timeout" });
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
  return async <T>(fn: () => Promise<T>): Promise<T> => new Promise<T>((resolvePromise, reject) => {
    const start = () => {
      active += 1;
      fn().then(resolvePromise, reject).finally(() => {
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
  const out = ["-h", env.AGENT_REDIS_HOST || process.env.AGENT_REDIS_HOST || "127.0.0.1"];
  out.push("-p", env.AGENT_REDIS_PORT || process.env.AGENT_REDIS_PORT || "6390");
  out.push("-n", env.AGENT_REDIS_DB || process.env.AGENT_REDIS_DB || "12");
  out.push("--no-auth-warning");
  if ((env.AGENT_REDIS_TLS || process.env.AGENT_REDIS_TLS || "0").match(/^(1|true|yes)$/i)) out.push("--tls");
  const user = env.AGENT_REDIS_USER || process.env.AGENT_REDIS_USER;
  if (user) out.push("--user", user);
  return out;
};

const redisStatusJsonArgs = (envFile: string, key: string): string[] => [...redisBaseArgs(envFile), "EVAL", STATUS_LUA, "1", key, ...STATUS_FIELDS];

const readTaskStatus = async (envFile: string, key: string): Promise<Record<string, string>> => {
  const res = await execFileText("redis-cli", redisStatusJsonArgs(envFile, key), { env: redisEnv(envFile), timeoutMs: 2500, maxBytes: 32_000 });
  if (!res.ok) return {};
  try {
    const parsed = JSON.parse(res.stdout) as Record<string, string | null>;
    return Object.fromEntries(Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
  } catch {
    return {};
  }
};

const readSeatHeartbeatTtl = async (envFile: string, key: string): Promise<number | undefined> => {
  const res = await execFileText("redis-cli", [...redisBaseArgs(envFile), "TTL", key], { env: redisEnv(envFile), timeoutMs: 1500, maxBytes: 1000 });
  if (!res.ok) return undefined;
  const n = Number(res.stdout.trim());
  return Number.isFinite(n) ? n : undefined;
};

const parseTaskId = (stderrPath: string): string | undefined => {
  if (!existsSync(stderrPath)) return undefined;
  const text = readFileSync(stderrPath, "utf8");
  const matches = [...text.matchAll(/task-id:\s*([0-9a-f-]+)/gi)];
  return matches.length ? matches[matches.length - 1][1] : undefined;
};

const summarizeJob = (job: Job): string => {
  const age = Math.round(((job.finishedAt ?? Date.now()) - job.startedAt) / 1000);
  const tid = job.taskId ? ` task=${short(job.taskId, 8)}` : "";
  const exit = job.state === "running" ? "" : ` exit=${job.exitCode ?? job.signal ?? "?"}`;
  return `${job.id} ${job.state} target=${job.targetId}${tid}${exit} ${age}s\n  out=${job.stdoutPath}\n  err=${job.stderrPath}`;
};

const registerAutoSynthesis = (params: DispatchParams, job: Job, ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (!params.autoSynthesize) {
    if (params.expectedTargets?.length || params.synthesisPrompt || params.synthesisOutputPath || params.deadlineMinutes !== undefined) {
      ctx?.ui.notify(`ARB: auto-synthesis params ignored for ${job.runId} — set autoSynthesize: true to arm the barrier`, "warning");
    }
    return;
  }
  const expectedTargets = [...new Set((params.expectedTargets || []).map((t) => t.trim()).filter(Boolean))];
  if (expectedTargets.length === 0) {
    ctx?.ui.notify(`ARB auto-synthesis disabled for ${job.runId}: expectedTargets is required`, "warning");
    return;
  }
  const existing = autoSynthesisRuns.get(job.runId);
  const mergedTargets = existing ? [...new Set([...existing.expectedTargets, ...expectedTargets])] : expectedTargets;
  const createdAt = existing?.createdAt ?? Date.now();
  autoSynthesisRuns.set(job.runId, {
    runId: job.runId,
    expectedTargets: mergedTargets,
    synthesisPrompt: params.synthesisPrompt || existing?.synthesisPrompt,
    synthesisOutputPath: params.synthesisOutputPath || existing?.synthesisOutputPath,
    fired: existing?.fired ?? false,
    createdAt,
    deadlineAtMs: params.deadlineMinutes !== undefined ? createdAt + params.deadlineMinutes * 60_000 : existing?.deadlineAtMs,
  });
  job.autoSynthesize = true;
  job.expectedTargets = mergedTargets;
  job.synthesisPrompt = params.synthesisPrompt;
  job.synthesisOutputPath = params.synthesisOutputPath;
};

const jobLogLines = (runJobs: Job[]): string[] => runJobs.flatMap((job) => [
  `- ${job.targetId}: state=${job.state}${job.taskId ? ` task=${job.taskId}` : ""}`,
  `  stdout=${job.stdoutPath}`,
  `  stderr=${job.stderrPath}`,
]);

const buildDefaultSynthesisPrompt = (run: AutoSynthesisRun, runJobs: Job[]): string => {
  const lines = [
    `All expected ARB panel seats for run ${run.runId} are terminal.`,
    "Read every seat's stdout AND stderr from the job logs below. Produce:",
    "(1) the dispatched roster vs the seats that actually returned a verdict/vote — name any gap;",
    "(2) a per-seat stance table (seat -> verdict -> lead findings), facts separated from severity;",
    "(3) hinge-claim verification for each substantive P0/P1/P2 finding against the actual code/logs before proposing remediation;",
    "(4) an arbitrated verdict you own, with vote gaps stated explicitly — never averaged away.",
  ];
  if (run.synthesisOutputPath) lines.push(`Write the synthesis to: ${run.synthesisOutputPath}`);
  lines.push("", "Expected targets:", ...run.expectedTargets.map((t) => `- ${t}`), "", "Job logs:", ...jobLogLines(runJobs));
  lines.push("", "Do not modify files except the requested synthesis output path, if one is provided.");
  return lines.join("\n");
};

const buildDeadlinePrompt = (run: AutoSynthesisRun, missing: string[], nonTerminal: Job[], presentJobs: Job[]): string => {
  const lines = [
    `ARB auto-synthesis DEADLINE reached for run ${run.runId} before all expected seats went terminal.`,
  ];
  if (missing.length) lines.push(`Seats with no job in this session: ${missing.join(", ")}`);
  if (nonTerminal.length) lines.push(`Seats not terminal: ${nonTerminal.map((j) => `${j.targetId} (${j.state})`).join(", ")}`);
  lines.push(
    "Apply the vote-gap procedure before any synthesis:",
    "(1) verify each apparently-absent seat is really absent — check log file size, process status, and stderr, then re-read;",
    "(2) record a NAMED absent vote for any seat with no verdict;",
    "(3) re-fire absent seats rather than silently arbitrating on a shrunken roster;",
    "(4) if closing on a reduced roster, state the reduction explicitly in the verdict.",
    "", "Job logs for seats present in this session:", ...jobLogLines(presentJobs),
  );
  return lines.join("\n");
};

const queueSynthesisMessage = (run: AutoSynthesisRun, prompt: string, label: string, ctx?: ExtensionContext | ExtensionCommandContext) => {
  run.fired = true;
  const fail = (err: unknown) => {
    run.fired = false;
    const message = err instanceof Error ? err.message : String(err);
    (ctx?.ui ?? lastCtx?.ui)?.notify(`ARB ${label} failed for ${run.runId}: ${message}`, "error");
  };
  try {
    if (!queueUserMessage) throw new Error("pi.sendUserMessage is unavailable");
    // Typed void on ExtensionAPI, but tolerate a thenable so an async rejection
    // still rolls back run.fired instead of surfacing as an unhandled rejection.
    const result = queueUserMessage(prompt, { deliverAs: "followUp" }) as unknown;
    if (result && typeof (result as Promise<void>).catch === "function") {
      (result as Promise<void>).catch(fail);
    }
    (ctx?.ui ?? lastCtx?.ui)?.notify(`ARB ${label} queued for ${run.runId}`, "info");
  } catch (err) {
    fail(err);
  }
};

const maybeAutoSynthesize = (runId: string, ctx?: ExtensionContext | ExtensionCommandContext, nowMs = Date.now()) => {
  const run = autoSynthesisRuns.get(runId);
  if (!run || run.fired) return;
  const runJobs = listJobs().filter((j) => j.runId === runId);
  const byTarget = new Map(runJobs.map((j) => [j.targetId, j]));
  const missing = run.expectedTargets.filter((t) => !byTarget.has(t));
  const presentJobs = run.expectedTargets.map((t) => byTarget.get(t)).filter((j): j is Job => Boolean(j));
  const nonTerminal = presentJobs.filter((j) => !isTerminalState(j.state));
  if (missing.length === 0 && nonTerminal.length === 0) {
    const prompt = run.synthesisPrompt
      ? `${run.synthesisPrompt}\n\nRun: ${run.runId}\n\nJob logs:\n${jobLogLines(presentJobs).join("\n")}`
      : buildDefaultSynthesisPrompt(run, presentJobs);
    queueSynthesisMessage(run, prompt, "auto-synthesis", ctx);
    return;
  }
  if (run.deadlineAtMs !== undefined && nowMs >= run.deadlineAtMs) {
    queueSynthesisMessage(run, buildDeadlinePrompt(run, missing, nonTerminal, presentJobs), "auto-synthesis deadline (vote-gap)", ctx);
  }
};

const checkAutoSynthesisRuns = (ctx?: ExtensionContext | ExtensionCommandContext, nowMs = Date.now()) => {
  for (const runId of autoSynthesisRuns.keys()) maybeAutoSynthesize(runId, ctx, nowMs);
};

type WidgetTheme = { fg?: (color: string, text: string) => string };

const color = (theme: WidgetTheme | undefined, name: string, text: string) => theme?.fg ? theme.fg(name, text) : text;

const stateGlyph = (job: ArbJob) => {
  if (job.state === "completed") return job.ok === false ? "⚠" : "✓";
  if (job.state === "failed" || job.state === "timeout" || job.state === "killed") return "✗";
  if (job.state === "queued") return "◦";
  if (job.state === "quiet-alive" || job.state === "stale") return "⚠";
  return "●";
};

const stateColor = (job: ArbJob) => {
  if (job.state === "completed") return job.ok === false ? "warning" : "success";
  if (job.state === "failed" || job.state === "timeout" || job.state === "killed") return "error";
  if (job.state === "queued" || job.state === "quiet-alive" || job.state === "stale") return "warning";
  return "accent";
};

const visibleAge = (job: ArbJob, nowMs: number) => {
  const base = parseTimeMs(job.updatedAt) ?? job.startedAt;
  const seconds = Math.max(0, Math.round((nowMs - base) / 1000));
  if (seconds < 90) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
};

const prioritizeJobs = (inputJobs: ArbJob[]) => [...inputJobs].sort((a, b) => {
  const rank = (j: ArbJob) => (j.state === "running" || j.state === "quiet-alive" || j.state === "stale" || j.state === "queued") ? 0 : 1;
  return rank(a) - rank(b) || a.startedAt - b.startedAt;
});

const renderSnapshotLines = (snapshot: WatchSnapshot, width: number, expanded = false, theme?: WidgetTheme): string[] => {
  const safeWidth = Math.max(1, width || 80);
  const snapshotJobs = snapshot.filterRunId ? snapshot.jobs.filter((j) => j.runId === snapshot.filterRunId) : snapshot.jobs;
  if (safeWidth < 32) {
    const active = snapshotJobs.filter((j) => !isTerminalState(j.state)).length;
    return [fit(`ARB ${active} active / ${snapshotJobs.length - active} done`, safeWidth)];
  }
  if (snapshotJobs.length === 0) return [fit("ARB watch: no jobs started in this pi session", safeWidth)];
  const active = snapshotJobs.filter((j) => !isTerminalState(j.state)).length;
  const lines: string[] = [];
  const runLabel = snapshot.filterRunId || (new Set(snapshotJobs.map((j) => j.runId)).size === 1 ? snapshotJobs[0]?.runId : "all runs");
  lines.push(`ARB ${runLabel} · ${active} active · ${snapshotJobs.length - active} done`);
  const shown = prioritizeJobs(snapshotJobs).slice(0, expanded ? 10 : 5);
  for (const job of shown) {
    const phase = job.phase ? `/${job.phase}` : "";
    const ok = job.state === "completed" ? (job.ok === false ? " not-ok" : job.ok === true ? " ok" : "") : "";
    const task = job.taskId ? ` · task ${short(job.taskId, 8)}` : job.taskIdMissedWindow ? " · no task-id (misroute?)" : " · task pending";
    lines.push(`${color(theme, stateColor(job), stateGlyph(job))} ${job.targetId} · ${color(theme, stateColor(job), job.state)}${phase}${ok}${task} · ${color(theme, "dim", visibleAge(job, snapshot.generatedAt))}`);
    if (job.state === "queued" && job.queueDepth !== undefined) lines.push(`  queue depth ${job.queueDepth}${job.enqueuedAt ? ` · enqueued ${job.enqueuedAt}` : ""}`);
    else if (job.summary && !isTerminalState(job.state)) lines.push(`  ${job.summary}`);
    if (expanded) {
      lines.push(color(theme, "dim", `  run ${job.runId} · raw ${job.rawState || "-"} · seat ttl ${job.seatHeartbeatTtl ?? "unknown"} · out ${job.stdoutPath}`));
      if (job.vote) lines.push(`  vote ${job.vote.stance || "?"} ${job.vote.severity || ""} (${job.vote.source})`);
    }
  }
  if (shown.length < snapshotJobs.length) lines.push(`+${snapshotJobs.length - shown.length} more`);
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

const makeWatchComponent = (getExpanded: () => boolean, theme?: WidgetTheme) => ({
  render(width: number) {
    return renderSnapshotLines(watchSnapshot, width, getExpanded(), theme);
  },
  invalidate() {},
});

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
    if (!job.taskIdMissedWindow) {
      job.taskIdMissedWindow = true;
      lastCtx?.ui?.notify(
        `ARB: no task-id from ${job.targetId} after ${Math.round(TASK_ID_RETRY_WINDOW_MS / 1000)}s — dispatch may be misrouted; check the bridge log for [turn-start] and the seat inbox`,
        "warning",
      );
    }
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

const hasPendingDeadline = () => [...autoSynthesisRuns.values()].some((run) => !run.fired && run.deadlineAtMs !== undefined);

const shouldPoll = () => watchEnabled || hasPendingDeadline() || listJobs().some((job) => !isTerminalState(job.state));

const updateWatchWidget = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  const ui = ctx?.ui ?? lastCtx?.ui;
  if (!ui || !watchEnabled) return;
  const isTui = (ctx as { mode?: string } | undefined)?.mode === "tui" || (lastCtx as { mode?: string } | undefined)?.mode === "tui";
  if (isTui) {
    ui.setWidget("arb-watch", (_tui: unknown, theme: WidgetTheme | undefined) => makeWatchComponent(() => Boolean((ui as { getToolsExpanded?: () => boolean }).getToolsExpanded?.()), theme), { placement: "belowEditor" });
  } else {
    const fallbackWidth = 100;
    const fallbackLines = renderSnapshotLines(watchSnapshot, fallbackWidth, false);
    ui.setWidget("arb-watch", fallbackLines, { placement: "belowEditor" });
  }
  updateStatus(ctx ?? lastCtx);
};

const stopPoller = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = undefined;
  if (!watchEnabled) (ctx?.ui ?? lastCtx?.ui)?.setWidget("arb-watch", undefined);
};

const reconcilePoller = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (shouldPoll()) ensurePoller(ctx);
  else stopPoller(ctx);
};

const pollOnce = async (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    watchSnapshot = await pollJobs(listJobs());
    checkAutoSynthesisRuns(ctx ?? lastCtx);
  } catch (err) {
    watchSnapshot = { ...watchSnapshot, generatedAt: Date.now(), pollError: err instanceof Error ? err.message : String(err) };
  } finally {
    pollInFlight = false;
  }
};

const ensurePoller = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  if (watchTimer) return;
  watchTimer = setInterval(() => {
    if (!shouldPoll()) return stopPoller(lastCtx);
    void pollOnce(lastCtx).then(() => updateWatchWidget(lastCtx));
  }, 3000);
  watchTimer.unref?.();
  lastCtx = ctx ?? lastCtx;
};

const refreshWatch = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  void pollOnce(ctx ?? lastCtx).then(() => updateWatchWidget(ctx ?? lastCtx));
};

const startWatch = (ctx: ExtensionContext | ExtensionCommandContext) => {
  lastCtx = ctx;
  watchEnabled = true;
  ensurePoller(ctx);
  refreshWatch(ctx);
};

const stopWatch = (ctx?: ExtensionContext | ExtensionCommandContext) => {
  watchEnabled = false;
  (ctx?.ui ?? lastCtx?.ui)?.setWidget("arb-watch", undefined);
  reconcilePoller(ctx);
  updateStatus(ctx ?? lastCtx);
};

const launchDispatch = (params: DispatchParams, ctx?: ExtensionContext | ExtensionCommandContext): Job => {
  lastCtx = ctx ?? lastCtx;
  const cwd = resolve(params.cwd || ctx?.cwd || process.cwd());
  const bridgeRoot = resolve(params.bridgeRoot || defaultBridgeRoot());
  const envFile = resolve(params.envFile || defaultEnvFile(cwd));
  const envFromFile = readEnvFile(envFile);
  const fromAgentId = params.fromAgentId || defaultFrom(envFromFile);
  const branch = params.branch || defaultBranch(envFromFile);
  const engine = params.engine || "codex";
  const timeout = String(params.timeout ?? 5400);
  const runId = params.runId || `arb-${nowStamp()}-${randomUUID().slice(0, 8)}`;
  const logDir = resolve(params.logDir || join(cwd, ".arb/logs"));
  mkdirSync(logDir, { recursive: true });
  const id = `${nowStamp()}-${params.targetId.replace(/[^a-zA-Z0-9_.-]/g, "_")}-${randomUUID().slice(0, 8)}`;
  const stdoutPath = join(logDir, `arb-dispatch-${id}.out`);
  const stderrPath = join(logDir, `arb-dispatch-${id}.err`);

  // Slice 1d-iv: publish-then-quartet (no free-form positional task).
  // --worktree/--worktree-base/--worktree-cleanup stay as-is: agent-dispatch
  // still accepts them on the ordinary path and translates to --worktree-json
  // (scripts/agent-dispatch:596-602) once the quartet gate passes.
  const briefPath = join(logDir, `arb-dispatch-${id}.brief.md`);
  const receiptPath = join(logDir, `arb-dispatch-${id}.receipt.json`);
  writeFileSync(briefPath, wrapInstructionsAsBrief(params.task, "Pi arb_dispatch brief"), "utf8");
  const publishEnv: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONPATH: join(bridgeRoot, "src"),
    AGENT_ENV_FILE: envFile,
    FROM_AGENT_ID: fromAgentId,
    BRANCH: branch,
  };
  if (!String(publishEnv.ARB_MEMORY_REDIS_URL || "").trim()) {
    throw new Error(
      "ARB_MEMORY_REDIS_URL required for harness publish (Slice 1d-iv authority path)",
    );
  }
  let receipt: { artefact_id: string; version: number | string };
  try {
    // Slice 1d-iv rem5 F4: sync publish mint is a deliberate accepted trade — one
    // blocking pre-spawn mint; Redis/status polling stays async (spawn only).
    // Guard strength (testNoSyncRedisNamesRemain): other sync child-process APIs
    // banned; this call site alone is allowed for the harness-publish mint.
    // F1: whole-stdout parse (producer prints indent=2 multi-line JSON; last-line
    // pop yields "}" and throws). F2: publisher consumes --env-file, not AGENT_ENV_FILE.
    const pubOut = execFileSync(
      join(bridgeRoot, "scripts/arb-memory-harness-publish"),
      ["--target-agent-id", params.targetId, "--brief", briefPath, "--env-file", envFile],
      {
        cwd: bridgeRoot,
        env: publishEnv,
        encoding: "utf8",
        timeout: 120_000,
      },
    );
    receipt = JSON.parse(pubOut.trim());
  } catch (err) {
    throw new Error(`harness-publish failed for ${params.targetId}: ${err}`);
  }
  writeFileSync(receiptPath, JSON.stringify(receipt), "utf8");

  const args = [
    join(bridgeRoot, "scripts/agent-dispatch"),
    "--engine", engine,
    "--target-id", params.targetId,
    "--timeout", timeout,
    "--run-id", runId,
  ];
  if (params.auditPanel ?? true) args.push("--audit-panel");
  if (params.freshContext ?? true) args.push("--fresh-context");
  if (params.worktree) {
    args.push("--worktree", params.worktree);
    if (params.worktreeBase) args.push("--worktree-base", params.worktreeBase);
    if (params.worktreeCleanup) args.push("--worktree-cleanup", params.worktreeCleanup);
  }
  args.push(
    "--artefact-id", String(receipt.artefact_id),
    "--version", String(receipt.version),
    "--receipt", receiptPath,
    "--brief", briefPath,
  );

  // Enqueue without publish credential (non-FABA identity).
  const enqueueEnv: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONPATH: join(bridgeRoot, "src"),
    AGENT_ENV_FILE: envFile,
    FROM_AGENT_ID: fromAgentId,
    BRANCH: branch,
  };
  // Publish-class credentials (mirror of dispatch_authority.PUBLISH_CREDENTIAL_ENV):
  // neither may reach the enqueue child. Coordination creds (AGENT_REDIS_*) stay
  // so the child can still LPUSH.
  delete enqueueEnv.ARB_MEMORY_REDIS_URL;
  delete enqueueEnv.ARB_AUDIT_REDIS_URL;

  let outFd: number | undefined;
  let errFd: number | undefined;
  let child: ChildProcessWithoutNullStreams;
  try {
    outFd = openSync(stdoutPath, "a");
    errFd = openSync(stderrPath, "a");
    child = spawn("bash", args, {
      cwd: bridgeRoot,
      env: enqueueEnv,
      stdio: ["ignore", outFd, errFd],
    }) as ChildProcessWithoutNullStreams;
  } catch (err) {
    if (outFd !== undefined) try { closeSync(outFd); } catch { /* ignore */ }
    if (errFd !== undefined) try { closeSync(errFd); } catch { /* ignore */ }
    throw err;
  }
  if (outFd !== undefined) closeSync(outFd);
  if (errFd !== undefined) closeSync(errFd);

  const job: Job = {
    id,
    runId,
    targetId: params.targetId,
    engine,
    task: params.task,
    pid: child.pid,
    state: "running",
    startedAt: Date.now(),
    stdoutPath,
    stderrPath,
    envFile,
    redisPrefix: redisPrefix(envFromFile),
    process: child,
    processAlive: true,
    autoSynthesize: params.autoSynthesize,
    expectedTargets: params.expectedTargets,
    synthesisPrompt: params.synthesisPrompt,
    synthesisOutputPath: params.synthesisOutputPath,
  };
  jobs.set(id, job);
  ensurePoller(ctx);
  registerAutoSynthesis(params, job, ctx);
  ctx?.ui.notify(`ARB dispatch started: ${params.targetId} pid=${child.pid}`, "info");
  updateStatus(ctx);

  child.on("exit", (code, signal) => {
    job.exitCode = code;
    job.signal = signal;
    job.finishedAt = Date.now();
    job.taskId = parseTaskId(stderrPath);
    if (signal) job.state = signal === "SIGTERM" || signal === "SIGKILL" ? "killed" : "failed";
    else if (code === 0) job.state = "completed";
    else if (code === 124) job.state = "timeout";
    else job.state = "failed";
    job.processAlive = false;
    job.process = undefined;

    const ui = lastCtx?.ui ?? ctx?.ui;
    const level = job.state === "completed" ? "info" : job.state === "timeout" ? "warning" : "error";
    ui?.notify(`ARB dispatch ${job.state}: ${job.targetId}${job.taskId ? ` task=${job.taskId}` : ""}`, level as any);
    void tryRefreshVoteFromStdout(job);
    updateStatus(lastCtx ?? ctx);
    maybeAutoSynthesize(job.runId, lastCtx ?? ctx);
    void pollOnce(lastCtx ?? ctx).then(() => updateWatchWidget(lastCtx ?? ctx));
  });

  child.on("error", (err) => {
    job.state = "failed";
    job.finishedAt = Date.now();
    job.processAlive = false;
    job.process = undefined;
    lastCtx?.ui.notify(`ARB dispatch spawn failed: ${params.targetId}: ${err.message}`, "error");
    updateStatus(lastCtx ?? ctx);
    maybeAutoSynthesize(job.runId, lastCtx ?? ctx);
    void pollOnce(lastCtx ?? ctx).then(() => updateWatchWidget(lastCtx ?? ctx));
  });

  return job;
};

const arbDispatchTool = {
  name: "arb_dispatch",
  label: "ARB Dispatch",
  description: "Start an AgentRedisBridge agent-dispatch in the background and notify when it exits. Use this instead of nohup when orchestrating ARB from pi.",
  parameters: {
    type: "object",
    properties: {
      targetId: { type: "string", description: "Target bridge agent id, e.g. codex-example-app-dev" },
      task: { type: "string", description: "Task body. Prefer: Read /path/to/brief.md and execute it." },
      runId: { type: "string", description: "Shared run id for a panel" },
      engine: { type: "string", description: "Dispatcher engine hint; default codex" },
      timeout: { type: "number", description: "agent-dispatch timeout seconds; default 5400" },
      bridgeRoot: { type: "string", description: "AgentRedisBridge clone path; default /home/<user>/AgentRedisBridge" },
      envFile: { type: "string", description: "AGENT_ENV_FILE path" },
      fromAgentId: { type: "string", description: "FROM_AGENT_ID sender trusted by the target" },
      branch: { type: "string", description: "Envelope branch label" },
      cwd: { type: "string", description: "Project cwd for default env/log paths" },
      logDir: { type: "string", description: "Directory for stdout/stderr logs" },
      auditPanel: { type: "boolean", description: "Pass --audit-panel; default true" },
      freshContext: { type: "boolean", description: "Pass --fresh-context; default true" },
      worktree: { type: "string", description: "Pass --worktree <name>: run the task in an isolated git worktree (hard isolation — the engine cwd IS the worktree). Prefer for file-mutating or concurrent-review dispatches." },
      worktreeBase: { type: "string", description: "Pass --worktree-base <ref>; default HEAD" },
      worktreeCleanup: { type: "string", enum: ["keep", "auto"], description: "Pass --worktree-cleanup; default keep (reports survive to be collected)" },
      autoSynthesize: { type: "boolean", description: "Opt in to queue a follow-up synthesis prompt when all expectedTargets for this run are terminal" },
      expectedTargets: { type: "array", items: { type: "string" }, description: "Full roster of target ids expected for auto-synthesis barrier" },
      synthesisPrompt: { type: "string", description: "Follow-up prompt to queue when the auto-synthesis barrier fires" },
      synthesisOutputPath: { type: "string", description: "Optional path the synthesis prompt should ask the model to write" },
      deadlineMinutes: { type: "number", description: "Auto-synthesis barrier deadline: if all expected seats are not terminal by then, queue a vote-gap follow-up (named absent seats + re-fire instructions) instead of waiting forever" },
    },
    required: ["targetId", "task"],
    additionalProperties: false,
  },
  async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
    const job = launchDispatch(params as DispatchParams, ctx);
    return {
      content: [{ type: "text", text: summarizeJob(job) }],
      details: { ...job, process: undefined },
    };
  },
};

const listJobs = () => [...jobs.values()].sort((a, b) => a.startedAt - b.startedAt);

const collect = (limit = 4000): string => {
  const parts: string[] = [];
  for (const job of listJobs()) {
    job.taskId = job.taskId || parseTaskId(job.stderrPath);
    parts.push(`## ${summarizeJob(job)}`);
    if (existsSync(job.stdoutPath)) {
      const out = readFileSync(job.stdoutPath, "utf8");
      parts.push(`stdout (${out.length} bytes):\n${out.slice(0, limit)}`);
    }
    if (existsSync(job.stderrPath)) {
      const err = readFileSync(job.stderrPath, "utf8");
      parts.push(`stderr (${err.length} bytes):\n${err.slice(-Math.min(limit, err.length))}`);
    }
  }
  return parts.join("\n\n");
};

const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");
const fit = (s: string, width: number) => {
  const plain = stripAnsi(s);
  if (plain.length <= width) return s;
  return plain.slice(0, Math.max(0, width - 1)) + "…";
};

const adoptJob = (params: { taskId: string; targetId?: string; runId?: string; envFile?: string; cwd?: string; stdoutPath?: string; stderrPath?: string }, ctx?: ExtensionContext | ExtensionCommandContext): Job => {
  const cwd = resolve(params.cwd || ctx?.cwd || process.cwd());
  const envFile = resolve(params.envFile || defaultEnvFile(cwd));
  const envFromFile = readEnvFile(envFile);
  const taskId = params.taskId.trim();
  const existing = [...jobs.values()].find((j) => j.taskId === taskId);
  if (existing) return existing;
  const id = `${nowStamp()}-adopted-${(params.targetId || taskId).replace(/[^a-zA-Z0-9_.-]/g, "_")}-${randomUUID().slice(0, 8)}`;
  const logDir = join(cwd, ".arb/logs");
  const job: Job = {
    id,
    runId: params.runId || "adopted",
    targetId: params.targetId || "adopted-arb-seat",
    engine: "adopted",
    task: "adopted existing ARB task",
    state: "running",
    startedAt: Date.now(),
    stdoutPath: params.stdoutPath || join(logDir, `arb-adopted-${taskId}.out`),
    stderrPath: params.stderrPath || join(logDir, `arb-adopted-${taskId}.err`),
    envFile,
    redisPrefix: redisPrefix(envFromFile),
    taskId,
  };
  jobs.set(id, job);
  if (!isTerminalState(job.state)) ensurePoller(ctx);
  updateStatus(ctx);
  void pollOnce(ctx).then(() => updateWatchWidget(ctx));
  return job;
};

const inferTargetFromLogName = (name: string): string | undefined => {
  let m = name.match(/^arb-dispatch-\d+T\d+Z-(.+)-[0-9a-f]{8}\.(?:out|err)$/);
  if (m) return m[1];
  m = name.match(/^(?:patch-review|review)-(.+)\.(?:out|err)$/);
  if (m) return m[1];
  return undefined;
};

const adoptRunFromLogs = (runId: string, logDir: string, envFile: string, ctx?: ExtensionContext | ExtensionCommandContext): Job[] => {
  const adopted: Job[] = [];
  if (!existsSync(logDir)) return adopted;
  for (const name of readdirSync(logDir)) {
    if (!name.endsWith(".err")) continue;
    const errPath = join(logDir, name);
    let text = "";
    try { text = readFileSync(errPath, "utf8"); } catch { continue; }
    if (!text.includes(runId)) continue;
    const taskId = parseTaskId(errPath);
    if (!taskId) continue;
    const base = name.slice(0, -4);
    const outPath = join(logDir, `${base}.out`);
    adopted.push(adoptJob({
      taskId,
      targetId: inferTargetFromLogName(name),
      runId,
      envFile,
      cwd: dirnameCompat(logDir),
      stdoutPath: existsSync(outPath) ? outPath : undefined,
      stderrPath: errPath,
    }, ctx));
  }
  return adopted;
};

const dirnameCompat = (p: string) => p.replace(/\/+$/, "").replace(/\/[^/]*$/, "") || "/";

export default function (pi: ExtensionAPI) {
  queueUserMessage = (content, options) => pi.sendUserMessage(content, options);
  pi.registerTool(arbDispatchTool);

  pi.on("session_start", (_event, ctx) => {
    lastCtx = ctx;
    updateStatus(ctx);
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    // Do not kill running dispatchers: they are the durable waiters. Just drop UI state.
    cleanupRuntime();
    ctx.ui.setWidget("arb-watch", undefined);
    ctx.ui.setStatus("arb", undefined);
  });

  pi.registerCommand("arb-adopt", {
    description: "Adopt an existing ARB Redis task after /reload: /arb-adopt <task-id> [target-id]",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      const [taskId, targetId] = (args || "").trim().split(/\s+/).filter(Boolean);
      if (!taskId) {
        ctx.ui.notify("Usage: /arb-adopt <task-id> [target-id]", "error");
        return;
      }
      const job = adoptJob({ taskId, targetId, cwd: ctx.cwd }, ctx);
      ctx.ui.notify(`Adopted ARB task ${taskId}${targetId ? ` (${targetId})` : ""}`, "info");
      if (watchEnabled) refreshWatch(ctx);
      else updateStatus(ctx);
    },
  });

  pi.registerCommand("arb-adopt-run", {
    description: "Adopt ARB jobs for a run id by scanning .arb/logs err files: /arb-adopt-run <run-id> [log-dir]",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      const [runId, logDirArg] = (args || "").trim().split(/\s+/).filter(Boolean);
      if (!runId) {
        ctx.ui.notify("Usage: /arb-adopt-run <run-id> [log-dir]", "error");
        return;
      }
      const logDir = resolve(logDirArg || join(ctx.cwd, ".arb/logs"));
      const envFile = defaultEnvFile(ctx.cwd);
      const adopted = adoptRunFromLogs(runId, logDir, envFile, ctx);
      ctx.ui.notify(`Adopted ${adopted.length} ARB task(s) for ${runId}`, adopted.length ? "info" : "warning");
      checkAutoSynthesisRuns(ctx);
      if (watchEnabled) refreshWatch(ctx);
      else updateStatus(ctx);
    },
  });

  pi.registerCommand("arb-auto-synthesize", {
    description: "Arm auto-synthesis for a run: /arb-auto-synthesize {runId, expectedTargets, synthesisPrompt?, synthesisOutputPath?, deadlineMinutes?}",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      try {
        const params = JSON.parse(args || "{}") as { runId?: string; expectedTargets?: string[]; synthesisPrompt?: string; synthesisOutputPath?: string; deadlineMinutes?: number };
        const runId = params.runId?.trim();
        const expectedTargets = [...new Set((params.expectedTargets || []).map((t) => t.trim()).filter(Boolean))];
        if (!runId || expectedTargets.length === 0) throw new Error("runId and non-empty expectedTargets are required");
        const existing = autoSynthesisRuns.get(runId);
        const createdAt = existing?.createdAt ?? Date.now();
        autoSynthesisRuns.set(runId, {
          runId,
          expectedTargets: existing ? [...new Set([...existing.expectedTargets, ...expectedTargets])] : expectedTargets,
          synthesisPrompt: params.synthesisPrompt || existing?.synthesisPrompt,
          synthesisOutputPath: params.synthesisOutputPath || existing?.synthesisOutputPath,
          fired: existing?.fired ?? false,
          createdAt,
          deadlineAtMs: params.deadlineMinutes !== undefined ? createdAt + params.deadlineMinutes * 60_000 : existing?.deadlineAtMs,
        });
        if (autoSynthesisRuns.get(runId)?.deadlineAtMs !== undefined) ensurePoller(ctx);
        ctx.ui.notify(`ARB auto-synthesis armed for ${runId} (${expectedTargets.length} expected target(s))`, "info");
        maybeAutoSynthesize(runId, ctx);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        ctx.ui.notify(`Usage: /arb-auto-synthesize {"runId":"panel-...","expectedTargets":["codex-..."],"synthesisPrompt":"..."}\nError: ${message}`, "error");
      }
    },
  });

  const notifyDebugConsoleDemoted = (ctx: ExtensionCommandContext) => {
    ctx.ui.notify("/arb-console is debug-only in this Phase 1 build. Use /arb-watch for the native compact cached widget, /arb-status for a one-shot snapshot, or /arb-collect for explicit logs.", "warning");
  };

  pi.registerCommand("arb-console", {
    description: "Debug-only placeholder; use /arb-watch for native compact ARB visibility",
    handler: async (_args, ctx) => notifyDebugConsoleDemoted(ctx),
  });

  pi.registerCommand("arb-debug-console", {
    description: "Debug-only placeholder; disabled until it renders from cached async state",
    handler: async (_args, ctx) => notifyDebugConsoleDemoted(ctx),
  });

  pi.registerCommand("arb-watch", {
    description: "Toggle compact below-editor ARB watch widget for cached Redis/log status",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      const filter = (args || "").trim();
      if (filter && filter !== "all") watchFilterRunId = filter;
      if (filter === "all") watchFilterRunId = undefined;
      if (watchEnabled && !filter) {
        watchEnabled = false;
        ctx.ui.setWidget("arb-watch", undefined);
        reconcilePoller(ctx);
        updateStatus(ctx);
        ctx.ui.notify("ARB watch hidden", "info");
        return;
      }
      watchEnabled = true;
      ensurePoller(ctx);
      await pollOnce(ctx);
      updateWatchWidget(ctx);
      ctx.ui.notify("ARB watch visible (cached Redis/log status; no model context streaming)", "info");
    },
  });

  pi.registerCommand("arb-hide", {
    description: "Hide the live ARB Redis status/events widget",
    handler: async (_args, ctx) => {
      watchEnabled = false;
      ctx.ui.setWidget("arb-watch", undefined);
      reconcilePoller(ctx);
      updateStatus(ctx);
      ctx.ui.notify("ARB watch hidden", "info");
    },
  });

  pi.registerCommand("arb-status", {
    description: "Show ARB background dispatch jobs started by this pi session",
    handler: async (_args, ctx) => {
      lastCtx = ctx;
      await pollOnce(ctx);
      const lines = listJobs().map((job) => {
        const suffix = job.rawState ? `\n  redis: state=${job.rawState} phase=${job.phase || ""} ok=${job.ok === undefined ? "" : String(job.ok)} summary=${(job.summary || "").slice(0, 140)}` : "";
        return summarizeJob(job) + suffix;
      });
      ctx.ui.notify(lines.length ? lines.join("\n") : "No ARB dispatch jobs in this session", "info");
      updateStatus(ctx);
    },
  });

  pi.registerCommand("arb-collect", {
    description: "Collect stdout/stderr snippets from ARB dispatch jobs started by this session",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      const n = Number(args?.trim() || "4000");
      ctx.ui.notify(collect(Number.isFinite(n) ? n : 4000), "info");
    },
  });

  pi.registerCommand("arb-dispatch", {
    description: "Start ARB dispatch from JSON args: {targetId, task, runId?, envFile?, fromAgentId?, branch?}",
    handler: async (args, ctx) => {
      lastCtx = ctx;
      try {
        const params = JSON.parse(args || "{}") as DispatchParams;
        if (!params.targetId || !params.task) throw new Error("targetId and task are required");
        const job = launchDispatch(params, ctx);
        ctx.ui.notify(summarizeJob(job), "info");
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        ctx.ui.notify(`Usage: /arb-dispatch {"targetId":"codex-proj-dev","task":"Read /path/brief.md and execute it"}\nError: ${message}`, "error");
      }
    },
  });
}

export const __test = {
  short,
  stripAnsi,
  fit,
  STATUS_FIELDS,
  parseRedisOk,
  normalizeStatus,
  parseVoteFromDispatcherStdoutText,
  parseVoteFenceFromText,
  decodeJsonStringEscapes,
  readTextFileCapped,
  readTextFileTail,
  tryRefreshVoteFromStdout,
  countJobStates,
  buildDefaultSynthesisPrompt,
  buildDeadlinePrompt,
  checkAutoSynthesisRunsForTests: (nowMs: number) => checkAutoSynthesisRuns(undefined, nowMs),
  execFileText,
  createLimit,
  redisStatusJsonArgs,
  readTaskStatus,
  readSeatHeartbeatTtl,
  pollJobsForTests,
  cleanupForTests: cleanupRuntime,
  STALE_GRACE_MS,
  PROCESS_STALE_GRACE_MS,
  classifyJob,
  applyStatusToJob,
  renderSnapshotLines,
  isPollerActiveForTests: () => Boolean(watchTimer),
  listJobStatesForTests: () => listJobs().map((job) => ({ id: job.id, runId: job.runId, targetId: job.targetId, state: job.state })),
};
