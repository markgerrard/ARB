// pi-sdk-host: long-lived JSON-RPC-over-stdio service that drives
// @earendil-works/pi-coding-agent via its TypeScript SDK rather than via
// `pi --mode rpc`. Sister to `pi_rpc` engine; the bridge wraps this process
// from src/agent_redis_bridge/engines/pi_sdk.py.
//
// Why this exists: the SDK gives us typed events and clean lifecycle hooks,
// so the Python engine can drop ~280 LOC of NDJSON parsing, ANSI prelude
// stripping, camelCase/snake_case tool-event dedup, prompt-ack watchdog,
// and the `get_last_assistant_text` heuristic that pi_rpc needed.
//
// Protocol (line-delimited JSON, one object per line):
//   client → server (requests, with numeric id):
//     {id, method: "initialize", params: {clientInfo, capabilities}}
//     {id, method: "thread/start", params: {cwd, model: {provider, id} | string,
//                                            tools?: string[], thinkingLevel?,
//                                            appendSystemPrompt?: string}}
//     {id, method: "turn/start", params: {threadId, message}}
//     {id, method: "turn/abort", params: {threadId}}
//     {id, method: "shutdown", params: {}}
//   server → client (responses): {id, result | error: {code, message}}
//   server → client (notifications, no id) during a turn:
//     {method: "turn/started",        params: {turnId}}
//     {method: "turn/textDelta",      params: {turnId, delta}}    // live streaming
//     {method: "turn/thinkingDelta",  params: {turnId, delta}}
//     {method: "turn/toolStarted",    params: {turnId, toolCallId, toolName, args}}
//     {method: "turn/toolFinished",   params: {turnId, toolCallId, toolName, result, isError}}
//     {method: "turn/completed",      params: {turnId, ok, finalText, toolCalls,
//                                              stopReason, error?}}
//
// PROTOCOL CONTRACT (load-bearing — Phase 2 Python engine relies on these):
// - Only one turn may be in-flight at a time per thread. Concurrent turn/start
//   returns JSON-RPC error code -32000 "turn already in progress".
// - After sending turn/abort, the client MUST wait for the turn/completed
//   notification (not just the abort response) before issuing a new turn/start.
//   `session.abort()` resolves when the abort signal has been delivered, not
//   when the in-flight prompt() has settled.
// - finalText in turn/completed is HARVESTED from agent_end.messages (last
//   assistant message's TextContent blocks concatenated). It is NOT inferred
//   from the streamed text deltas, so it includes pre-tool text segments and
//   is the canonical final answer per pi-ai's StopReason semantics.
// - "jsonrpc": "2.0" is intentionally OMITTED to match engines/codex.py's
//   wire shape. Strict JSON-RPC clients will not work; the bridge does not
//   validate the discriminator.
//
// Replay log (debuggability parity with pi --mode rpc's grep-able NDJSON):
// - When BRIDGE_PI_SDK_EVENT_LOG=<path> is set, host.mjs appends one
//   NDJSON line per raw AgentSessionEvent it receives (BEFORE protocol
//   mapping). Each line: {ts, threadId, turnId?, event}. File open
//   failures log to stderr and continue without the log — never block
//   real work on debug-log availability. Off by default; per-seat env
//   files opt in.

import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import { createWriteStream, existsSync, statSync } from "node:fs";
import { pathToFileURL } from "node:url";

import {
  DefaultResourceLoader,
  ModelRegistry,
  ModelRuntime,
  SessionManager,
  createAgentSession,
  createBashToolDefinition,
  createEditToolDefinition,
  createFindToolDefinition,
  createGrepToolDefinition,
  createWriteToolDefinition,
  getAgentDir,
} from "@earendil-works/pi-coding-agent";
// NOTE: pi-ai >=0.80 removed the getModel() export; ModelRegistry.find()
// (which also resolves ~/.pi/agent/models.json custom models) is the sole
// resolution path now.
import { startMcpBridge } from "./mcp-bridge.mjs";
import { makeGuardedFsTools } from "./tool-guard.mjs";

// JSON-RPC error codes. -32000..-32099 is the "server error" reserved range
// per the JSON-RPC 2.0 spec; the bridge inspects these so map them to stable
// values rather than ad-hoc numbers.
const ERR_PARSE = -32700;
const ERR_INVALID_REQUEST = -32600;
const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_PARAMS = -32602;
const ERR_INTERNAL = -32603;
const ERR_TURN_IN_PROGRESS = -32000;
const ERR_NO_THREAD = -32001;
const ERR_MODEL_NOT_FOUND = -32002;
const ERR_BAD_STATE = -32003;

const SDK_VERSION = "0.1.0";

// Single-thread-per-process state. The bridge dedicates one harness
// subprocess per engine-pool slot, so multi-thread support isn't needed.
function createState() {
  return {
    initialized: false,
    thread: null, // {id, session, model, eventHandler}
    threadInitInFlight: false,
    rotateInFlight: false,
    activeTurn: null, // {id, finalTextChunks, toolCallCount, aborted, doneEvent, promptPromise}
    shuttingDown: false,
    // Set only when a thread opts into approvals via `thread/start`. Stays
    // null for the cold pi-sdk seat path, which never sends that param — so
    // no tool is wrapped and no tool/approve reaches the wire.
    approvals: null,
  };
}

const standaloneContext = { state: createState(), replies: null, deps: {} };

