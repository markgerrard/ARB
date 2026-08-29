import test from "node:test";
import assert from "node:assert/strict";
import { buildSessionToolArgs, startSessionWithBridge } from "./host.mjs";

const fakeBridge = {
  customTools: [{ name: "mcp__arb_memory_local__memory_recent" }],
  toolNames: ["mcp__arb_memory_local__memory_recent"],
  dispose: async () => {},
};

const startOK = async () => fakeBridge;

test("no mcpServers -> no bridge, args unchanged", async () => {
  let called = false;
  const r = await buildSessionToolArgs({ tools: ["read"], mcpServers: undefined }, async () => {
    called = true;
    return fakeBridge;
  });
  assert.equal(called, false);
  assert.equal(r.bridge, null);
  assert.deepEqual(r.tools, ["read"]);
  assert.deepEqual(r.customTools, []);
});

test("allowlist present -> MCP names appended (deduped, order preserved)", async () => {
  const r = await buildSessionToolArgs(
    {
      tools: ["read", "grep", "mcp__arb_memory_local__memory_recent"],
      mcpServers: [{ name: "arb-memory-local" }],
    },
    startOK,
  );
  assert.deepEqual(r.tools, ["read", "grep", "mcp__arb_memory_local__memory_recent"]);
  assert.equal(r.excludeTools, undefined);
  assert.deepEqual(r.customTools.map((tool) => tool.name), ["grep", "mcp__arb_memory_local__memory_recent"]);
  assert.equal(r.bridge, fakeBridge);
});

test("no allowlist -> custom tools pass through, tools stays undefined", async () => {
  const r = await buildSessionToolArgs({ tools: undefined, mcpServers: [{ name: "arb-memory-local" }] }, startOK);
  assert.equal(r.tools, undefined);
  assert.equal(r.customTools.length, 1);
  assert.equal(r.bridge, fakeBridge);
});

test("createAgentSession failure -> host disposes the bridge (real path, not manual)", async () => {
  let disposed = false;
  const bridge = {
    customTools: [],
    toolNames: [],
    dispose: async () => {
      disposed = true;
    },
  };
  const startBridge = async () => bridge;
  const createSession = async () => {
    throw new Error("session boom");
  };
  await assert.rejects(
    startSessionWithBridge(
      { tools: ["read"], mcpServers: [{ name: "x" }], baseSessionArgs: {} },
      { startBridge, createSession },
    ),
    /session boom/,
  );
  assert.equal(disposed, true);
});

test("session success -> returns {session, bridge}, bridge NOT disposed", async () => {
  let disposed = false;
  const bridge = {
    customTools: [],
    toolNames: [],
    dispose: async () => {
      disposed = true;
    },
  };
  const r = await startSessionWithBridge(
    { tools: undefined, mcpServers: [{ name: "x" }], baseSessionArgs: {} },
    { startBridge: async () => bridge, createSession: async () => ({ session: { id: "s" } }) },
  );
  assert.deepEqual(r.session, { id: "s" });
  assert.equal(r.bridge, bridge);
  assert.equal(disposed, false);
});
