# pi-sdk MCP-client support — design spec

**Status:** v2. Design panel APPROVE-WITH-NITS (codex + cold-Opus + agy, all P0-free). Spec panel
(same trio): codex/agy APPROVE-WITH-NITS, cold-Opus BLOCK on two false spec claims (now corrected) —
all P0-free. v2 folds in: pi-side `name` (shared helper untouched), the `createAgentSession`-failure
leak window closed, spawn-time reap-list, the typebox/MCP-SDK dependency contract + install.sh, and
tightened tests (#3b, #4 three-part, #11 sentinel+memory-silent, #12 two-part negative control).

**Goal:** Give pi-sdk engine seats (M3, GLM, …) the ability to consume stdio MCP servers — first and
only configured consumer being the existing read-only `arb-memory-local-mcp` — so pi judgment/reviewer
seats can `memory_search/get/recent` like the other five engines already can.

**Architecture:** pi-sdk drives `@earendil-works/pi-coding-agent` through a Node host
(`tools/pi-sdk-host/host.mjs`) over JSON-RPC. That SDK has **no MCP support**, but `createAgentSession`
accepts `customTools: ToolDefinition[]`. A new host-side module spawns each configured MCP server,
discovers its tools, and wraps each as a `ToolDefinition` whose `execute` forwards to the MCP server.
The Python side reuses the existing `local_memory_mcp_servers()` config source and threads it through
`thread/start` as a new `mcpServers` param. Flag-gated by `ARB_MEMORY_LOCAL_MCP`.

**Tech stack:** Node ≥20 (host), `@modelcontextprotocol/sdk` (new dep), `typebox@1.1.38` (already
present, used via `Type.Unsafe`), Python (pi_sdk.py), the existing `arb-memory-local-mcp` server.

---

## Global Constraints

- **Flag-gated, byte-unchanged when off.** `ARB_MEMORY_LOCAL_MCP` unset ⇒ `local_memory_mcp_servers()`
  returns `[]` ⇒ no `mcpServers` param ⇒ host behavior identical to today. No new env, no new DSN logic.
- **Reuse the single config source; add the name PI-SIDE.** `pi_sdk.py` MUST obtain server specs from
  `local_memory_mcp_servers()` (the same helper the other engines use) — not a pi-specific config path.
  The shared helper `local_memory_mcp_config()` is **left UNCHANGED** (spec panel: it has 7 consumers;
  5 of them — the ACP engines — pass the whole dict on the `session/new` wire, so adding a `name` key
  there would change their payload and break `test_local_memory_injection_acp.py`). Instead `pi_sdk.py`
  attaches the canonical `name="arb-memory-local"` literal to each spec as it builds `mcpServers`. ACP
  wire stays byte-for-byte unchanged; zero regression to the other 7 consumers by construction.
- **Read-only by construction.** No write surface reaches pi; the local server has none. Memory tools
  are read-only; enabling widens a seat only by read access to a store it may already read.
- **Per-seat opt-in.** A pi seat gets memory only if its launch env sets `ARB_MEMORY_LOCAL_MCP`.
- **Fail-loud at thread start.** Any configured MCP server failing spawn / `initialize` / `tools/list`
  ⇒ `EngineError` out of `thread/start`; seat does not register healthy. No degraded-silent start.
- **No JSON-Schema→TypeBox converter.** Wrap MCP `inputSchema` via `Type.Unsafe(schema)`. typebox@1.1.38
  validates raw JSON Schema natively; a converter is lossy and strictly worse. (Panel B, unanimous.)
- **Integration point is `customTools`,** not a pi extension. (Panel A, unanimous.)
- **Generic mechanism, but one configured server today.** The host accepts a *list* and must handle
  multi-server correctly (lifecycle/naming), but `pi_sdk.py` passes only the memory server for now.

---

## Components & responsibilities

### 1. `tools/pi-sdk-host/mcp-bridge.mjs` (NEW)
One focused module. Public surface:

```
async function startMcpBridge(serverSpecs: Array<{name, command, args, env}>):
    Promise<{ customTools: ToolDefinition[], toolNames: string[], dispose(): Promise<void> }>
```

Behavior:
- **Transactional startup (agy P0 / codex+opus P1).** Iterate specs; for each: spawn the stdio
  subprocess (`@modelcontextprotocol/sdk` `Client` + `StdioClientTransport`), run `initialize` +
  `tools/list`, build `ToolDefinition`s. **Add each client to the reap-list the MOMENT its subprocess
  is spawned — BEFORE `initialize`** (agy spec P1: a client that spawns but fails handshake/timeout
  would not be in a "successfully started" list and would leak). **On ANY failure, `dispose()` every
  spawned client (started or mid-handshake) before re-throwing.** No orphaned children on a failed start.
- **Per-server bounded handshake timeout (opus P2).** `initialize`+`tools/list` must complete within a
  bounded timeout (default e.g. 15s); on timeout throw a clear `server <name> handshake timed out`
  error — do NOT let it ride pi_sdk.py's 30s `thread/start` timeout into an opaque failure.
- **`dispose()` is idempotent** — closes each MCP transport and terminates each child (SIGTERM →
  timeout → SIGKILL escalation), safe to call twice.
- **Adapter — per MCP tool → `ToolDefinition`:**
  - `name`: `mcp__<sanitizedServerName>__<sanitizedToolName>`. Sanitize BOTH the server name AND the
    MCP tool name to `[A-Za-z0-9_]` before composing (codex spec P2: an MCP tool name may contain
    punctuation/slash/colon/whitespace). The FINAL name MUST match `^[A-Za-z0-9_-]{1,64}$` (provider
    limit). If a generated name would exceed 64 chars, deterministically clamp (truncate + short stable
    hash suffix). Fail loud on duplicate final names or collision with pi built-ins
    (`read,bash,edit,write,grep,find,ls`).
  - `label`: the tool name; `description`: passthrough from MCP.
  - `promptSnippet`: derived from the MCP tool description (opus P1-3 — **load-bearing**). Without it,
    custom tools are omitted from the system prompt's "Available tools" section and the seat may never
    learn the tool exists. `promptGuidelines` optional.
  - `parameters`: `Type.Unsafe(mcpTool.inputSchema)`.
  - `executionMode`: `"parallel"` (read-only, concurrency-safe).
  - `execute(toolCallId, params, signal, …)`: forward to `client.callTool({name:<originalToolName>,
    arguments: params})`, racing the `AbortSignal` and transport-close so a dead server rejects
    promptly. **On SUCCESS** map the MCP result → pi `AgentToolResult`: text blocks concatenated;
    non-text blocks (image/resource) stringified into a compact diagnostic text block (NOT silently
    dropped). **On FAILURE (MCP result `isError:true`, OR `callTool` rejection / transport-close) the
    executor MUST THROW** — it must NOT return an `{isError:true}` object. **Verified against
    `pi-agent-core/dist/agent-loop.js`:** a resolved `execute()` is recorded as `isError:false`
    REGARDLESS of the returned object (`:433`); only a thrown/rejected `execute()` becomes an error
    tool-result (`:440`). So returning `isError:true` would make pi report an MCP error as SUCCESS
    (silent failure). The throw is how pi surfaces tool failure to the model; the turn proceeds.

### 2. `tools/pi-sdk-host/host.mjs` (MODIFY `doThreadStart`)
- Read optional `params.mcpServers` (array). When absent ⇒ unchanged path.
- **Spawn the bridge as late as possible** — after cwd/model validation, immediately before
  `createAgentSession` (opus P1-1: minimizes the leak window across the existing early-return paths at
  cwd-invalid, model-not-found, createAgentSession-failure).
- `try/catch` the bridge start; the fail-loud throw propagates as the thread/start error (bridge has
  already reaped its own servers per its transactional contract).
- **Close the post-spawn leak window (all 3 spec reviewers, P1).** Hold the bridge in a LOCAL handle.
  Existing `host.mjs` only assigns `state.thread` (and thus the bridge handle) AFTER `createAgentSession`
  succeeds (line ~310), while `createAgentSession` failure returns earlier (lines ~306-308) — so a
  bridge that started successfully then a failed session-build would orphan its children. Structure as
  `try { bridge = await startMcpBridge(...); session = await createAgentSession({...,customTools}); }
  catch (e) { if (bridge) await bridge.dispose(); throw/reply-error }` — i.e. dispose the bridge on
  EVERY return between spawn and the successful `state.thread` assignment, then hand ownership to
  `state.thread`.
- Merge `customTools` into `createAgentSession({ ..., customTools })`.
- **Allowlist augmentation (load-bearing).** When a `tools` allowlist is present (reviewer seats set
  `BRIDGE_PI_TOOLS`), append the bridge's `toolNames` to it (dedupe, preserve order) so the registry
  admits them. When no allowlist is present, custom tools activate by default — no append needed.
  Log the final effective tool list to stderr (`thread_started`) as surface proof.
- Once constructed, store the bridge handle on `state.thread`; call `bridge.dispose()` in
  `gracefulCleanup` alongside `session.dispose()` (shutdown path).

### 3. `src/agent_redis_bridge/local_memory_mcp.py` (NO CHANGE)
- **Deliberately unchanged.** The shared helper has 7 consumers; the 5 ACP engines pass its output
  directly on the `session/new` wire, so mutating it (e.g. adding a `name` key) would change their
  payload and break `test_local_memory_injection_acp.py`. The canonical server id is supplied pi-side
  (§4), keeping every other consumer byte-for-byte unchanged.

### 4. `src/agent_redis_bridge/engines/pi_sdk.py` (MODIFY)
- Call `local_memory_mcp_servers()`; if non-empty, build `mcpServers` by attaching the canonical
  `name="arb-memory-local"` literal to each spec (`{**spec, "name": "arb-memory-local"}`) and add it to
  the `thread/start` params dict. Empty ⇒ omit the param (byte-unchanged). The name lives pi-side only
  (the same literal already hard-coded in `codex.py`/`agent_sdk.py`). No other engine logic changes.

### 5. `tools/pi-sdk-host/package.json` + `install.sh` (MODIFY)
- Declare BOTH `@modelcontextprotocol/sdk` AND a `typebox` (pin compatible with the pi-bundled
  `typebox@1.1.38`) under `dependencies` — `package.json` currently declares none. **codex spec P1
  (verified empirically):** from `tools/pi-sdk-host`, `import('typebox')` and `import('@modelcontextprotocol/sdk')`
  both FAIL today — typebox is nested under the symlinked pi package, not directly resolvable, and
  `install.sh` only symlinks pi packages (never `npm install`s). So: add the deps, and update
  `install.sh` to install them. Add a `"test": "node --test"` script (agy spec P2).

---

## Failure & security posture (explicit)

- **Startup failure** (any configured server) → fail-loud `EngineError`, seat unhealthy, all spawned
  children reaped. **Mid-turn server death** → `callTool` rejects fast → executor THROWS → pi error
  tool-result → turn proceeds (no hang, no silent loss). **Handshake hang** → bounded per-server
  timeout → clear error.
- **Security:** read-only tools, flag-gated per seat, no write surface, no broadening of non-memory
  tools (only the discovered MCP tool names are appended to the allowlist, audited via the logged
  effective tool list).

## Testing strategy (acceptance criteria)

**Node unit (`tools/pi-sdk-host/`, fake MCP server fixtures unless noted):**
1. Adapter maps a fake MCP tool → valid `ToolDefinition` (namespaced name with BOTH components
   sanitized, `Type.Unsafe` params, `executionMode:"parallel"`, `promptSnippet` set, executor calls
   the ORIGINAL tool name).
2. Allowlist present ⇒ exactly the discovered MCP tool names appended, deduped, order preserved.
3. **Transactional startup — two cases, both assert NO orphan child:** (a) two fake servers, second
   fails during `initialize`/`tools/list` ⇒ `startMcpBridge` throws and the first server's child is
   terminated; (b) **bridge starts OK then `createAgentSession` is forced to throw** ⇒ host disposes
   the bridge (the post-spawn leak window). Assert the spawned child PID is gone in both.
4. **Schema pinned (opus/codex P1-2) — three explicit assertions, real schema:** obtain the REAL
   `inputSchema` for `memory_search/get/recent` by spawning the actual `arb-memory-local-mcp` and
   calling `tools/list` (or a committed fixture WITH a documented refresh command — not a hand-written
   stub). Assert: (i) `Compile(Type.Unsafe(schema)).Check(validArgs) === true`; (ii)
   `Compile(...).Check(badArgs) === false` where badArgs has a WRONG TYPE on a required field (not a
   trivially malformed blob); (iii) driving the wrapped `ToolDefinition`/pi `validateToolArguments`
   path with badArgs produces the error-tool result (proves pi's real throw-on-invalid behavior — raw
   `Check` returns false, it does NOT throw).
5. **Mid-turn death:** fake server exits before answering `callTool` ⇒ `execute` THROWS within the
   timeout (no hang); driven through pi's tool path this yields an error tool-result.
6. Name clamp: an over-64-char generated name is deterministically clamped to ≤64 and still unique.
7. **Clean-install import smoke (codex P1):** after `install.sh` in a clean checkout, importing
   `./mcp-bridge.mjs` from `tools/pi-sdk-host` succeeds (proves `typebox` + `@modelcontextprotocol/sdk`
   resolve as direct imports, not just transitively).

**Python unit (`pi_sdk.py`):**
8. Flag set ⇒ `thread/start` params include `mcpServers` carrying `name:"arb-memory-local"` + `command`
   + `args` + `env` (DSN + present keys). Flag unset ⇒ no `mcpServers` key.
9. **No-regression by construction:** `local_memory_mcp_config()` is unchanged, so codex / agent_sdk /
   ACP existing injection tests are untouched. (Run them anyway as a gate; they must stay green with no
   edits — if any needs editing, the pi-side `name` decision was violated.)

**Live E2E (close-condition — mirrors the codex decorrelated-hash test):**
10. Enable `ARB_MEMORY_LOCAL_MCP=dev` on a pi seat; **instructed** probe: dispatch "call `memory_recent`,
    echo the `content_hash`"; verify the hash against an independent dev-store read.
11. **Non-instructed probe (opus P1-3/P2-B — the anti-vacuous-green gate), two preconditions stated:**
    (a) the seat role profile MUST be memory-silent (must NOT mention memory or the tool — else the
    probe passes by instruction even if `promptSnippet` is broken, masking the load-bearing fix);
    (b) seed a UNIQUE sentinel artefact in the dev store, then dispatch a question whose answer is only
    that sentinel WITHOUT naming the tool; assert the seat autonomously calls a memory tool AND its
    answer contains the sentinel (mirror #10's hash discipline). Proves discoverability, not plumbing.
12. **Negative controls (two-part, codex P2):** (a) instructed probe, flag OFF ⇒ `TOOL ABSENT`;
    (b) non-instructed probe, flag OFF ⇒ seat cannot produce the sentinel AND the event log shows no
    MCP tool call.
13. Run 10–12 against BOTH M3 and GLM pi seats.

## Out of scope (YAGNI)
- Multiple *configured* servers from `pi_sdk.py` (mechanism supports a list; only memory is wired).
- Binary/image/resource MCP content beyond compact-stringify diagnostic.
- A JSON-Schema→TypeBox converter (explicitly rejected).
- Wiring pi_rpc (only pi_sdk).
- Any change to `local_memory_mcp_config()` or the other 7 consumers (the `name` is pi-side only).

## Residual risks (documented, accepted)
- Exotic MCP schemas (unusual `format`, `additionalProperties:false` + model-added keys, `$ref`-bearing
  param schemas where `coerceWithJsonSchema` doesn't follow `$ref`) can hard-fail a tool call. Acceptable
  fail-loud behavior; arb-memory schemas are pinned by test #4 so the shipped path is proven.
- `~unsafe` own-key from `Type.Unsafe` (one-line verify pi strips TypeBox `~`-internal keys before
  serializing `parameters` to the provider tool spec; built-in pi tools are TypeBox and ship fine).
- **Handshake-budget composition (opus P2-C):** bridge startup runs INSIDE pi_sdk.py's 30s
  `thread/start` timeout (`pi_sdk.py:208`), and `createAgentSession` runs after it in the same window.
  The per-server handshake default (15s) is chosen deliberately to stay well under 30s so the bridge
  fails with its own clear error before the Python side times out opaquely. Keep handshake budget ≪ 30s.