// Optional NDJSON replay log. Gives parity with `pi --mode rpc`'s grep-able
// wire — one event per line, in the order the SDK fired it, before any
// routing decision in this harness. Operators tail this for live trace or
// grep it post-mortem; it is the primary debuggability surface for pi-sdk
// (the parallel to NDJSON-on-stdout that pi-rpc engines drop directly into
// the bridge log).
export function resolveEventLogPath(pathTemplate, pid = process.pid) {
  if (!pathTemplate) return pathTemplate;
  return String(pathTemplate).replaceAll("{pid}", String(pid));
}

const eventLog = (() => {
  const path = resolveEventLogPath(process.env.BRIDGE_PI_SDK_EVENT_LOG);
  if (!path) return null;
  try {
    const stream = createWriteStream(path, { flags: "a" });
    stream.on("error", (err) => {
      // Treat the log as best-effort. A disk-full or rotated-out file
      // must NOT take the harness down — log to stderr and forget.
      process.stderr.write(
        `[pi-sdk] event_log write failed: ${String(err?.message || err)}\n`,
      );
    });
    return stream;
  } catch (err) {
    process.stderr.write(
      `[pi-sdk] event_log open failed (${path}): ${String(err?.message || err)}\n`,
    );
    return null;
  }
})();

function recordEvent(event, context = standaloneContext) {
  if (eventLog === null) return;
  const entry = {
    ts: new Date().toISOString(),
    threadId: context.state.thread?.id ?? null,
    turnId: context.state.activeTurn?.id ?? null,
    event,
  };
  // Best-effort serialize: a circular/oversized payload should not kill
  // the harness. If JSON.stringify throws, write a minimal record so the
  // sequence stays intact.
  let line;
  try {
    line = JSON.stringify(entry) + "\n";
  } catch (err) {
    line = JSON.stringify({
      ts: entry.ts,
      threadId: entry.threadId,
      turnId: entry.turnId,
      event: { type: event?.type ?? "<unknown>", error: "serialize_failed" },
    }) + "\n";
  }
  eventLog.write(line);
}

