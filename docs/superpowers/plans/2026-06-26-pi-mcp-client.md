# pi-sdk MCP-client support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let pi-sdk engine seats consume stdio MCP servers (first consumer: the existing read-only
`arb-memory-local-mcp`) so pi judgment/reviewer seats can `memory_search/get/recent`.

**Architecture:** A new Node module `tools/pi-sdk-host/mcp-bridge.mjs` spawns each configured MCP server,
discovers its tools via `initialize`+`tools/list`, and wraps each as a pi `ToolDefinition` whose
`execute` forwards to the MCP server. `host.mjs` calls it in `doThreadStart` and registers the tools via
`createAgentSession({ customTools })`. `pi_sdk.py` threads the existing `local_memory_mcp_servers()`
config through `thread/start` as a new `mcpServers` param, attaching a pi-side canonical `name`.

**Tech Stack:** Node ≥20 (`node:test`), `@modelcontextprotocol/sdk` (new dep), `typebox` (new DIRECT
dep — currently only transitively present), Python (pi_sdk.py, pytest), `arb-memory-local-mcp`.

**Spec:** `docs/superpowers/specs/2026-06-26-pi-mcp-client-design.md` (v2, design+spec paneled).

## Global Constraints
- Flag-gated by `ARB_MEMORY_LOCAL_MCP`; OFF ⇒ no `mcpServers` param ⇒ byte-unchanged host behavior.
- `src/agent_redis_bridge/local_memory_mcp.py` is **NOT modified** — the canonical `name="arb-memory-local"`
  is attached pi-side in `pi_sdk.py`. The shared helper's 7 consumers (esp. 5 ACP engines that pass its
  output on the wire) must stay byte-for-byte unchanged; their existing tests must pass with NO edits.
- Integration point is `createAgentSession({ customTools })`, NOT a pi extension.
- Parameters use `Type.Unsafe(mcpTool.inputSchema)` — NO JSON-Schema→TypeBox converter.
- Fail-loud at thread start: any configured server failing spawn/`initialize`/`tools/list` ⇒ throw;
  ALL spawned children reaped (reap-list populated at spawn, before `initialize`).
- Tool names: `mcp__<sanitizedServer>__<sanitizedTool>`, both components sanitized to `[A-Za-z0-9_]`,
  final name matches `^[A-Za-z0-9_-]{1,64}$` (deterministic clamp if >64), no dup, no built-in collision
  (`read,bash,edit,write,grep,find,ls`).
