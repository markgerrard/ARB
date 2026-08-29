import test from "node:test";
import assert from "node:assert/strict";

import { makeGuardedFsTools, makeGuardedTool } from "./tool-guard.mjs";

const CWD = "/workspace/project";

function inner(execute = async () => ({ content: [{ type: "text", text: "ok" }] })) {
  let calls = 0;
  return {
    name: "find",
    execute: async (...args) => {
      calls += 1;
      return execute(...args);
    },
    get calls() {
      return calls;
    },
  };
}

test("scope clamp rejects outside paths without invoking the inner tool", async () => {
  for (const path of ["/", "../x", `${CWD}-evil`]) {
    const definition = inner();
    const guarded = makeGuardedTool(definition, { cwd: CWD, timeoutS: 30, label: "find" });

    await assert.rejects(
      guarded.execute("call", { path }, new AbortController().signal),
      /path outside workspace:/,
    );
    assert.equal(definition.calls, 0, path);
  }
});

test("inside, relative, and omitted paths pass through unchanged", async () => {
  for (const params of [{ path: `${CWD}/src` }, { path: "src" }, {}]) {
    const result = { content: [{ type: "text", text: JSON.stringify(params) }] };
    const definition = inner(async () => result);
    const guarded = makeGuardedTool(definition, { cwd: CWD, timeoutS: 30, label: "find" });

    assert.equal(await guarded.execute("call", params, new AbortController().signal), result);
    assert.equal(definition.calls, 1);
  }
});

test("timeout aborts the inner tool and rejects with a tool error", async () => {
  let innerSignal;
  const definition = inner(async (_id, _params, signal) => {
    innerSignal = signal;
    await new Promise(() => {});
  });
  const guarded = makeGuardedTool(definition, { cwd: CWD, timeoutS: 0.05, label: "find" });

  await assert.rejects(
    guarded.execute("call", {}, new AbortController().signal),
    /find timed out after 0.05s/,
  );
  assert.equal(innerSignal.aborted, true);
});

test("inner errors pass through unchanged", async () => {
  const error = new Error("inner failed");
  const definition = inner(async () => { throw error; });
  const guarded = makeGuardedTool(definition, { cwd: CWD, timeoutS: 30, label: "find" });

  await assert.rejects(guarded.execute("call", {}, new AbortController().signal), (actual) => actual === error);
});

test("an already-aborted caller signal is passed through to the inner tool", async () => {
  const definition = inner(async (_id, _params, signal) => {
    if (signal.aborted) throw new Error("Operation aborted");
  });
  const guarded = makeGuardedTool(definition, { cwd: CWD, timeoutS: 30, label: "find" });
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(guarded.execute("call", {}, controller.signal), /Operation aborted/);
});

test("makeGuardedFsTools preserves names and reads the timeout from the environment", () => {
  const original = process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S;
  try {
    process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S = "0.05";
    const fromEnv = makeGuardedFsTools({
      cwd: CWD,
      createFind: () => inner(),
      createGrep: () => ({ ...inner(), name: "grep" }),
    });
    assert.deepEqual(fromEnv.map((definition) => definition.name), ["find", "grep"]);
    assert.equal(fromEnv[0].__timeoutS, 0.05);

    delete process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S;
    const fromDefault = makeGuardedFsTools({
      cwd: CWD,
      createFind: () => inner(),
      createGrep: () => ({ ...inner(), name: "grep" }),
    });
    assert.equal(fromDefault[0].__timeoutS, 30);
  } finally {
    if (original === undefined) delete process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S;
    else process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S = original;
  }
});