// Direct line writes. process.stdout.write is atomic at the JS level on a
// single-threaded event loop; the previous queue layer was redundant for
// ordering (per agy P2.1 / opus P2-1 reviewers). We still need a flush
// helper for the shutdown drain path — see `flushStdout()`.
function writeLine(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function flushStdout() {
  // Drains the stdout write buffer before process.exit so the shutdown reply
  // / final turn/completed isn't truncated when the parent reads from a pipe.
  // Resolves when stdout reports 'drain' (or immediately if buffer is empty).
  return new Promise((resolve) => {
    if (process.stdout.writableLength === 0) {
      resolve();
      return;
    }
    process.stdout.once("drain", resolve);
  });
}

function logStderr(msg, extras = {}) {
  process.stderr.write(
    `[pi-sdk] ${msg}` +
      (Object.keys(extras).length ? " " + JSON.stringify(extras) : "") +
      "\n",
  );
}

function reply(id, result, context = standaloneContext) {
  // Reply MUST be written before any subsequent notification under the same
  // method/turn, since clients match responses by id. Both go through the
  // same single-threaded process.stdout.write so order on the wire matches
  // call order in this file.
  const payload = { id, result };
  if (context.replies !== null) context.replies.push(payload);
  else writeLine(payload);
}

function replyError(id, code, message, data, context = standaloneContext) {
  const err = { code, message };
  if (data !== undefined) err.data = data;
  const payload = { id, error: err };
  if (context.replies !== null) context.replies.push(payload);
  else writeLine(payload);
}

function notify(method, params, _context = standaloneContext) {
  writeLine({ method, params });
}

function jsonSafe(value) {
  if (value === undefined) return null;
  try {
    return JSON.parse(
      JSON.stringify(value, (_key, inner) =>
        typeof inner === "bigint" ? inner.toString() : inner,
      ),
    );
  } catch (_err) {
    return String(value);
  }
}

function resolveModelSpec(params) {
  // Accept either {provider, id} or a "provider/id" string. The string form
  // (e.g. "openrouter/qwen/qwen3-coder-next" from the bridge's AGENT_MODEL
  // env) splits on the FIRST slash so model ids with embedded slashes stay
  // intact (provider="openrouter", id="qwen/qwen3-coder-next").
  const m = params?.model;
  if (typeof m === "string") {
    const slash = m.indexOf("/");
    if (slash <= 0) throw paramError("model string must be 'provider/id'");
    return { provider: m.slice(0, slash), id: m.slice(slash + 1) };
  }
  if (m && typeof m === "object" && typeof m.provider === "string" && typeof m.id === "string") {
    return { provider: m.provider, id: m.id };
  }
  throw paramError("model must be 'provider/id' string or {provider, id}");
}

function paramError(msg) {
  const e = new Error(msg);
  e.jsonRpcCode = ERR_INVALID_PARAMS;
  return e;
}

async function handleInitialize(id, context = standaloneContext) {
  if (context.state.initialized) {
    return replyError(id, ERR_BAD_STATE, "already initialized", undefined, context);
  }
  context.state.initialized = true;
  reply(id, {
    serverInfo: { name: "pi-sdk-host", version: SDK_VERSION },
    capabilities: {},
  }, context);
}

async function handleThreadStart(id, params, context = standaloneContext) {
  // Synchronous gate covers the race where two thread/start lines arrive
  // back-to-back and both pass the `state.thread === null` check before
  // either await resolves. Set the flag BEFORE the first await so a second
  // entrant sees it (opus P1-4).
  if (context.state.rotateInFlight) {
    return replyError(id, ERR_BAD_STATE, "thread rotate in progress", undefined, context);
  }
  if (context.state.thread !== null || context.state.threadInitInFlight) {
    return replyError(id, ERR_BAD_STATE, "thread already started", undefined, context);
  }
  context.state.threadInitInFlight = true;
  try {
    await doThreadStart(id, params, context);
  } finally {
    context.state.threadInitInFlight = false;
  }
}

// Tools a warm orchestrator must not be able to run unapproved. Read-only
// tools are deliberately absent: gating them would add a round-trip per read
// for no control benefit, and the merge/close gate only cares about actions.
const APPROVAL_GATED_TOOLS = ["bash", "write", "edit"];

export async function buildSessionToolArgs(
  { tools, mcpServers, cwd = process.cwd(), approvals = null },
  startBridge = startMcpBridge,
  {
    createFindTool = createFindToolDefinition,
    createGrepTool = createGrepToolDefinition,
    createBashTool = createBashToolDefinition,
    createWriteTool = createWriteToolDefinition,
    createEditTool = createEditToolDefinition,
  } = {},
) {
  const guardedNames = Array.isArray(tools)
    ? ["find", "grep"].filter((name) => tools.includes(name))
    : [];
  const guardedTools = guardedNames.length > 0
    ? makeGuardedFsTools({ cwd, createFind: createFindTool, createGrep: createGrepTool })
      .filter((tool) => guardedNames.includes(tool.name))
    : [];

  // Opt-in only: `approvals` is null unless thread/start asked for it, which
  // the cold pi-sdk seat engine never does. Gate only tools the thread
  // actually has — gating a tool into existence would ADD capability.
  const gatedFactories = {
    bash: createBashTool,
    write: createWriteTool,
    edit: createEditTool,
  };
  const approvalTools = approvals
    ? APPROVAL_GATED_TOOLS
      .filter((name) => !Array.isArray(tools) || tools.includes(name))
      .map((name) =>
        makeApprovalGatedTool(gatedFactories[name](cwd), { approvals, toolName: name })
      )
    : [];
  guardedTools.push(...approvalTools);

  if (!Array.isArray(mcpServers) || mcpServers.length === 0) {
    return {
      tools,
      customTools: guardedTools,
      bridge: null,
    };
  }
  const bridge = await startBridge(mcpServers);
  let nextTools = tools;
  if (Array.isArray(tools)) {
    const merged = [...tools];
    for (const name of bridge.toolNames) {
      if (!merged.includes(name)) merged.push(name);
    }
    nextTools = merged;
  }
  return {
    tools: nextTools,
    customTools: [...guardedTools, ...bridge.customTools],
    bridge,
  };
}

/**
 * Choose the SessionManager for a thread.
 *
 * The host has always used `SessionManager.inMemory(cwd)`, which is right for
 * the COLD seat engine: a dispatched worker must not accumulate context and
 * should leave nothing on disk. The WARM orchestrator is the opposite polarity
 * — its channel is durable and must be reopened by a later process — so it
 * asks for a persisted session instead. pi's SDK supported this all along
 * (`SessionManager.open` is documented "used for resume and branching"); the
 * host simply never exposed it.
 *
 * inMemory remains the DEFAULT so the cold path is unchanged by this addition.
 * Errors from `open` propagate on purpose: falling back to a fresh session
 * would present a cold session as a warm one, which is invisible from the
 * outside because the turn would simply have no memory.
 *
 * @param {{cwd: string, sessionDir?: string, sessionFile?: string}} options
 * @param {typeof SessionManager} SM injected for fast, offline tests
 */
/**
 * Host -> client approval wire (warm orchestrator only).
 *
 * pi's SDK has no client-consulted approval seam of its own; `tool-guard.mjs`
 * checks workspace containment, which is a different job. A warm orchestrator
 * holds Bash and its merge/close gate IS the control, so it needs to be able
 * to veto a tool call from outside the host.
 *
 * STRICTLY OPT-IN: nothing constructs this unless `thread/start` carried an
 * `approvals` param, which the cold pi-sdk seat engine never sends. With it
 * absent, no tool is wrapped and no `tool/approve` ever reaches the wire.
 *
 * Fail closed. If the deciding party does not answer, the call is REFUSED —
 * a gate that opens when nobody is listening is not a gate.
 */
export function createApprovalChannel({ send, timeoutMs = 30000 }) {
  const pending = new Map();
  let nextId = 1;

  function settle(id, verdict) {
    const entry = pending.get(id);
    if (entry === undefined) return; // unknown or already settled — ignore
    pending.delete(id);
    clearTimeout(entry.timer);
    entry.resolve(verdict);
  }

  return {
    ask({ toolName, args }) {
      const id = nextId++;
      return new Promise((resolve) => {
        const timer = setTimeout(
          () => settle(id, { allow: false, code: "pi-approval-timeout" }),
          timeoutMs,
        );
        // `unref` so a pending approval never holds the process open during
        // shutdown; the timer still fires while the host is alive.
        if (typeof timer.unref === "function") timer.unref();
        pending.set(id, { resolve, timer });
        send({ jsonrpc: "2.0", id, method: "tool/approve", params: { toolName, args } });
      });
    },
    /** Called by dispatch when the client answers. No-op if already settled. */
    resolve(id, verdict) {
      settle(id, verdict ?? { allow: false, code: "pi-approval-malformed" });
    },
    pendingCount() {
      return pending.size;
    },
    has(id) {
      return pending.has(id);
    },
  };
}

/**
 * Wrap a tool so the client must approve each call before it runs.
 *
 * Only an EXPLICIT allow proceeds. A malformed or missing verdict refuses —
 * letting `undefined` reach a truthiness check is how a gate quietly becomes a
 * no-op, and it would be invisible because the tool would simply work.
 *
 * Denial throws, so the refusal surfaces to the model as a failed tool call
 * carrying the gate's own code rather than an anonymous error.
 */
export function makeApprovalGatedTool(innerDef, { approvals, toolName }) {
  const name = toolName ?? innerDef.name;
  return {
    ...innerDef,
    __approvalGated: true,
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const verdict = await approvals.ask({ toolName: name, args: params });
      if (verdict?.allow !== true) {
        const code = verdict?.code ?? "pi-approval-malformed";
        const detail = verdict?.detail ? `: ${verdict.detail}` : "";
        throw new Error(`pi-approval-denied: ${code}${detail}`);
      }
      return innerDef.execute(toolCallId, params, signal, onUpdate, ctx);
    },
  };
}

