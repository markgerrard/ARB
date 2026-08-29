import test from "node:test";
import assert from "node:assert/strict";

import { createHost } from "./host.mjs";

const MODEL = { provider: "anthropic", id: "claude-sonnet-4-5" };

function session() {
  return {
    dispose: () => {},
    subscribe: () => () => {},
    prompt: async () => {},
    abort: async () => {},
  };
}

function guardedFactory(name) {
  return () => ({
    name,
    execute: async () => ({ content: [{ type: "text", text: name }] }),
  });
}

function harness() {
  const created = [];
  const host = createHost({
    createSession: async (args) => {
      created.push(args);
      return { session: session() };
    },
    createFindTool: guardedFactory("find"),
    createGrepTool: guardedFactory("grep"),
  });
  return { host, created };
}

async function startThread(host, tools) {
  await host.handlers.threadStart(1, {
    cwd: process.cwd(),
    model: MODEL,
    tools,
  });
  const response = host.replies.at(-1);
  assert.ok(response.result?.thread?.id, JSON.stringify(response));
  return response.result.thread.id;
}

test("requested find and grep are replaced with guarded custom tools", async () => {
  const { host, created } = harness();
  await startThread(host, ["find", "grep", "read"]);

  assert.deepEqual(created[0].tools, ["find", "grep", "read"]);
  assert.equal(created[0].excludeTools, undefined);
  assert.deepEqual(created[0].customTools.map((definition) => definition.name), ["find", "grep"]);
  assert.ok(created[0].customTools.every((definition) => definition.__guarded === true));
});

test("unrequested find and grep are not displaced", async () => {
  const { host, created } = harness();
  await startThread(host, ["read"]);

  assert.deepEqual(created[0].tools, ["read"]);
  assert.equal(created[0].excludeTools, undefined);
  assert.equal(created[0].customTools, undefined);
});

test("rotation reuses guarded find and grep definitions", async () => {
  const { host, created } = harness();
  const threadId = await startThread(host, ["find", "grep", "read"]);

  await host.handlers.threadRotate(2, { threadId });

  assert.equal(created.length, 2);
  assert.equal(created[1].excludeTools, undefined);
  assert.deepEqual(created[1].customTools.map((definition) => definition.name), ["find", "grep"]);
  assert.ok(created[1].customTools.every((definition) => definition.__guarded === true));
});
