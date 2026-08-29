import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

import { SessionManager, createAgentSession } from "@earendil-works/pi-coding-agent";
import { buildSessionToolArgs } from "./host.mjs";

test("real SDK registry keeps requested guarded find and grep active", async (t) => {
  const cwd = await mkdtemp(path.join(os.tmpdir(), "pi-sdk-host-guard-"));
  t.after(() => rm(cwd, { recursive: true, force: true }));

  const toolArgs = await buildSessionToolArgs({
    cwd,
    tools: ["read", "find", "grep"],
  });
  const { session } = await createAgentSession({
    cwd,
    sessionManager: SessionManager.inMemory(),
    tools: toolArgs.tools,
    customTools: toolArgs.customTools,
    ...(toolArgs.excludeTools ? { excludeTools: toolArgs.excludeTools } : {}),
  });
  t.after(() => session.dispose());

  assert.ok(session.getActiveToolNames().includes("find"));
  assert.ok(session.getActiveToolNames().includes("grep"));

  const find = session._toolDefinitions.get("find")?.definition;
  const grep = session._toolDefinitions.get("grep")?.definition;
  assert.equal(find?.__guarded, true);
  assert.equal(grep?.__guarded, true);
  await assert.rejects(
    find.execute("guard-test", { pattern: "*", path: "/" }),
    /path outside workspace/,
  );
});