/**
 * Route a client message that is an ANSWER to a host-originated request.
 *
 * `dispatch` refuses anything without a `method` as ERR_INVALID_REQUEST, and
 * an approval answer is exactly that shape ({id, result}), so without this the
 * answer would be rejected and the tool would block until the fail-closed
 * timeout — indistinguishable from a client that never replied.
 *
 * Returns false for anything it does not own, including ids we never issued,
 * so genuine protocol errors still surface instead of being swallowed here.
 */
export function routeClientResponse(message, approvals) {
  if (!approvals) return false;
  if (typeof message?.method === "string") return false;
  const id = message?.id;
  if (typeof id === "undefined" || !approvals.has(id)) return false;
  approvals.resolve(id, message.result);
  return true;
}

/**
 * Read the optional session-persistence params off a `thread/start` request.
 *
 * Non-strings are DROPPED rather than forwarded: a JSON-RPC peer can send
 * anything, and handing a number to `SessionManager.open` would fail deep in
 * the SDK with a confusing error instead of being ignored here.
 */
export function sessionOptionsFromParams(params, cwd) {
  const options = { cwd };
  if (typeof params?.sessionDir === "string") options.sessionDir = params.sessionDir;
  if (typeof params?.sessionFile === "string") options.sessionFile = params.sessionFile;
  return options;
}

export function buildSessionManager({ cwd, sessionDir, sessionFile }, SM = SessionManager) {
  if (sessionFile) return SM.open(sessionFile, sessionDir, cwd);
  if (sessionDir) return SM.create(cwd, sessionDir);
  return SM.inMemory(cwd);
}

export async function startSessionWithBridge(
  { tools, mcpServers, baseSessionArgs, approvals = null },
  deps = {},
) {
  const startBridge = deps.startBridge ?? startMcpBridge;
  const createSession = deps.createSession ?? createAgentSession;
  const toolArgs = await buildSessionToolArgs(
    { tools, mcpServers, cwd: baseSessionArgs.cwd, approvals },
    startBridge,
    {
      createFindTool: deps.createFindTool ?? createFindToolDefinition,
      createGrepTool: deps.createGrepTool ?? createGrepToolDefinition,
    },
  );
  try {
    const result = await createSession({
      ...baseSessionArgs,
      ...(toolArgs.tools !== undefined ? { tools: toolArgs.tools } : {}),
      ...(toolArgs.customTools.length > 0 ? { customTools: toolArgs.customTools } : {}),
    });
    return {
      session: result.session,
      bridge: toolArgs.bridge,
      effectiveTools: toolArgs.tools,
      toolArgs,
    };
  } catch (err) {
    if (toolArgs.bridge) {
      try {
        await toolArgs.bridge.dispose();
      } catch {}
    }
    throw err;
  }
}