- Custom tools set `promptSnippet` (from MCP description) so the seat is TOLD the tool exists.
- Per-server handshake timeout 15s (≪ pi_sdk.py's 30s `thread/start` budget).

---

### Task 1: Dependency contract (package.json + install.sh + resolve smoke)

**Files:**
- Modify: `tools/pi-sdk-host/package.json`
- Modify: `tools/pi-sdk-host/install.sh`
- Test: `tools/pi-sdk-host/deps.test.mjs`

**Interfaces:**
- Produces: direct, host-resolvable imports `typebox` (→ `Type.Unsafe`) and
  `@modelcontextprotocol/sdk/client/index.js` (→ `Client`) + `/client/stdio.js` (→ `StdioClientTransport`).

- [ ] **Step 1: Confirm the gap (baseline).**
Run: `cd tools/pi-sdk-host && node -e "import('typebox').then(()=>console.log('OK')).catch(e=>console.log('FAIL',e.code))"`
Expected: `FAIL MODULE_NOT_FOUND` (typebox is only nested under `@earendil-works/pi-coding-agent/node_modules`).

- [ ] **Step 2: Write the failing resolve test** — `tools/pi-sdk-host/deps.test.mjs`:
```js
import test from "node:test";
import assert from "node:assert/strict";

test("typebox resolves directly and exposes Type.Unsafe", async () => {
  const { Type } = await import("typebox");
  assert.equal(typeof Type.Unsafe, "function");
});

test("@modelcontextprotocol/sdk client + stdio transport resolve", async () => {
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StdioClientTransport } = await import("@modelcontextprotocol/sdk/client/stdio.js");
  assert.equal(typeof Client, "function");
  assert.equal(typeof StdioClientTransport, "function");
});
```

- [ ] **Step 3: Run it to verify it fails**
Run: `cd tools/pi-sdk-host && node --test deps.test.mjs`
Expected: FAIL (`Cannot find package 'typebox'`).

- [ ] **Step 4: Declare deps.** In `tools/pi-sdk-host/package.json`, add (pin `typebox` to the version
pi bundles — read it from `node_modules/@earendil-works/pi-coding-agent/node_modules/typebox/package.json`
`version` so there is ONE typebox, not two incompatible copies) and a test script:
```json
  "scripts": {
    "install-symlinks": "./install.sh",
    "smoke-sdk": "node smoke.mjs",
    "smoke-protocol": "python3 smoke_protocol.py",
    "test": "node --test"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.20.0",
    "typebox": "1.1.38"
  },
```
(Verify the `@modelcontextprotocol/sdk` version exists; use the latest 1.x that exports
`/client/index.js` + `/client/stdio.js`. Pin `typebox` to the exact bundled version.)

- [ ] **Step 5: Make install.sh install deps — BEFORE creating the symlinks (P0, plan-panel).**
`npm install` PRUNES undeclared entries under `node_modules`, which would DELETE the `@earendil-works/*`
symlinks (`install.sh:34-36`) that the host imports — codex verified this empirically. So the install
MUST run BEFORE the `mkdir -p … ln -sfn` block, not after. Insert, after the pi-ai validation
(`install.sh:~32`) and BEFORE line 34's `mkdir -p`:
```sh
# Install the direct deps declared in package.json (@modelcontextprotocol/sdk + typebox)
# BEFORE creating the @earendil-works symlinks below: `npm install` prunes undeclared
# entries under node_modules, so running it AFTER symlinking would delete those symlinks.
# Running it first means the symlinks are (re)created afterward and survive. Idempotent.
( cd "${HERE}" && npm install --no-audit --no-fund --omit=dev )
```
(Use the script's existing `HERE` variable.)

- [ ] **Step 6: Install + verify host imports survive AND deps resolve (post-install assertion).**
Run: `cd tools/pi-sdk-host && ./install.sh && node --test deps.test.mjs`
Then prove the symlinks survived the install AND the new deps resolve — the real invariant (NOT
`npm ls` tree count, which is misleading with the symlinked nested typebox):
```sh
cd tools/pi-sdk-host && node -e "(async()=>{ \
  await import('@earendil-works/pi-coding-agent'); \
  await import('@earendil-works/pi-ai'); \
  await import('typebox'); \
  await import('@modelcontextprotocol/sdk/client/index.js'); \
  console.log('all 4 imports OK'); })()"
node -p "require('typebox/package.json').version"   # must equal the pinned version
```
Expected: deps.test.mjs PASS (2/2); "all 4 imports OK"; typebox version == pinned. If any
`@earendil-works` import fails, the install order pruned the symlinks — fix the ordering.

- [ ] **Step 7: Commit**
```bash
git add tools/pi-sdk-host/package.json tools/pi-sdk-host/install.sh tools/pi-sdk-host/deps.test.mjs
git commit -m "feat(pi-mcp): declare @modelcontextprotocol/sdk + typebox as direct host deps"
```

---

### Task 2: `mcp-bridge.mjs` adapter — MCP tool → pi ToolDefinition

**Files:**
- Create: `tools/pi-sdk-host/mcp-bridge.mjs`
- Test: `tools/pi-sdk-host/mcp-bridge.adapter.test.mjs`

**Interfaces:**
- Consumes: `typebox` `Type.Unsafe`; pi `defineTool` from `@earendil-works/pi-coding-agent`.
- Produces (exported): `sanitizeComponent(s: string): string`,
  `composeToolName(server: string, tool: string): string` (namespaced, sanitized, ≤64, deterministic
  clamp), `buildToolDefinition(serverName: string, mcpTool: {name,description,inputSchema}, client: {callTool}): ToolDefinition`.
  `buildToolDefinition`'s `execute(toolCallId, params, signal)` calls
  `client.callTool({name: mcpTool.name, arguments: params}, undefined, {signal})` and maps the MCP
  result to a pi `AgentToolResult`.

- [ ] **Step 0: Pin the SDK shapes (read, don't guess).** Read `createReadTool` /
`core/tools/read.d.ts` and `core/extensions/types.d.ts:335-365` in
`tools/pi-sdk-host/node_modules/@earendil-works/pi-coding-agent/dist/` to copy the EXACT `ToolDefinition`
field names and the EXACT `AgentToolResult` shape a built-in tool returns. Match that shape in `execute`
below (the literal field names — e.g. whether result content is `{content:[{type:"text",text}]}` or
similar — must match the framework, not this plan's approximation). Also read the MCP SDK
`client.callTool` return type from `@modelcontextprotocol/sdk/dist/.../types.d.ts` (`CallToolResult`:
`{content: Array<{type:"text",text}|...>, isError?: boolean}`).

- [ ] **Step 1: Write the failing adapter tests** — `tools/pi-sdk-host/mcp-bridge.adapter.test.mjs`:
```js
import test from "node:test";
import assert from "node:assert/strict";
import { sanitizeComponent, composeToolName, buildToolDefinition } from "./mcp-bridge.mjs";

test("sanitizeComponent maps non-alnum to underscore", () => {
  assert.equal(sanitizeComponent("arb-memory-local"), "arb_memory_local");
  assert.equal(sanitizeComponent("mem/search:v2 "), "mem_search_v2_");
});

test("composeToolName namespaces both components and stays <=64", () => {
  assert.equal(composeToolName("arb-memory-local", "memory_recent"),
               "mcp__arb_memory_local__memory_recent");
  const long = composeToolName("a".repeat(40), "b".repeat(40));
  assert.ok(long.length <= 64, `len ${long.length}`);
  assert.match(long, /^[A-Za-z0-9_-]{1,64}$/);
  // deterministic: same inputs => same clamp
  assert.equal(long, composeToolName("a".repeat(40), "b".repeat(40)));
});

test("buildToolDefinition wraps schema with Type.Unsafe, sets promptSnippet, parallel mode", async () => {
  const mcpTool = { name: "memory_recent",
    description: "List recent artefacts.",
    inputSchema: { type: "object", properties: { limit: { type: "integer" } } } };
  let captured;
  const client = { callTool: async (req) => { captured = req; return { content: [{ type: "text", text: "ok" }] }; } };
  const td = buildToolDefinition("arb-memory-local", mcpTool, client);
  assert.equal(td.name, "mcp__arb_memory_local__memory_recent");
  assert.equal(td.promptSnippet, "List recent artefacts.");
  assert.equal(td.executionMode, "parallel");
  // Type.Unsafe wraps the schema and adds `~`-prefixed own keys (~unsafe/~kind) — so compare the
  // JSON-enumerable shape, NOT object identity (plan-panel P1; raw deepEqual fails on a correct impl):
  assert.deepEqual(JSON.parse(JSON.stringify(td.parameters)), mcpTool.inputSchema);
  // execute forwards the ORIGINAL tool name + args to callTool
  const res = await td.execute("call-1", { limit: 3 }, undefined);
  assert.equal(captured.name, "memory_recent");
  assert.deepEqual(captured.arguments, { limit: 3 });
  assert.match(JSON.stringify(res), /ok/); // success path returns the mapped content
});

// CRITICAL (plan-panel P1, verified against pi-agent-core/dist/agent-loop.js:433/440): a RESOLVED
// execute() is recorded isError:false regardless of the returned object; only a THROW marks the tool
// failed. So the executor must THROW on failure — returning {isError:true} would be a silent success.
test("buildToolDefinition: MCP isError -> execute THROWS (not a returned isError)", async () => {
  const client = { callTool: async () => ({ content: [{ type: "text", text: "boom" }], isError: true }) };
  const td = buildToolDefinition("s", { name: "t", description: "d", inputSchema: { type: "object" } }, client);
  await assert.rejects(td.execute("c", {}, undefined), /boom|MCP tool/i);
});

test("buildToolDefinition: callTool rejection (dead server) -> execute THROWS", async () => {
  const client = { callTool: async () => { throw new Error("transport closed"); } };
  const td = buildToolDefinition("s", { name: "t", description: "d", inputSchema: { type: "object" } }, client);
  await assert.rejects(td.execute("c", {}, undefined), /transport closed|MCP tool/i);
});
```

- [ ] **Step 2: Run to verify it fails**
Run: `cd tools/pi-sdk-host && node --test mcp-bridge.adapter.test.mjs`
Expected: FAIL (`Cannot find module ./mcp-bridge.mjs`).

- [ ] **Step 3: Implement the adapter** — create `tools/pi-sdk-host/mcp-bridge.mjs` (match the
`AgentToolResult` field names to what Step 0 found; the mapping below is the shape to align):
```js
import { Type } from "typebox";
import { createHash } from "node:crypto";

const BUILTIN_TOOL_NAMES = new Set(["read", "bash", "edit", "write", "grep", "find", "ls"]);
const MAX_TOOL_NAME = 64;

export function sanitizeComponent(s) {
  return String(s).replace(/[^A-Za-z0-9_]/g, "_");
}

export function composeToolName(server, tool) {
  const full = `mcp__${sanitizeComponent(server)}__${sanitizeComponent(tool)}`;
  if (full.length <= MAX_TOOL_NAME) return full;
  // deterministic clamp: keep a readable head + 8-char stable hash of the full name
  const hash = createHash("sha256").update(full).digest("hex").slice(0, 8);
  return full.slice(0, MAX_TOOL_NAME - 9) + "_" + hash;
}

function blocksToText(callToolResult) {
  const blocks = Array.isArray(callToolResult?.content) ? callToolResult.content : [];
  return blocks
    .map((b) => (b?.type === "text" ? b.text : `[${b?.type ?? "unknown"} block: ${JSON.stringify(b)}]`))
    .join("\n");
}

export function buildToolDefinition(serverName, mcpTool, client) {
  return {
    name: composeToolName(serverName, mcpTool.name),
    label: mcpTool.name,
    description: mcpTool.description ?? mcpTool.name,
    promptSnippet: mcpTool.description ?? mcpTool.name,
    parameters: Type.Unsafe(mcpTool.inputSchema ?? { type: "object" }),
    executionMode: "parallel",
    async execute(_toolCallId, params, signal) {
      let res;
      try {
        res = await client.callTool({ name: mcpTool.name, arguments: params ?? {} }, undefined, { signal });
      } catch (err) {
        // pi only flags a tool error when execute() THROWS (agent-loop.js:440); a returned
        // isError would be treated as success. So rethrow with a clear message.
        throw new Error(`MCP tool ${mcpTool.name} failed: ${String(err?.message || err)}`);
      }
      if (res?.isError === true) {
        throw new Error(`MCP tool ${mcpTool.name} returned error: ${blocksToText(res)}`);
      }
      // SUCCESS: return the mapped content in the framework's AgentToolResult shape (match Step 0).
      return { content: [{ type: "text", text: blocksToText(res) }] };
    },
  };
}
```

- [ ] **Step 4: Run to verify it passes**
Run: `cd tools/pi-sdk-host && node --test mcp-bridge.adapter.test.mjs`
Expected: PASS (all cases).

- [ ] **Step 5: Real-schema pin test (spec test #4)** — append to the same test file. Spawn the REAL
`arb-memory-local-mcp` to capture its `inputSchema`s (do NOT hand-write them), wrap with `Type.Unsafe`,
and assert validation behaves. Use the project venv DSN; if the dev DSN env isn't available in the test
host, read schemas from a committed fixture `tools/pi-sdk-host/fixtures/arb-memory-tools.json` AND
document the refresh command in a comment. Three explicit assertions:
```js
// typebox@1.1.38 exports `Compile` from "typebox/compile" — NOT `TypeCompiler` (that is the old
// @sinclair/typebox@0.x name; codex verified `typebox/compile` exports Compile/Code/Validator).
// Confirm the exact export in Step 0 by reading node_modules/typebox/compile.
import { Compile } from "typebox/compile";
test("real arb-memory schemas: Type.Unsafe compiles + validates (valid true, bad false)", async () => {
  const tools = await loadRealArbMemoryToolsList(); // spawn arb-memory-local-mcp, call tools/list (helper below)
  const search = tools.find((t) => t.name === "memory_search");
  const checker = Compile(Type.Unsafe(search.inputSchema));
  assert.equal(checker.Check({ query: "x", k: 8 }), true);       // (i) valid args
  assert.equal(checker.Check({ query: 123 }), false);            // (ii) WRONG TYPE on required field
});
```
And (iii) drive the wrapped ToolDefinition / pi `validateToolArguments` path with bad args and assert it
yields the error tool-result (proves pi's real throw-on-invalid → error result; raw `Check` only returns
false). Implement `loadRealArbMemoryToolsList()` by spawning `arb-memory-local-mcp` via a minimal MCP
client and calling `listTools()` (you build the client in Task 3; for this test a thin inline spawn is
acceptable, or land this assertion in Task 3 after `startMcpBridge` exists — note the dependency).

- [ ] **Step 6: Run + Commit**
Run: `cd tools/pi-sdk-host && node --test mcp-bridge.adapter.test.mjs` → PASS
```bash
git add tools/pi-sdk-host/mcp-bridge.mjs tools/pi-sdk-host/mcp-bridge.adapter.test.mjs tools/pi-sdk-host/fixtures/ 2>/dev/null
git commit -m "feat(pi-mcp): MCP-tool->pi ToolDefinition adapter (sanitize/clamp, Type.Unsafe, promptSnippet, isError mapping)"
```

---

### Task 3: `mcp-bridge.mjs` — `startMcpBridge` lifecycle (spawn, reap, timeout, dispose)

**Files:**
- Modify: `tools/pi-sdk-host/mcp-bridge.mjs`
- Test: `tools/pi-sdk-host/mcp-bridge.lifecycle.test.mjs`
- Create (test fixture): `tools/pi-sdk-host/fixtures/fake-mcp-server.mjs` (a tiny stdio MCP server)

**Interfaces:**
- Consumes: `@modelcontextprotocol/sdk` `Client`, `StdioClientTransport`; `buildToolDefinition` (Task 2).
- Produces (exported): `startMcpBridge(serverSpecs: Array<{name,command,args?,env?}>):
  Promise<{ customTools: ToolDefinition[], toolNames: string[], dispose(): Promise<void> }>`.
  Throws on any server failure after reaping all spawned children. `dispose()` is idempotent.

- [ ] **Step 1: Build a controllable fake stdio MCP server** — `fixtures/fake-mcp-server.mjs`: a minimal
MCP server (use `@modelcontextprotocol/sdk/server`) that exposes one tool `echo`, and honors env knobs:
`FAKE_FAIL_MODE=handshake` (exit during initialize), `=toolslist` (throw on tools/list), `=die_on_call`
(exit before answering callTool), `=hang` (accept stdio but NEVER answer initialize — to exercise the
handshake-timeout branch), unset = behave normally (echo). **PID contract (plan-panel P1):** on start,
if `process.env.FAKE_PID_FILE` is set, the fixture writes `String(process.pid)` to that path
(`fs.writeFileSync`). Tests pass a unique `FAKE_PID_FILE` per spec and read it back to assert the child
process is gone — this is the executable no-orphan mechanism (the host owns the transport, so the test
cannot see the PID any other way).

- [ ] **Step 2: Write failing lifecycle tests** — `tools/pi-sdk-host/mcp-bridge.lifecycle.test.mjs`:
```js
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startMcpBridge } from "./mcp-bridge.mjs";

const FAKE = new URL("./fixtures/fake-mcp-server.mjs", import.meta.url).pathname;
const TMP = mkdtempSync(join(tmpdir(), "pi-mcp-"));
let n = 0;
// each spec gets a unique PID file so the test can read back the child PID and assert it died
function spec(name, env = {}) {
  const pidFile = join(TMP, `pid-${name}-${n++}`);
  return { spec: { name, command: process.execPath, args: [FAKE], env: { FAKE_PID_FILE: pidFile, ...env } }, pidFile };
}
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const readPid = (f) => Number(readFileSync(f, "utf8").trim());
async function deadWithin(pid, ms = 2000) { // poll for OS reaping
  const end = Date.now() + ms; // NOTE: Date.now() is fine in a Node test (this is not a workflow script)
  while (Date.now() < end) { if (!alive(pid)) return true; await new Promise((r) => setTimeout(r, 25)); }
  return !alive(pid);
}

test("happy path: discovers tools, namespaced names; dispose kills the child", async () => {
  const a = spec("fake");
  const b = await startMcpBridge([a.spec]);
  assert.ok(b.toolNames.includes("mcp__fake__echo"));
  const pid = readPid(a.pidFile);
  assert.equal(alive(pid), true);
  await b.dispose();
  assert.equal(await deadWithin(pid), true);   // dispose() terminates the child (executable assertion)
});

test("transactional: 2nd server fails handshake -> throws AND first child reaped (no orphan)", async () => {
  const ok = spec("ok");
  const bad = spec("bad", { FAKE_FAIL_MODE: "handshake" });
  await assert.rejects(startMcpBridge([ok.spec, bad.spec]), /handshake|timed out|failed|error/i);
  const okPid = readPid(ok.pidFile);
  assert.equal(await deadWithin(okPid), true);  // the already-spawned first child MUST be dead
});

test("dispose is idempotent", async () => {
  const a = spec("fake");
  const b = await startMcpBridge([a.spec]);
  await b.dispose();
  await b.dispose(); // must not throw
});

test("handshake TIMEOUT branch fires (hang server, short injected budget)", async () => {
  const h = spec("slow", { FAKE_FAIL_MODE: "hang" });
  // inject a tiny handshake budget so the setTimeout path — not a prompt rejection — is what rejects
  await assert.rejects(startMcpBridge([h.spec], { handshakeMs: 200 }), /timed out/i);
  const pid = readPid(h.pidFile);
  assert.equal(await deadWithin(pid), true);    // the hung child is reaped on timeout
});

test("mid-turn death: discovered tool execute() THROWS when server dies before answering callTool", async () => {
  const d = spec("dying", { FAKE_FAIL_MODE: "die_on_call" });
  const b = await startMcpBridge([d.spec]);
  const echo = b.customTools.find((t) => t.name === "mcp__dying__echo");
  await assert.rejects(echo.execute("c", { msg: "x" }, undefined), /failed|closed|error/i); // no hang
  await b.dispose();
});
```

- [ ] **Step 3: Run to verify it fails**
Run: `cd tools/pi-sdk-host && node --test mcp-bridge.lifecycle.test.mjs`
Expected: FAIL (`startMcpBridge is not exported`).

- [ ] **Step 4: Implement `startMcpBridge`** — append to `mcp-bridge.mjs`:
```js
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const HANDSHAKE_MS = 15_000;

function withTimeout(promise, ms, message) {
  let t;
  const timeout = new Promise((_, reject) => { t = setTimeout(() => reject(new Error(message)), ms); });
  return Promise.race([promise.finally(() => clearTimeout(t)), timeout]);
}

async function killChild(transport) {
  // SIGTERM -> short grace -> SIGKILL escalation so a stuck/hung child cannot leak (plan-panel P2).
  // StdioClientTransport exposes a `pid` getter and an internal `_process`; confirm in Step 0.
  const proc = transport?._process;
  const pid = transport?.pid;
  if (!proc || proc.exitCode !== null) return;
  try { proc.kill("SIGTERM"); } catch {}
  await new Promise((r) => setTimeout(r, 300));
  if (proc.exitCode === null) { try { proc.kill("SIGKILL"); } catch {} }
}

async function disposeAll(entries) {
  await Promise.allSettled(entries.map(async ({ client, transport }) => {
    try { await client?.close?.(); } catch {}
    try { await transport?.close?.(); } catch {}
    try { await killChild(transport); } catch {}   // guarantee the child is gone
  }));
}

export async function startMcpBridge(serverSpecs, { handshakeMs = HANDSHAKE_MS } = {}) {
  const entries = []; // reap-list: push BEFORE connect so a mid-handshake child is still reaped
  try {
    const customTools = [];
    for (const spec of serverSpecs) {
      const transport = new StdioClientTransport({
        command: spec.command,
        args: spec.args ?? [],
        env: { ...process.env, ...(spec.env ?? {}) }, // MERGE: Node `env` REPLACES otherwise (loses PATH/HOME)
      });
      const client = new Client({ name: "pi-sdk-host", version: "0.1.0" }, { capabilities: {} });
      entries.push({ client, transport });               // <-- reap-list at spawn
      await withTimeout(client.connect(transport), handshakeMs, `server ${spec.name} handshake timed out`);
      const listed = await withTimeout(client.listTools(), handshakeMs, `server ${spec.name} tools/list timed out`);
      for (const t of listed.tools) customTools.push(buildToolDefinition(spec.name, t, client));
    }
    const toolNames = customTools.map((t) => t.name);
    const seen = new Set();
    for (const n of toolNames) {
      if (BUILTIN_TOOL_NAMES.has(n)) throw new Error(`MCP tool name collides with built-in: ${n}`);
      if (seen.has(n)) throw new Error(`duplicate MCP tool name after namespacing: ${n}`);
      seen.add(n);
    }
    let disposed = false;
    const dispose = async () => { if (disposed) return; disposed = true; await disposeAll(entries); };
    return { customTools, toolNames, dispose };
  } catch (err) {
    await disposeAll(entries); // reap every spawned child before propagating
    throw err;
  }
}
```
(`StdioClientTransport.connect` spawns the child; `client.close()` closes the transport and terminates
the child. Confirm the exact close/terminate semantics from the SDK in Step 0; if `close()` does not
guarantee child termination, also call `transport._process?.kill()` with SIGTERM→SIGKILL escalation.)

- [ ] **Step 5: Run to verify it passes**
Run: `cd tools/pi-sdk-host && node --test mcp-bridge.lifecycle.test.mjs`
Expected: PASS, including the no-leaked-child assertion.

- [ ] **Step 6: Land the Task-2 Step-5(iii) real-schema execute assertion here** if it was deferred
(now that `startMcpBridge` can spawn the real server). Run the full host test suite:
Run: `cd tools/pi-sdk-host && node --test` → all PASS.

- [ ] **Step 7: Commit**
```bash
git add tools/pi-sdk-host/mcp-bridge.mjs tools/pi-sdk-host/mcp-bridge.lifecycle.test.mjs tools/pi-sdk-host/fixtures/fake-mcp-server.mjs
git commit -m "feat(pi-mcp): startMcpBridge lifecycle (spawn-time reap-list, transactional dispose, handshake timeout)"
```

---

### Task 4: `host.mjs` `doThreadStart` wiring (mcpServers param, spawn-late, leak-window, allowlist, dispose)

**Files:**
- Modify: `tools/pi-sdk-host/host.mjs` (`doThreadStart` ~244-325; `gracefulCleanup` ~532-557; imports)
- Test: `tools/pi-sdk-host/host.mcp.test.mjs`

**Interfaces:**
- Consumes: `startMcpBridge` (Task 3). Reads new optional `params.mcpServers: Array<{name,command,args,env}>`.
- Produces: `createAgentSession` called with `customTools` + (when a `tools` allowlist is present) the
  appended `toolNames`; bridge handle stored on `state.thread`; disposed on every failure path and in
  `gracefulCleanup`.

- [ ] **Step 1: Write failing tests** — `tools/pi-sdk-host/host.mcp.test.mjs`. Drive `doThreadStart`'s
behavior by importing the wiring helper (extract the mcpServers→customTools+allowlist logic into a small
exported pure function `buildSessionToolArgs({ tools, mcpServers }, startBridge)` so it's unit-testable
without spawning a full session). Tests:
```js
import test from "node:test";
import assert from "node:assert/strict";
import { buildSessionToolArgs, startSessionWithBridge } from "./host.mjs";

const fakeBridge = { customTools: [{ name: "mcp__arb_memory_local__memory_recent" }],
                     toolNames: ["mcp__arb_memory_local__memory_recent"], dispose: async () => {} };
const startOK = async () => fakeBridge;

test("no mcpServers -> no bridge, args unchanged", async () => {
  const r = await buildSessionToolArgs({ tools: ["read"], mcpServers: undefined }, startOK);
  assert.equal(r.bridge, null);
  assert.deepEqual(r.tools, ["read"]);
  assert.deepEqual(r.customTools, []);
});

test("allowlist present -> MCP names appended (deduped, order preserved)", async () => {
  const r = await buildSessionToolArgs({ tools: ["read", "grep"], mcpServers: [{ name: "arb-memory-local" }] }, startOK);
  assert.deepEqual(r.tools, ["read", "grep", "mcp__arb_memory_local__memory_recent"]);
  assert.equal(r.customTools.length, 1);
});

test("no allowlist -> custom tools pass through, tools stays undefined", async () => {
  const r = await buildSessionToolArgs({ tools: undefined, mcpServers: [{ name: "arb-memory-local" }] }, startOK);
  assert.equal(r.tools, undefined);
  assert.equal(r.customTools.length, 1);
});

// REAL leak-window test (plan-panel P1): drive the host's own startSessionWithBridge with an injected
// createSession that THROWS, and assert the HOST CODE disposed the bridge — not the test. A broken host
// that forgets the catch-dispose fails this.
test("createAgentSession failure -> host disposes the bridge (real path, not manual)", async () => {
  let disposed = false;
  const bridge = { customTools: [], toolNames: [], dispose: async () => { disposed = true; } };
  const startBridge = async () => bridge;
  const createSession = async () => { throw new Error("session boom"); };
  await assert.rejects(
    startSessionWithBridge(
      { tools: ["read"], mcpServers: [{ name: "x" }], baseSessionArgs: {} },
      { startBridge, createSession }),
    /session boom/);
  assert.equal(disposed, true); // disposed by startSessionWithBridge's catch, not by the test
});

test("session success -> returns {session, bridge}, bridge NOT disposed", async () => {
  let disposed = false;
  const bridge = { customTools: [], toolNames: [], dispose: async () => { disposed = true; } };
  const r = await startSessionWithBridge(
    { tools: undefined, mcpServers: [{ name: "x" }], baseSessionArgs: {} },
    { startBridge: async () => bridge, createSession: async () => ({ session: { id: "s" } }) });
  assert.equal(r.bridge, bridge);
  assert.equal(disposed, false);
});
```

- [ ] **Step 2: Run to verify it fails**
Run: `cd tools/pi-sdk-host && node --test host.mcp.test.mjs`
Expected: FAIL (`buildSessionToolArgs is not exported`).

- [ ] **Step 3: Implement.** In `host.mjs`: import `startMcpBridge`; add the exported helper; wire
`doThreadStart`.
```js
import { startMcpBridge } from "./mcp-bridge.mjs";

// Pure, testable: given the thread/start tool inputs, start the MCP bridge (if any)
// and compute the final {tools, customTools, bridge} for createAgentSession.
export async function buildSessionToolArgs({ tools, mcpServers }, startBridge = startMcpBridge) {
  if (!Array.isArray(mcpServers) || mcpServers.length === 0) {
    return { tools, customTools: [], bridge: null };
  }
  const bridge = await startBridge(mcpServers); // throws -> propagates (fail-loud); bridge reaped internally
  let nextTools = tools;
  if (Array.isArray(tools)) {
    const merged = [...tools];
    for (const n of bridge.toolNames) if (!merged.includes(n)) merged.push(n);
    nextTools = merged;
  }
  return { tools: nextTools, customTools: bridge.customTools, bridge };
}

// Owns the leak window: start the bridge, create the session, and dispose the bridge if session
// construction throws (createAgentSession is injected so this is testable without spawning).
export async function startSessionWithBridge({ tools, mcpServers, baseSessionArgs }, deps = {}) {
  const startBridge = deps.startBridge ?? startMcpBridge;
  const createSession = deps.createSession ?? createAgentSession;
  const toolArgs = await buildSessionToolArgs({ tools, mcpServers }, startBridge);
  try {
    const result = await createSession({
      ...baseSessionArgs,
      ...(toolArgs.tools !== undefined ? { tools: toolArgs.tools } : {}),
      ...(toolArgs.customTools.length ? { customTools: toolArgs.customTools } : {}),
    });
    return { session: result.session, bridge: toolArgs.bridge, effectiveTools: toolArgs.tools };
  } catch (err) {
    if (toolArgs.bridge) { try { await toolArgs.bridge.dispose(); } catch {} } // close the leak window
    throw err;
  }
}
```
(`createAgentSession` is already imported at the top of `host.mjs`; reference it as the default dep.)
Then in `doThreadStart`, AFTER cwd+model validation and BEFORE `createAgentSession` (spawn-late), parse
`mcpServers` and call the helper inside the same `try` that wraps `createAgentSession`, disposing the
bridge on ANY failure between spawn and the successful `state.thread` assignment:
```js
  const mcpServers = Array.isArray(params?.mcpServers) ? params.mcpServers : undefined;

  let session, bridge = null, effectiveTools = tools;
  try {
    const out = await startSessionWithBridge({
      tools, mcpServers,
      baseSessionArgs: {
        cwd, model, thinkingLevel,
        sessionManager: SessionManager.inMemory(cwd),
        authStorage, modelRegistry,
        ...(resourceLoader !== undefined ? { resourceLoader } : {}),
      },
    });
    session = out.session; bridge = out.bridge; effectiveTools = out.effectiveTools ?? tools;
  } catch (err) {
    // startSessionWithBridge already disposed the bridge on a post-spawn failure; fail loud.
    return replyError(id, ERR_INTERNAL, `createAgentSession failed: ${String(err?.message || err)}`);
  }

  state.thread = { id: "th_" + randomUUID(), session, model, bridge }; // hand ownership to state.thread
```
And extend the `thread_started` stderr log to include the effective tool list
(`tools: effectiveTools ?? "default"`) as surface proof. In `gracefulCleanup`, after disposing the
session, dispose the bridge:
```js
  try { state.thread?.session.dispose(); } catch (err) { logStderr("cleanup_dispose_threw", { error: String(err?.message || err) }); }
  try { await state.thread?.bridge?.dispose(); } catch (err) { logStderr("cleanup_bridge_dispose_threw", { error: String(err?.message || err) }); }
```

- [ ] **Step 4: Run unit + full host suite to verify pass**
Run: `cd tools/pi-sdk-host && node --test`
Expected: PASS (host.mcp.test.mjs + all prior). Also run the existing pi-sdk protocol smoke to prove the
flag-off path is unchanged: `node smoke.mjs` (or `python3 smoke_protocol.py`) → unchanged behavior.

- [ ] **Step 5: Commit**
```bash
git add tools/pi-sdk-host/host.mjs tools/pi-sdk-host/host.mcp.test.mjs
git commit -m "feat(pi-mcp): wire mcpServers into doThreadStart (spawn-late, leak-window closed, allowlist append, dispose)"
```

---

### Task 5: `pi_sdk.py` — thread/start mcpServers (pi-side name) + regression gate

**Files:**
- Modify: `src/agent_redis_bridge/engines/pi_sdk.py` (imports + `start()` thread_params ~195-208)
- Test: `tests/arb_memory/test_local_memory_injection_pi_sdk.py` (new)

**Interfaces:**
- Consumes: `agent_redis_bridge.local_memory_mcp.local_memory_mcp_servers()` → `list[dict]` (each
  `{command,args,env}`) or `[]`.
- Produces: `thread/start` params gain `mcpServers: list[{name:"arb-memory-local", command, args, env}]`
  when the flag is set; absent otherwise.

- [ ] **Step 1: Write failing test** — `tests/arb_memory/test_local_memory_injection_pi_sdk.py`:
```python
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine


# Drive the EXTRACTED thread_start_params() method directly (no fake subprocess) — see Step 3.
def test_flag_set_injects_mcpservers_with_pi_side_name(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "postgresql://r:p@h:25060/d?sslmode=require")
    eng = PiSdkEngine(cwd="/tmp", model="minimax/MiniMax-M3")
    p = eng.thread_start_params()
    assert p["cwd"] == "/tmp"
    assert "mcpServers" in p
    assert p["mcpServers"][0]["name"] == "arb-memory-local"
    assert p["mcpServers"][0]["command"] == "arb-memory-local-mcp"
    assert "ARB_MEMORY_LOCAL_DSN" in p["mcpServers"][0]["env"]


def test_flag_unset_omits_mcpservers(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    eng = PiSdkEngine(cwd="/tmp", model="minimax/MiniMax-M3")
    p = eng.thread_start_params()
    assert "mcpServers" not in p
```

- [ ] **Step 2: Run to verify it fails**
Run: `PYTHONPATH="$(pwd):$(pwd)/src" uv run --extra arb-memory --with pytest pytest tests/arb_memory/test_local_memory_injection_pi_sdk.py -v`
Expected: FAIL (no `mcpServers`, and/or `_send_thread_start` missing).

- [ ] **Step 3: Implement — extract `thread_start_params()` (commit to ONE path).** Move the inline
`thread_params` assembly out of `start()` (currently `pi_sdk.py:198-206`) into a new method that returns
the dict, mirroring `CodexEngine.thread_start_params()`. `start()` then calls it. This makes the param
build testable without faking a subprocess. Add the import at module top:
```python
from agent_redis_bridge.local_memory_mcp import local_memory_mcp_servers
```
The new method (replaces the inline block; `start()` calls `self.request("thread/start", self.thread_start_params(), timeout=30)`):
```python
    def thread_start_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": self.cwd}
        if self.model is not None:
            params["model"] = self.model
        if self._tools_list:
            params["tools"] = list(self._tools_list)
        if self.thinking_level is not None:
            params["thinkingLevel"] = self.thinking_level
        if self.append_system_prompt is not None:
            params["appendSystemPrompt"] = self.append_system_prompt
        mcp_servers = local_memory_mcp_servers()
        if mcp_servers:
            params["mcpServers"] = [{**spec, "name": "arb-memory-local"} for spec in mcp_servers]
        return params
```

- [ ] **Step 4: Run to verify it passes**
Run: `PYTHONPATH="$(pwd):$(pwd)/src" uv run --extra arb-memory --with pytest pytest tests/arb_memory/test_local_memory_injection_pi_sdk.py -v`
Expected: PASS (2/2).

- [ ] **Step 5: Regression gate (spec test #9) — existing suites stay green with NO edits.**
Run: `PYTHONPATH="$(pwd):$(pwd)/src" uv run --extra arb-memory --with pytest pytest tests/arb_memory/test_local_memory_injection_acp.py tests/arb_memory/test_local_memory_injection_agent_sdk.py tests/arb_memory/test_local_memory_injection_codex.py -v`
Expected: ALL PASS, **with zero changes to those test files or `local_memory_mcp.py`**. If any fails,
the pi-side-name constraint was violated — STOP and fix the approach, do not edit those tests.
Then ENFORCE "zero edits" mechanically (plan-panel P2) — this must exit 0:
```bash
git diff --exit-code -- src/agent_redis_bridge/local_memory_mcp.py \
  tests/arb_memory/test_local_memory_injection_acp.py \
  tests/arb_memory/test_local_memory_injection_agent_sdk.py \
  tests/arb_memory/test_local_memory_injection_codex.py
```
A non-zero exit means a protected consumer/test was edited — revert and fix the pi-side approach.

- [ ] **Step 6: Commit**
```bash
git add src/agent_redis_bridge/engines/pi_sdk.py tests/arb_memory/test_local_memory_injection_pi_sdk.py
git commit -m "feat(pi-mcp): pi_sdk threads mcpServers (pi-side name=arb-memory-local) into thread/start"
```

---

### Task 6: Live pi E2E (orchestrator-run, post-merge) — decorrelated-hash + non-instructed sentinel

**Files:** none (operational). Run by the orchestrator after the branch merges to dev and the venv +
`install.sh` are applied on the host running the pi seats. Mirrors the codex decorrelated-hash test.

**Preconditions:** enable `ARB_MEMORY_LOCAL_MCP=dev` in the M3 and GLM pi seat plists
(`com.example.pi-m3-bridge.bridge-dev`, `com.example.pi-glm-bridge.bridge-dev`); ensure the
seat role profiles are **memory-silent** (must NOT mention memory/the tool — spec test #11a); `bootout`+
`bootstrap` each (plist change ⇒ not `kickstart`).

- [ ] **Step 1: Instructed probe (spec test #10).** Dispatch to each pi seat: "call `mcp__arb_memory_local__memory_recent`
with limit 1; echo the `content_hash` VERBATIM; if absent say TOOL ABSENT." Verify the returned hash
against an independent dev-store read (the psycopg-direct method used for the codex test).

- [ ] **Step 2: Seed a unique sentinel (spec test #11b).** Store a dev-store artefact whose content is a
freshly-generated unique sentinel string (not derivable from anything in the brief).

- [ ] **Step 3: Non-instructed probe (spec test #11).** Dispatch a question whose only answer is the
sentinel, WITHOUT naming any tool. Assert: (a) the seat's reply contains the sentinel, AND (b) the seat
log shows an `mcp__arb_memory_local__*` tool call. This proves autonomous discoverability via
`promptSnippet`, not plumbing.

- [ ] **Step 4: Two-part negative control (spec test #12).** With the flag OFF on a control seat:
(a) the instructed probe returns `TOOL ABSENT`; (b) the non-instructed probe cannot produce the sentinel
and the log shows no MCP tool call.

- [ ] **Step 5: Record results** for both M3 and GLM in the PR/merge notes; write a feature-specific
memory `pi-sdk-mcp-client-coverage` with the pi-engine coverage outcome (cross-link `[[go-python-boundary]]`).

---

## Self-Review (completed by plan author)
- **Spec coverage:** §1 mcp-bridge → Tasks 2-3; §2 host.mjs → Task 4; §3 (no change) → enforced by Task 5
  regression gate; §4 pi_sdk → Task 5; §5 deps → Task 1; all 13 acceptance tests mapped (Task 1: #7;
  Task 2: #1,#4,#5,#6; Task 3: #3a; Task 4: #2,#3b; Task 5: #8,#9; Task 6: #10-13).
- **Naming consistency:** `startMcpBridge`, `buildToolDefinition`, `composeToolName`, `sanitizeComponent`,
  `buildSessionToolArgs`, pi-side `name="arb-memory-local"`, tool name `mcp__arb_memory_local__<tool>`
  used consistently across Tasks 2-5.
- **External-API caveat (honest):** the EXACT `AgentToolResult` field names, the typebox compile
  entrypoint, and `@modelcontextprotocol/sdk` close-semantics are pinned by each task's Step 0 against the
  installed packages — these are real, inspectable installs, not invented.
