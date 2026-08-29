import test from "node:test";
import assert from "node:assert/strict";

import { createHost } from "./host.mjs";

const MODEL = { provider: "anthropic", id: "claude-sonnet-4-5" };
const BRIDGE = {
  customTools: [{ name: "mcp__fake__lookup" }],
  toolNames: ["mcp__fake__lookup"],
  dispose: async () => {},
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function session(overrides = {}) {
  return {
    dispose: () => {},
    subscribe: () => () => {},
    prompt: async () => {},
    abort: async () => {},
    ...overrides,
  };
}

function harness({ createSession, startBridge = async () => BRIDGE } = {}) {
  const created = [];
  const sessions = [];
  const create = createSession ?? (async (args) => {
    created.push(args);
    const next = session();
    sessions.push(next);
    return { session: next };
  });
  const host = createHost({
    createSession: async (args) => {
      const result = await create(args);
      if (!created.includes(args)) created.push(args);
      if (result?.session && !sessions.includes(result.session)) sessions.push(result.session);
      return result;
    },
    startBridge,
  });
  return { host, created, sessions };
}

async function startThread(host, params = {}) {
  await host.handlers.threadStart(1, {
    cwd: process.cwd(),
    model: MODEL,
    tools: ["read"],
    mcpServers: [{ name: "fake" }],
    thinkingLevel: "high",
    ...params,
  });
  const response = host.replies.at(-1);
  assert.equal(response.id, 1);
  assert.ok(response.result?.thread?.id, JSON.stringify(response));
  return response.result.thread.id;
}

function lastReply(host) {
  return host.replies.at(-1);
}

test("rotate happy path: new thread id, old session disposed, bridge preserved, reply oldDisposed true", async () => {
  let disposed = false;
  const oldSession = session({ dispose: () => { disposed = true; } });
  const newSession = session();
  let calls = 0;
  const { host, created } = harness({
    createSession: async (args) => {
      created.push(args);
      calls += 1;
      return { session: calls === 1 ? oldSession : newSession };
    },
  });

  const oldId = await startThread(host);
  await host.handlers.threadRotate(2, { threadId: oldId });

  const response = lastReply(host);
  assert.deepEqual(response.result.oldDisposed, true);
  assert.notEqual(response.result.thread.id, oldId);
  assert.equal(disposed, true);
  assert.equal(host.state.thread.session, newSession);
  assert.equal(host.state.thread.bridge, BRIDGE);
  assert.equal(created.length, 2);
  assert.equal(host.state.thread.params.thinkingLevel, "high");
  assert.equal(response.result.thread.params.thinkingLevel, "high");
});

test("rotate reconstructs resourceLoader when appendSystemPrompt was set", async () => {
  const { host, created } = harness();
  const oldId = await startThread(host, { appendSystemPrompt: "seat profile" });
  const firstLoader = created[0].resourceLoader;
  assert.ok(firstLoader);

  await host.handlers.threadRotate(2, { threadId: oldId });

  assert.ok(created[1].resourceLoader);
  assert.notEqual(created[1].resourceLoader, firstLoader);
  assert.deepEqual(created[1].resourceLoader.getAppendSystemPrompt(), ["seat profile"]);
});

test("rotate reuses stored authStorage and modelRegistry, fresh SessionManager", async () => {
  const { host, created } = harness();
  const oldId = await startThread(host);
  const oldArgs = created[0];

  await host.handlers.threadRotate(2, { threadId: oldId });

  assert.equal(created[1].authStorage, oldArgs.authStorage);
  assert.equal(created[1].modelRegistry, oldArgs.modelRegistry);
  assert.notEqual(created[1].sessionManager, oldArgs.sessionManager);
  assert.deepEqual(created[1].tools, ["read", "mcp__fake__lookup"]);
  assert.deepEqual(created[1].customTools, BRIDGE.customTools);
});

test("rotate rejects: no thread started", async () => {
  const { host } = harness();
  await host.handlers.threadRotate(1, { threadId: "th_missing" });
  assert.equal(lastReply(host).error.code, -32001);
});

test("rotate rejects: threadId mismatch", async () => {
  const { host } = harness();
  const id = await startThread(host);
  await host.handlers.threadRotate(2, { threadId: id + "-wrong" });
  assert.equal(lastReply(host).error.code, -32602);
  assert.equal(host.state.thread.id, id);
});

test("rotate rejects: active turn in flight", async () => {
  const { host } = harness();
  const id = await startThread(host);
  host.state.activeTurn = { id: "tn_active" };

  await host.handlers.threadRotate(2, { threadId: id });
  assert.equal(lastReply(host).error.code, -32003);
  assert.equal(host.state.rotateInFlight, false);
});

test("rotate rejects while rotateInFlight; second concurrent rotate rejected", async () => {
  const gate = deferred();
  let calls = 0;
  const oldSession = session();
  const { host } = harness({
    createSession: async () => {
      calls += 1;
      if (calls === 1) return { session: oldSession };
      await gate.promise;
      return { session: session() };
    },
  });
  const id = await startThread(host);

  const first = host.handlers.threadRotate(2, { threadId: id });
  await Promise.resolve();
  await host.handlers.threadRotate(3, { threadId: id });
  assert.equal(lastReply(host).id, 3);
  assert.equal(lastReply(host).error.code, -32003);

  gate.resolve();
  await first;
  assert.equal(host.state.rotateInFlight, false);
});

test("turn/start rejected while rotateInFlight", async () => {
  const gate = deferred();
  let calls = 0;
  const { host } = harness({
    createSession: async () => {
      calls += 1;
      if (calls === 1) return { session: session() };
      await gate.promise;
      return { session: session() };
    },
  });
  const id = await startThread(host);
  const rotating = host.handlers.threadRotate(2, { threadId: id });
  await Promise.resolve();

  await host.handlers.turnStart(3, { threadId: id, message: "hello" });
  assert.equal(lastReply(host).id, 3);
  assert.equal(lastReply(host).error.code, -32003);

  await host.handlers.threadStart(4, { cwd: process.cwd(), model: MODEL });
  assert.equal(lastReply(host).id, 4);
  assert.equal(lastReply(host).error.code, -32003);
  assert.equal(lastReply(host).error.message, "thread rotate in progress");

  gate.resolve();
  await rotating;
});

test("create-failure leaves old session installed and replies error", async () => {
  const oldSession = session();
  let calls = 0;
  const { host } = harness({
    createSession: async () => {
      calls += 1;
      if (calls === 1) return { session: oldSession };
      throw new Error("new session unavailable");
    },
  });
  const id = await startThread(host);

  await host.handlers.threadRotate(2, { threadId: id });
  assert.equal(lastReply(host).error.code, -32603);
  assert.match(lastReply(host).error.message, /new session unavailable/);
  assert.equal(host.state.thread.session, oldSession);
  assert.equal(host.state.thread.id, id);
  assert.equal(host.state.rotateInFlight, false);
});

test("dispose-throw completes swap and replies oldDisposed false", async () => {
  const oldSession = session({ dispose: () => { throw new Error("dispose failed"); } });
  const newSession = session();
  let calls = 0;
  const { host } = harness({
    createSession: async () => {
      calls += 1;
      return { session: calls === 1 ? oldSession : newSession };
    },
  });
  const id = await startThread(host);

  await host.handlers.threadRotate(2, { threadId: id });
  assert.equal(lastReply(host).result.oldDisposed, false);
  assert.notEqual(lastReply(host).result.thread.id, id);
  assert.equal(host.state.thread.session, newSession);
});