async function doThreadStart(id, params, context = standaloneContext) {
  const state = context.state;
  if (state.rotateInFlight) {
    return replyError(id, ERR_BAD_STATE, "thread rotate in progress", undefined, context);
  }
  let modelSpec;
  try {
    modelSpec = resolveModelSpec(params);
  } catch (err) {
    return replyError(id, err.jsonRpcCode || ERR_INVALID_PARAMS, err.message, undefined, context);
  }

  const cwd = typeof params?.cwd === "string" ? params.cwd : process.cwd();
  if (!existsSync(cwd) || !statSync(cwd).isDirectory()) {
    return replyError(
      id,
      ERR_INVALID_PARAMS,
      `cwd does not exist or is not a directory: ${cwd}`,
      undefined,
      context,
    );
  }

  const tools = Array.isArray(params?.tools)
    ? params.tools.filter((t) => typeof t === "string" && t.length > 0)
    : undefined;
  const mcpServers = Array.isArray(params?.mcpServers) ? params.mcpServers : undefined;
  const thinkingLevel = typeof params?.thinkingLevel === "string" ? params.thinkingLevel : undefined;
  const appendSystemPrompt =
    typeof params?.appendSystemPrompt === "string" && params.appendSystemPrompt.length > 0
      ? params.appendSystemPrompt
      : undefined;

  // pi-coding-agent 0.80.9 stopped re-exporting AuthStorage; ModelRuntime.create()
  // builds the credential store internally (same ~/.pi/agent auth path).
  const modelRuntime = await ModelRuntime.create();
  const modelRegistry = new ModelRegistry(modelRuntime);

  // Use ModelRegistry.find() so custom models from ~/.pi/agent/models.json
  // resolve too (getModel() fallback removed with pi-ai >=0.80).
  let model = modelRegistry.find(modelSpec.provider, modelSpec.id);
  if (!model) {
    return replyError(
      id,
      ERR_MODEL_NOT_FOUND,
      `unknown model: ${modelSpec.provider}/${modelSpec.id}`,
      undefined,
      context,
    );
  }

  // Append a role profile to pi's default system prompt without losing the
  // base prompt or tool descriptions. `systemPromptOverride` would REPLACE
  // (pi SDK's examples/sdk/03-custom-prompt.ts labels it "Option 1: Replace
  // prompt entirely"); we need the "Option 2" hook which extends the
  // append array. Tri-model review (P0 across all three reviewers).
  let resourceLoader;
  if (appendSystemPrompt !== undefined) {
    resourceLoader = new DefaultResourceLoader({
      cwd,
      agentDir: getAgentDir(),
      appendSystemPromptOverride: (base) => [...base, appendSystemPrompt],
    });
    await resourceLoader.reload();
  }

  let session;
  let bridge = null;
  let effectiveTools = tools;
  let toolArgs;
  // Opt-in. Absent this param nothing is wrapped and no tool/approve is ever
  // emitted, so the cold pi-sdk seat path is unchanged.
  const approvals = params?.approvals
    ? createApprovalChannel({
        send: (msg) => writeLine(msg),
        ...(typeof params.approvals.timeoutMs === "number"
          ? { timeoutMs: params.approvals.timeoutMs }
          : {}),
      })
    : null;
  state.approvals = approvals;
  try {
    const result = await startSessionWithBridge({
      tools,
      mcpServers,
      approvals,
      baseSessionArgs: {
        cwd,
        model,
        thinkingLevel,
        // inMemory unless the caller asks to persist — cold seat path
        // unchanged, warm orchestrator opts in (see buildSessionManager).
        sessionManager: buildSessionManager(sessionOptionsFromParams(params, cwd)),
        modelRuntime,
        modelRegistry,
        ...(resourceLoader !== undefined ? { resourceLoader } : {}),
      },
    }, context.deps);
    session = result.session;
    bridge = result.bridge;
    effectiveTools = result.effectiveTools;
    toolArgs = result.toolArgs;
  } catch (err) {
    return replyError(
      id,
      ERR_INTERNAL,
      `createAgentSession failed: ${String(err?.message || err)}`,
      undefined,
      context,
    );
  }

  state.thread = {
    id: "th_" + randomUUID(),
    session,
    model,
    bridge,
    params: { modelSpec, cwd, tools, mcpServers, thinkingLevel, appendSystemPrompt },
    modelRuntime,
    modelRegistry,
    toolArgs,
  };

  logStderr("thread_started", {
    threadId: state.thread.id,
    provider: model.provider,
    modelId: model.id,
    tools: effectiveTools ?? "default",
    thinkingLevel: thinkingLevel ?? "default",
    appendSystemPrompt: appendSystemPrompt !== undefined,
  });

  // sessionFile travels back so a warm caller can persist it against its
  // channel and reopen the SAME session on a later process. Undefined for the
  // cold in-memory path, which has no file to reopen.
  const sessionFile = session?.sessionManager?.getSessionFile?.();
  reply(
    id,
    {
      thread: {
        id: state.thread.id,
        ...(typeof sessionFile === "string" ? { sessionFile } : {}),
      },
    },
    context,
  );
}

async function doThreadRotate(id, params, context = standaloneContext) {
  const state = context.state;
  if (state.thread === null) {
    return replyError(id, ERR_NO_THREAD, "thread not started", undefined, context);
  }
  if (typeof params?.threadId !== "string" || params.threadId !== state.thread.id) {
    return replyError(id, ERR_INVALID_PARAMS, "threadId mismatch", undefined, context);
  }
  if (state.activeTurn !== null) {
    return replyError(id, ERR_BAD_STATE, "turn already in progress", undefined, context);
  }
  if (state.rotateInFlight) {
    return replyError(id, ERR_BAD_STATE, "thread rotate in progress", undefined, context);
  }

  state.rotateInFlight = true;
  const oldThread = state.thread;
  try {
    const { modelSpec, cwd, appendSystemPrompt } = oldThread.params;
    const thinkingLevel = typeof params?.thinkingLevel === "string"
      ? params.thinkingLevel
      : oldThread.params.thinkingLevel;
    const model = oldThread.model || oldThread.modelRegistry.find(modelSpec.provider, modelSpec.id);
    let resourceLoader;
    if (appendSystemPrompt !== undefined) {
      resourceLoader = new DefaultResourceLoader({
        cwd,
        agentDir: getAgentDir(),
        appendSystemPromptOverride: (base) => [...base, appendSystemPrompt],
      });
      await resourceLoader.reload();
    }

    const result = await context.deps.createSession({
      cwd,
      model,
      thinkingLevel,
      sessionManager: SessionManager.inMemory(cwd),
      modelRuntime: oldThread.modelRuntime,
      modelRegistry: oldThread.modelRegistry,
      ...(resourceLoader !== undefined ? { resourceLoader } : {}),
      ...(oldThread.toolArgs.tools !== undefined ? { tools: oldThread.toolArgs.tools } : {}),
      ...(oldThread.toolArgs.customTools.length > 0
        ? { customTools: oldThread.toolArgs.customTools }
        : {}),
    });

    let oldDisposed = true;
    try {
      oldThread.session.dispose();
    } catch (err) {
      oldDisposed = false;
      logStderr("rotate_dispose_failed", { error: String(err?.message || err) });
    }

    state.thread = {
      id: "th_" + randomUUID(),
      session: result.session,
      model,
      bridge: oldThread.bridge,
      params: { ...oldThread.params, thinkingLevel },
      modelRuntime: oldThread.modelRuntime,
      modelRegistry: oldThread.modelRegistry,
      toolArgs: oldThread.toolArgs,
    };
    reply(id, { thread: { id: state.thread.id, params: state.thread.params }, oldDisposed }, context);
  } catch (err) {
    return replyError(
      id,
      ERR_INTERNAL,
      `createAgentSession failed: ${String(err?.message || err)}`,
      undefined,
      context,
    );
  } finally {
    state.rotateInFlight = false;
  }
}

