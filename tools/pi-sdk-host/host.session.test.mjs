// Warm-orch runtime 4 (pi): session persistence selection.
//
// The host has always built `SessionManager.inMemory(cwd)` — deliberately, for
// the COLD seat engine: a dispatched worker must not accumulate context and
// should leave nothing on disk. The warm orchestrator is the opposite polarity
// and needs a PERSISTED session it can reopen on a later process.
//
// pi's SDK supports this and the host simply never exposed it:
//   SessionManager.create(cwd, sessionDir?)  -> new, persisted
//   SessionManager.open(path, sessionDir?)   -> resume ("used for resume and
//                                               branching", session-manager.d.ts)
//   SessionManager.inMemory(cwd)             -> not persisted at all
//
// These tests inject a fake SessionManager so they stay fast and offline —
// host.rotate.test.mjs and host.guard.integration.test.mjs both HANG at
// baseline (pre-existing, unrelated to this change), so neither can serve as a
// regression net.

import test from "node:test";
import assert from "node:assert/strict";

import { buildSessionManager, sessionOptionsFromParams } from "./host.mjs";

function fakeSessionManager() {
  const calls = [];
  return {
    calls,
    inMemory: (...args) => (calls.push(["inMemory", ...args]), { kind: "inMemory" }),
    create: (...args) => (calls.push(["create", ...args]), { kind: "create" }),
    open: (...args) => (calls.push(["open", ...args]), { kind: "open" }),
  };
}

test("defaults to inMemory so the cold seat path is unchanged", () => {
  const SM = fakeSessionManager();
  const manager = buildSessionManager({ cwd: "/w" }, SM);
  assert.equal(manager.kind, "inMemory");
  assert.deepEqual(SM.calls, [["inMemory", "/w"]]);
});

test("a session dir alone creates a new PERSISTED session", () => {
  const SM = fakeSessionManager();
  const manager = buildSessionManager({ cwd: "/w", sessionDir: "/s" }, SM);
  assert.equal(manager.kind, "create");
  assert.deepEqual(SM.calls, [["create", "/w", "/s"]]);
});

test("a session file RESUMES that session rather than creating one", () => {
  const SM = fakeSessionManager();
  const manager = buildSessionManager(
    { cwd: "/w", sessionDir: "/s", sessionFile: "/s/prior.jsonl" },
    SM,
  );
  assert.equal(manager.kind, "open");
  assert.deepEqual(SM.calls, [["open", "/s/prior.jsonl", "/s", "/w"]]);
});

test("resuming never silently falls back to a fresh session", () => {
  // If `open` throws, the caller must SEE it. Swallowing the error and
  // creating a new session would present a cold session as a warm one — the
  // failure the channel abstraction exists to make impossible, and invisible
  // from the outside because the turn would simply have no memory.
  const SM = fakeSessionManager();
  SM.open = () => {
    throw new Error("session file is corrupt");
  };
  assert.throws(
    () => buildSessionManager({ cwd: "/w", sessionDir: "/s", sessionFile: "/s/x.jsonl" }, SM),
    /corrupt/,
  );
  assert.equal(SM.calls.length, 0, "must not fall back to create/inMemory");
});


// Param extraction is separated out because the full `thread/start` path is
// only exercised by host.rotate.test.mjs and host.guard.integration.test.mjs,
// both of which HANG at baseline. A wrong param name is the realistic bug
// here, so it gets a fast test of its own rather than no test.

test("session options are absent unless the caller asks for persistence", () => {
  assert.deepEqual(sessionOptionsFromParams({}, "/w"), { cwd: "/w" });
});

test("sessionDir and sessionFile are read from the request params", () => {
  assert.deepEqual(
    sessionOptionsFromParams({ sessionDir: "/s", sessionFile: "/s/p.jsonl" }, "/w"),
    { cwd: "/w", sessionDir: "/s", sessionFile: "/s/p.jsonl" },
  );
});

test("non-string session params are ignored rather than passed through", () => {
  // A JSON-RPC peer can send anything. Passing a number to SessionManager.open
  // would fail deep in the SDK with a confusing error instead of here.
  assert.deepEqual(
    sessionOptionsFromParams({ sessionDir: 7, sessionFile: null }, "/w"),
    { cwd: "/w" },
  );
});
