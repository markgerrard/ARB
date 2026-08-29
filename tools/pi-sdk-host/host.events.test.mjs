import test from "node:test";
import assert from "node:assert/strict";
import { makeTurnHandler, resolveEventLogPath } from "./host.mjs";

function captureStdout(fn) {
  const original = process.stdout.write;
  const lines = [];
  process.stdout.write = (chunk, ...args) => {
    lines.push(String(chunk));
    const callback = args.find((arg) => typeof arg === "function");
    if (callback) callback();
    return true;
  };
  try {
    fn();
  } finally {
    process.stdout.write = original;
  }
  return lines.map((line) => JSON.parse(line));
}

function turn(overrides = {}) {
  return {
    id: "tn_test",
    completed: false,
    finalTextChunks: [],
    toolCallCount: 0,
    ...overrides,
  };
}

test("auto_retry_start forwards one truncated liveness notification", () => {
  const handler = makeTurnHandler(turn());
  const longError = "x".repeat(700);

  const messages = captureStdout(() => {
    handler({
      type: "auto_retry_start",
      attempt: 2,
      maxAttempts: 4,
      delayMs: 8000,
      errorMessage: longError,
      ignored: "not forwarded",
    });
  });

  assert.equal(messages.length, 1);
  assert.equal(messages[0].method, "turn/autoRetry");
  assert.deepEqual(messages[0].params, {
    turnId: "tn_test",
    phase: "start",
    attempt: 2,
    maxAttempts: 4,
    delayMs: 8000,
    errorMessage: "x".repeat(500),
  });
});

test("compaction_start forwards compaction liveness notification", () => {
  const handler = makeTurnHandler(turn());

  const messages = captureStdout(() => {
    handler({ type: "compaction_start", reason: "context_pressure", aborted: false });
  });

  assert.equal(messages.length, 1);
  assert.equal(messages[0].method, "turn/compaction");
  assert.deepEqual(messages[0].params, {
    turnId: "tn_test",
    phase: "start",
    reason: "context_pressure",
    aborted: false,
  });
});

test("retry and compaction events after turn completion do not notify", () => {
  const handler = makeTurnHandler(turn({ completed: true }));

  const messages = captureStdout(() => {
    handler({ type: "auto_retry_start", attempt: 1 });
    handler({ type: "compaction_start", reason: "late" });
  });

  assert.equal(messages.length, 0);
});

test("event log path template substitutes process pid placeholder", () => {
  assert.equal(
    resolveEventLogPath("/tmp/pi-sdk-{pid}.ndjson", 12345),
    "/tmp/pi-sdk-12345.ndjson",
  );
  assert.equal(
    resolveEventLogPath("/tmp/pi-sdk.ndjson", 12345),
    "/tmp/pi-sdk.ndjson",
  );
});