// Per-turn event handler factory. Closing over `turn` here is what protects
// us from late events from a previous turn being misrouted into the next
// turn's notifications: `state.activeTurn` could already be the next turn
// when a delayed tool_execution_end fires, but our handler is bound to its
// own turn object and we unsubscribe on completion (agy P1.1 / opus P1-5).
function copyPresent(event, target, fields) {
  for (const field of fields) {
    if (Object.prototype.hasOwnProperty.call(event, field)) {
      target[field] = event[field];
    }
  }
}

function truncatedErrorMessage(value) {
  return typeof value === "string" ? value.slice(0, 500) : value;
}

export function makeTurnHandler(turn, context = standaloneContext) {
  return (event) => {
    // Record FIRST, before any routing decision. We want the log to
    // reflect what the SDK actually fired, not what we chose to forward.
    // Includes events emitted after turn.completed (late retry teardown,
    // etc.) so post-mortem traces show the full tail.
    recordEvent(event, context);
    if (turn.completed) return;
    switch (event.type) {
      case "message_update": {
        const inner = event.assistantMessageEvent;
        if (!inner || typeof inner !== "object") return;
        if (inner.type === "text_delta" && typeof inner.delta === "string") {
          // Live stream to bridge — this is for progressive UX only.
          // Canonical finalText is harvested from agent_end.messages below.
          // No reset on text_start: pi-ai emits text_start per text-content
          // BLOCK within a single AssistantMessage (types.d.ts:260-273), so
          // a text→tool→text message produces two text_starts and resetting
          // would drop the pre-tool segment.
          turn.finalTextChunks.push(inner.delta);
          notify("turn/textDelta", { turnId: turn.id, delta: inner.delta }, context);
        } else if (inner.type === "thinking_delta" && typeof inner.delta === "string") {
          notify("turn/thinkingDelta", { turnId: turn.id, delta: inner.delta }, context);
        }
        return;
      }
      case "tool_execution_start": {
        const toolName = typeof event.toolName === "string" ? event.toolName : "<unknown>";
        const toolCallId = typeof event.toolCallId === "string" ? event.toolCallId : null;
        const args = jsonSafe(event.args);
        turn.toolCallCount += 1;
        notify("turn/toolStarted", { turnId: turn.id, toolCallId, toolName, args }, context);
        return;
      }
      case "tool_execution_end": {
        const toolName = typeof event.toolName === "string" ? event.toolName : "<unknown>";
        const toolCallId = typeof event.toolCallId === "string" ? event.toolCallId : null;
        const isError = event.isError === true;
        const result = jsonSafe(event.result);
        notify("turn/toolFinished", { turnId: turn.id, toolCallId, toolName, result, isError }, context);
        return;
      }
      case "auto_retry_start":
      case "auto_retry_end": {
        const params = {
          turnId: turn.id,
          phase: event.type === "auto_retry_start" ? "start" : "end",
        };
        copyPresent(event, params, ["attempt", "maxAttempts", "delayMs", "success"]);
        if (Object.prototype.hasOwnProperty.call(event, "errorMessage")) {
          params.errorMessage = truncatedErrorMessage(event.errorMessage);
        }
        notify("turn/autoRetry", params, context);
        return;
      }
      case "compaction_start":
      case "compaction_end": {
        const params = {
          turnId: turn.id,
          phase: event.type === "compaction_start" ? "start" : "end",
        };
        copyPresent(event, params, ["reason", "aborted"]);
        notify("turn/compaction", params, context);
        return;
      }
      case "agent_end": {
        // The agent_end event carries `messages: AgentMessage[]` — the new
        // messages added during this prompt() run. Stash for harvesting in
        // the completion path; AssistantMessage.stopReason will tell us
        // whether to mark ok / aborted / errored definitively, and the
        // TextContent blocks of the last assistant message are the
        // canonical finalText (replaces the brittle delta accumulation).
        turn.agentEndEvent = event;
        return;
      }
      default:
        return;
    }
  };
}

function harvestFinalText(messages) {
  // Concatenate all TextContent blocks of the LAST assistant message in the
  // event.messages array. Matches pi-rpc engine's "last assistant message"
  // semantic; works correctly even for text→tool→text messages where the
  // streamed text_delta sequence emitted two text_starts.
  if (!Array.isArray(messages)) return { text: "", stopReason: null };
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === "assistant" && Array.isArray(m.content)) {
      const parts = [];
      for (const c of m.content) {
        if (c && c.type === "text" && typeof c.text === "string") parts.push(c.text);
      }
      return { text: parts.join(""), stopReason: m.stopReason || null };
    }
  }
  return { text: "", stopReason: null };
}

