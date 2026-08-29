import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startMcpBridge } from "./mcp-bridge.mjs";

const FAKE = new URL("./fixtures/fake-mcp-server.mjs", import.meta.url).pathname;
const TMP = mkdtempSync(join(tmpdir(), "pi-mcp-"));
let n = 0;

function spec(name, env = {}) {
  const pidFile = join(TMP, `pid-${name}-${n++}`);
  return {
    spec: {
      name,
      command: process.execPath,
      args: [FAKE],
      env: { FAKE_PID_FILE: pidFile, ...env },
    },
    pidFile,
  };
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function readPid(file) {
  return Number(readFileSync(file, "utf8").trim());
}

async function waitForPidFile(file, ms = 2_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (existsSync(file)) return readPid(file);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`PID file was not written: ${file}`);
}

async function deadWithin(pid, ms = 2_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (!alive(pid)) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return !alive(pid);
}

test("happy path: discovers tools, namespaced names; dispose kills the child", async () => {
  const a = spec("fake");
  const bridge = await startMcpBridge([a.spec]);
  assert.ok(bridge.toolNames.includes("mcp__fake__echo"));
  const pid = readPid(a.pidFile);
  assert.equal(alive(pid), true);
  await bridge.dispose();
  assert.equal(alive(pid), false);
  assert.equal(await deadWithin(pid), true);
});

test("transactional: 2nd server fails initialize while alive -> throws AND spawned children are reaped", async () => {
  const ok = spec("ok");
  const bad = spec("bad", { FAKE_FAIL_MODE: "initialize_error_alive" });
  await assert.rejects(startMcpBridge([ok.spec, bad.spec]), /initialize failed|handshake|timed out|failed|error|closed/i);
  const okPid = readPid(ok.pidFile);
  assert.equal(alive(okPid), false);
  assert.equal(await deadWithin(okPid), true);
  const badPid = await waitForPidFile(bad.pidFile);
  assert.equal(alive(badPid), false);
  assert.equal(await deadWithin(badPid), true);
});

test("transactional: tools/list failure -> throws AND spawned children are reaped", async () => {
  const ok = spec("ok");
  const bad = spec("bad", { FAKE_FAIL_MODE: "toolslist" });
  await assert.rejects(startMcpBridge([ok.spec, bad.spec]), /tools\/list|failed|error/i);
  assert.equal(alive(readPid(ok.pidFile)), false);
  assert.equal(alive(readPid(bad.pidFile)), false);
  assert.equal(await deadWithin(readPid(ok.pidFile)), true);
  assert.equal(await deadWithin(readPid(bad.pidFile)), true);
});

test("dispose is idempotent", async () => {
  const a = spec("fake");
  const bridge = await startMcpBridge([a.spec]);
  const pid = readPid(a.pidFile);
  await bridge.dispose();
  await bridge.dispose();
  assert.equal(alive(pid), false);
  assert.equal(await deadWithin(pid), true);
});

test("server env is merged with process.env", async () => {
  process.env.PI_MCP_ENV_MERGE_SENTINEL = "present";
  const a = spec("env", { FAKE_REQUIRE_ENV: "PI_MCP_ENV_MERGE_SENTINEL" });
  try {
    const bridge = await startMcpBridge([a.spec]);
    assert.ok(bridge.toolNames.includes("mcp__env__echo"));
    const pid = readPid(a.pidFile);
    await bridge.dispose();
    assert.equal(alive(pid), false);
    assert.equal(await deadWithin(pid), true);
  } finally {
    delete process.env.PI_MCP_ENV_MERGE_SENTINEL;
  }
});

test("transactional: duplicate tool names -> throws AND all spawned children are reaped", async () => {
  const a = spec("dup");
  const b = spec("dup");
  await assert.rejects(startMcpBridge([a.spec, b.spec]), /duplicate MCP tool name after namespacing/i);
  assert.equal(alive(readPid(a.pidFile)), false);
  assert.equal(alive(readPid(b.pidFile)), false);
  assert.equal(await deadWithin(readPid(a.pidFile)), true);
  assert.equal(await deadWithin(readPid(b.pidFile)), true);
});

test("handshake TIMEOUT branch fires and reaps hung child", async () => {
  const h = spec("slow", { FAKE_FAIL_MODE: "hang" });
  await assert.rejects(startMcpBridge([h.spec], { handshakeMs: 200 }), /timed out/i);
  const pid = await waitForPidFile(h.pidFile);
  assert.equal(alive(pid), false);
  assert.equal(await deadWithin(pid), true);
});

test("mid-turn death: discovered tool execute() THROWS when server dies before answering callTool", async () => {
  const d = spec("dying", { FAKE_FAIL_MODE: "die_on_call" });
  const bridge = await startMcpBridge([d.spec]);
  const echo = bridge.customTools.find((tool) => tool.name === "mcp__dying__echo");
  assert.ok(echo, "echo tool is discovered");
  await assert.rejects(echo.execute("c", { msg: "x" }, undefined), /failed|closed|error/i);
  const pid = readPid(d.pidFile);
  await bridge.dispose();
  assert.equal(alive(pid), false);
  assert.equal(await deadWithin(pid), true);
});
