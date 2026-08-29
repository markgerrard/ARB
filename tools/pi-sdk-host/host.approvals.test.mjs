// Warm-orch runtime 4 (pi): the host->client approval wire.
//
// pi's host has no client-consulted approval seam — `tool-guard.mjs` is a
// workspace-path containment check, not a veto the client can exercise. The
// other three runtimes all have one (Claude's PreToolUse hook, codex's
// approval request, grok's session/request_permission), and without it a warm
// pi ORCHESTRATOR could not enforce the merge/close gate while holding Bash.
//
// STRICTLY OPT-IN. The cold pi-sdk seat engine sends only
// {cwd, model?, thinkingLevel?, appendSystemPrompt?, tools?, mcpServers?} —
// never `approvals` — so with the param absent nothing is wrapped and not one
// new message appears on the wire. That is the constraint this whole feature
// is built under: cold seats must be unaffected.

import test from "node:test";
import assert from "node:assert/strict";

import {
  createApprovalChannel,
  makeApprovalGatedTool,
  routeClientResponse,
} from "./host.mjs";

function channel(overrides = {}) {
  const sent = [];
  const ch = createApprovalChannel({
    send: (msg) => sent.push(msg),
    timeoutMs: 50,
    ...overrides,
  });
  return { ch, sent };
}

test("asking emits a tool/approve REQUEST carrying the tool and its args", async () => {
  const { ch, sent } = channel();
  const pending = ch.ask({ toolName: "bash", args: { command: "git merge topic" } });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, "tool/approve");
  assert.equal(typeof sent[0].id, "number");
  assert.equal(sent[0].params.toolName, "bash");
  assert.deepEqual(sent[0].params.args, { command: "git merge topic" });
  ch.resolve(sent[0].id, { allow: true });
  await pending;
});

test("an allow verdict from the client permits the call", async () => {
  const { ch, sent } = channel();
  const pending = ch.ask({ toolName: "bash", args: {} });
  ch.resolve(sent[0].id, { allow: true });
  assert.deepEqual(await pending, { allow: true });
});

test("a deny verdict carries the code back to the tool", async () => {
  const { ch, sent } = channel();
  const pending = ch.ask({ toolName: "bash", args: {} });
  ch.resolve(sent[0].id, { allow: false, code: "merge-close-evidence-unresolved" });
  const verdict = await pending;
  assert.equal(verdict.allow, false);
  assert.equal(verdict.code, "merge-close-evidence-unresolved");
});

test("silence from the client DENIES rather than permitting", async () => {
  // Fail closed. A gate that opens when the deciding party is absent is not a
  // gate — this is the `refusal-is-ambient` defect class inverted, where
  // APPROVAL would be the ambient outcome.
  const { ch } = channel({ timeoutMs: 20 });
  const verdict = await ch.ask({ toolName: "bash", args: {} });
  assert.equal(verdict.allow, false);
  assert.equal(verdict.code, "pi-approval-timeout");
});

test("a late reply after timeout does not resurrect the call", async () => {
  const { ch, sent } = channel({ timeoutMs: 20 });
  const verdict = await ch.ask({ toolName: "bash", args: {} });
  assert.equal(verdict.allow, false);
  // The client answers after we already refused; resolving must be a no-op
  // rather than throwing or flipping a settled promise.
  ch.resolve(sent[0].id, { allow: true });
  assert.equal(ch.pendingCount(), 0);
});

test("resolving an unknown id is ignored, not fatal", () => {
  const { ch } = channel();
  assert.doesNotThrow(() => ch.resolve(4242, { allow: true }));
});


// `dispatch` rejects any client message without a `method` as
// ERR_INVALID_REQUEST "missing method". An approval ANSWER is exactly that
// shape — {id, result} — so without routing it would be refused and the tool
// would hang until the fail-closed timeout. Silent, and it would look like the
// client never answered.

test("an approval answer is routed to the channel, not treated as a request", () => {
  const { ch, sent } = channel();
  ch.ask({ toolName: "bash", args: {} });
  const handled = routeClientResponse(
    { id: sent[0].id, result: { allow: true } },
    ch,
  );
  assert.equal(handled, true);
  assert.equal(ch.pendingCount(), 0);
});

test("a normal request is left alone for the handler table", () => {
  const { ch } = channel();
  assert.equal(routeClientResponse({ id: 1, method: "thread/start", params: {} }, ch), false);
});