async function handleTurnStart(id, params, context = standaloneContext) {
  const state = context.state;
  if (state.thread === null) {
    return replyError(id, ERR_NO_THREAD, "thread not started", undefined, context);
  }
  if (state.rotateInFlight) {
    return replyError(id, ERR_BAD_STATE, "thread rotate in progress", undefined, context);
  }
  if (typeof params?.threadId !== "string" || params.threadId !== state.thread.id) {
    return replyError(id, ERR_INVALID_PARAMS, "threadId mismatch", undefined, context);
  }
  if (typeof params?.message !== "string" || params.message.length === 0) {
    return replyError(id, ERR_INVALID_PARAMS, "message must be non-empty string", undefined, context);
  }
  if (state.activeTurn !== null) {
    return replyError(id, ERR_TURN_IN_PROGRESS, "turn already in progress", undefined, context);
  }

  const turnId = "tn_" + randomUUID();
  const turn = {
    id: turnId,
    finalTextChunks: [],
    toolCallCount: 0,
    aborted: false,
    completed: false,
    agentEndEvent: null,
  };
  state.activeTurn = turn;

  // Subscribe a per-turn handler. We unsubscribe at completion to ensure
  // any delayed/late events the SDK fires after agent_end (e.g. retry
  // teardown, telemetry callbacks) cannot land on a *next* turn's notifs.
  const handler = makeTurnHandler(turn, context);
  const unsubscribe = state.thread.session.subscribe(handler);

  // Ack synchronously, then drive the prompt asynchronously. The bridge
  // expects turn/completed notification to mark the actual end, not this
  // response (the response just confirms acceptance + new turnId).
  reply(id, { turn: { id: turnId } }, context);
  notify("turn/started", { turnId }, context);

  turn.promptPromise = state.thread.session
    .prompt(params.message)
    .then(() => completeTurn(turn, unsubscribe, null, context))
    .catch((err) => completeTurn(turn, unsubscribe, err, context));
}

function completeTurn(turn, unsubscribe, err, context = standaloneContext) {
  if (turn.completed) return;
  turn.completed = true;
  try {
    unsubscribe();
  } catch (e) {
    logStderr("unsubscribe_threw", { error: String(e?.message || e) });
  }

  // Prefer the canonical text harvested from agent_end.messages over the
  // streamed deltas. If the agent_end didn't fire (early reject from
  // pi/network/etc.), fall back to whatever the delta stream captured.
  const harvested = turn.agentEndEvent
    ? harvestFinalText(turn.agentEndEvent.messages)
    : { text: "", stopReason: null };
  const finalText = (harvested.text || turn.finalTextChunks.join("")).trim();
  const stopReason = harvested.stopReason;

  // Determine ok / error. Precedence:
  //  1. Explicit user abort (turn.aborted set by turn/abort) → "aborted"
  //  2. AssistantMessage.stopReason "aborted" → "aborted" (SDK-detected)
  //  3. AssistantMessage.stopReason "error" → propagate errorMessage
  //  4. Prompt rejection (err arg) → propagate err message
  //  5. Otherwise ok
  let ok, errorMsg;
  if (turn.aborted) {
    ok = false;
    errorMsg = "aborted";
  } else if (stopReason === "aborted") {
    ok = false;
    errorMsg = "aborted";
  } else if (stopReason === "error") {
    ok = false;
    const lastAssistant = turn.agentEndEvent?.messages
      ?.slice()
      .reverse()
      .find((m) => m && m.role === "assistant");
    errorMsg = lastAssistant?.errorMessage || "model error";
  } else if (err !== null) {
    ok = false;
    errorMsg = String(err?.message || err);
  } else {
    ok = true;
  }

  const params = {
    turnId: turn.id,
    ok,
    finalText,
    toolCalls: turn.toolCallCount,
    stopReason,
  };
  if (!ok) params.error = errorMsg;
  notify("turn/completed", params, context);
  context.state.activeTurn = null;
}

async function handleTurnAbort(id, params, context = standaloneContext) {
  const state = context.state;
  if (state.thread === null) {
    return replyError(id, ERR_NO_THREAD, "thread not started", undefined, context);
  }
  if (typeof params?.threadId !== "string" || params.threadId !== state.thread.id) {
    return replyError(id, ERR_INVALID_PARAMS, "threadId mismatch", undefined, context);
  }
  const turn = state.activeTurn;
  if (turn === null) {
    return reply(id, { aborted: false, reason: "no_active_turn" }, context);
  }
  turn.aborted = true;
  try {
    await state.thread.session.abort();
  } catch (err) {
    logStderr("abort_threw", { error: String(err?.message || err) });
  }
  // NOTE: session.abort() resolves when the abort signal is delivered, NOT
  // when the in-flight prompt() has actually settled. The bridge MUST wait
  // for the subsequent turn/completed notification before issuing a new
  // turn/start (documented in the protocol contract block at the top).
  reply(id, { aborted: true, turnId: turn.id }, context);
}

async function gracefulCleanup(context = standaloneContext) {
  const state = context.state;
  // Best-effort tear-down used by both shutdown and stdin-EOF paths.
  // Aborting an active turn lets completeTurn fire and emit a final
  // turn/completed notification before the harness exits, so the bridge
  // never sees a turn vanish silently mid-stream (codex P1.5).
  if (state.activeTurn !== null && state.thread !== null) {
    try {
      state.activeTurn.aborted = true;
      await state.thread.session.abort();
      // Wait briefly for the prompt() to settle and completeTurn to fire.
      // 2s cap so a misbehaving SDK doesn't hang the harness on shutdown.
      const deadline = Date.now() + 2000;
      while (state.activeTurn !== null && Date.now() < deadline) {
        await new Promise((r) => setImmediate(r));
      }
    } catch (err) {
      logStderr("cleanup_abort_threw", { error: String(err?.message || err) });
    }
  }
  try {
    state.thread?.session.dispose();
  } catch (err) {
    logStderr("cleanup_dispose_threw", { error: String(err?.message || err) });
  }
  try {
    await state.thread?.bridge?.dispose();
  } catch (err) {
    logStderr("cleanup_bridge_dispose_threw", { error: String(err?.message || err) });
  }
  await flushStdout();
}

async function handleShutdown(id, context = standaloneContext) {
  const state = context.state;
  if (state.shuttingDown) {
    return replyError(id, ERR_BAD_STATE, "already shutting down", undefined, context);
  }
  state.shuttingDown = true;
  await gracefulCleanup(context);
  reply(id, { ok: true }, context);
  context.flushReplies?.();
  await flushStdout();
  await closeEventLog();
  process.exit(0);
}

function closeEventLog() {
  if (eventLog === null) return Promise.resolve();
  return new Promise((resolve) => {
    eventLog.end(() => resolve());
    // Hard cap: an open log shouldn't block shutdown beyond 1s.
    setTimeout(resolve, 1000);
  });
}

export function createHost(deps = {}) {
  const context = {
    state: createState(),
    replies: [],
    deps: {
      createSession: deps.createSession ?? createAgentSession,
      startBridge: deps.startBridge ?? startMcpBridge,
      createFindTool: deps.createFindTool ?? createFindToolDefinition,
      createGrepTool: deps.createGrepTool ?? createGrepToolDefinition,
    },
  };
  context.flushReplies = () => {
    const pending = context.replies.splice(0);
    for (const payload of pending) writeLine(payload);
  };
  return {
    state: context.state,
    handlers: {
      threadStart: (id, params) => handleThreadStart(id, params, context),
      threadRotate: (id, params) => doThreadRotate(id, params, context),
      turnStart: (id, params) => handleTurnStart(id, params, context),
      turnAbort: (id, params) => handleTurnAbort(id, params, context),
    },
    replies: context.replies,
    context,
    initialize: (id) => handleInitialize(id, context),
    shutdown: (id) => handleShutdown(id, context),
    flushReplies() {
      context.flushReplies();
    },
  };
}

const host = createHost();

const handlers = {
  initialize: host.initialize,
  "thread/start": async (id, params) => {
    if (!host.state.initialized) {
      return replyError(id, ERR_BAD_STATE, "must initialize first", undefined, {
        state: host.state,
        replies: host.replies,
      });
    }
    return host.handlers.threadStart(id, params);
  },
  "thread/rotate": host.handlers.threadRotate,
  "turn/start": host.handlers.turnStart,
  "turn/abort": host.handlers.turnAbort,
  shutdown: host.shutdown,
};

async function dispatch(message) {
  if (!message || typeof message !== "object") return;
  const { id, method, params } = message;
  // An answer to a host-originated tool/approve arrives as {id, result} with
  // no method, which the check below would reject. Only ever non-null when a
  // thread opted into approvals; the cold seat path never reaches here.
  if (routeClientResponse(message, host.state.approvals ?? null)) return;
  if (typeof method !== "string") {
    if (typeof id !== "undefined") replyError(id, ERR_INVALID_REQUEST, "missing method");
    return;
  }
  const handler = handlers[method];
  if (!handler) {
    if (typeof id !== "undefined") replyError(id, ERR_METHOD_NOT_FOUND, `unknown method: ${method}`);
    return;
  }
  if (typeof id === "undefined") return; // notifications from client: not in protocol
  try {
    await handler(id, params);
    host.flushReplies();
  } catch (err) {
    logStderr("handler_threw", { method, error: String(err?.message || err) });
    replyError(id, ERR_INTERNAL, String(err?.message || err));
    host.flushReplies();
  }
}

async function shutdownFromSignal(reason) {
  // Common path for stdin EOF and SIGTERM. We do NOT reply to anything
  // here — the parent has either closed the pipe or signalled us. We just
  // tear down cleanly so OpenRouter connections and pi internals drop.
  if (host.state.shuttingDown) return;
  host.state.shuttingDown = true;
  logStderr("shutdown_from_signal", { reason });
  await gracefulCleanup(host.context);
  await closeEventLog();
  process.exit(0);
}

function main() {
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", (line) => {
    const trimmed = line.trim();
    if (trimmed.length === 0) return;
    let parsed;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      logStderr("parse_error", { line: trimmed.slice(0, 200) });
      return;
    }
    // Fire-and-forget. Handlers serialise state mutations themselves
    // (thread/start uses threadInitInFlight; turn/start checks
    // state.activeTurn). Pipelined requests run interleaved on the
    // microtask queue, but the gating flags reject the second entrant.
    dispatch(parsed);
  });
  rl.on("close", () => {
    // Parent closed stdin (bridge engine pool tearing the harness down).
    // Route through the same cleanup as shutdown so the session disposes,
    // pending tools abort, and OpenRouter connections close cleanly.
    shutdownFromSignal("stdin_closed").catch((err) => {
      logStderr("stdin_close_cleanup_threw", { error: String(err?.message || err) });
      process.exit(1);
    });
  });
  process.on("SIGTERM", () => {
    // engines/codex.py:stop() sends SIGTERM with a 5s wait before SIGKILL,
    // so we have a budget to dispose. Mirror that behaviour for pi-sdk
    // (agy P2.3 / opus implied).
    shutdownFromSignal("sigterm").catch(() => process.exit(1));
  });
  process.on("uncaughtException", (err) => {
    logStderr("uncaught_exception", { error: String(err?.stack || err) });
    // Hard exit — uncaught means we've already lost integrity. A graceful
    // path would risk recursing.
    process.exit(1);
  });
  process.on("unhandledRejection", (reason) => {
    // Log and continue. Killing the harness on any stray rejection (a
    // pi-internal retry, a telemetry promise, a network blip) drops the
    // in-flight turn/completed and hangs the bridge until its own
    // turn-timeout fires. Node's default is "warn"; do the same.
    logStderr("unhandled_rejection", { reason: String(reason) });
  });
  logStderr("ready", { version: SDK_VERSION });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