test("with no approval channel nothing is routed — cold path untouched", () => {
  assert.equal(routeClientResponse({ id: 1, result: {} }, null), false);
});

test("an answer for an id we never issued is not swallowed", () => {
  // Returning true would hide a genuine protocol error behind the approval
  // wire; it must fall through to the existing "missing method" handling.
  const { ch } = channel();
  assert.equal(routeClientResponse({ id: 9999, result: { allow: true } }, ch), false);
});


// The channel is dead code until a tool consults it. These cover the wrapper
// that makes the verdict load-bearing.

function innerTool(record) {
  return {
    name: "bash",
    description: "run a command",
    execute: async (toolCallId, params) => {
      record.push(params);
      return { output: "ran" };
    },
  };
}

test("an allowed call reaches the inner tool", async () => {
  const ran = [];
  const gated = makeApprovalGatedTool(innerTool(ran), {
    approvals: { ask: async () => ({ allow: true }) },
  });
  const result = await gated.execute("tc1", { command: "ls" });
  assert.deepEqual(result, { output: "ran" });
  assert.deepEqual(ran, [{ command: "ls" }]);
});

test("a denied call NEVER reaches the inner tool and names the code", async () => {
  const ran = [];
  const gated = makeApprovalGatedTool(innerTool(ran), {
    approvals: {
      ask: async () => ({ allow: false, code: "merge-close-evidence-unresolved" }),
    },
  });
  await assert.rejects(
    () => gated.execute("tc1", { command: "git merge topic" }),
    /merge-close-evidence-unresolved/,
  );
  assert.deepEqual(ran, [], "the command must not have run");
});

test("a malformed verdict denies rather than falling through", async () => {
  // Anything that is not an explicit allow is a refusal. An undefined verdict
  // reaching a truthiness check is how a gate quietly becomes a no-op.
  const ran = [];
  const gated = makeApprovalGatedTool(innerTool(ran), {
    approvals: { ask: async () => undefined },
  });
  await assert.rejects(() => gated.execute("tc1", { command: "ls" }));
  assert.deepEqual(ran, []);
});

test("the tool is asked about with its own name and real args", async () => {
  const asked = [];
  const gated = makeApprovalGatedTool(innerTool([]), {
    approvals: {
      ask: async (q) => (asked.push(q), { allow: true }),
    },
  });
  await gated.execute("tc1", { command: "git -C /repo merge topic" });
  assert.deepEqual(asked, [
    { toolName: "bash", args: { command: "git -C /repo merge topic" } },
  ]);
});


// Wiring: which tools actually get gated. The cold seat path must come out of
// this function byte-for-byte as before.

import { buildSessionToolArgs } from "./host.mjs";

const fakeFactories = {
  createFindTool: () => ({ name: "find", execute: async () => ({}) }),
  createGrepTool: () => ({ name: "grep", execute: async () => ({}) }),
  createBashTool: () => ({ name: "bash", execute: async () => ({}) }),
  createWriteTool: () => ({ name: "write", execute: async () => ({}) }),
  createEditTool: () => ({ name: "edit", execute: async () => ({}) }),
};

test("without approvals no tool is gated — the cold seat path is untouched", async () => {
  const args = await buildSessionToolArgs(
    { tools: ["bash", "read"], mcpServers: [], cwd: "/w" },
    undefined,
    fakeFactories,
  );
  assert.equal(args.customTools.some((t) => t.__approvalGated), false);
});

test("with approvals on, the dangerous tools are gated", async () => {
  const args = await buildSessionToolArgs(
    {
      tools: ["bash", "write", "read"],
      mcpServers: [],
      cwd: "/w",
      approvals: { ask: async () => ({ allow: true }) },
    },
    undefined,
    fakeFactories,
  );
  const gated = args.customTools.filter((t) => t.__approvalGated).map((t) => t.name).sort();
  assert.deepEqual(gated, ["bash", "write"]);
});

test("a tool the thread does not have is not gated into existence", async () => {
  const args = await buildSessionToolArgs(
    {
      tools: ["read"],
      mcpServers: [],
      cwd: "/w",
      approvals: { ask: async () => ({ allow: true }) },
    },
    undefined,
    fakeFactories,
  );
  assert.deepEqual(args.customTools.filter((t) => t.__approvalGated), []);
});
