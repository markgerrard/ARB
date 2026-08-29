import { writeFileSync } from "node:fs";
import process from "node:process";

if (process.env.FAKE_PID_FILE) {
  writeFileSync(process.env.FAKE_PID_FILE, String(process.pid));
}

if (process.env.FAKE_REQUIRE_ENV && !process.env[process.env.FAKE_REQUIRE_ENV]) {
  process.exit(44);
}

if (process.env.FAKE_FAIL_MODE === "handshake") {
  process.exit(42);
}

if (process.env.FAKE_FAIL_MODE === "hang") {
  process.stdin.resume();
  setInterval(() => {}, 1_000);
  await new Promise(() => {});
}

let buffer = "";

function send(message) {
  process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`);
}

function sendResult(id, result) {
  send({ id, result });
}

function sendError(id, code, message) {
  send({ id, error: { code, message } });
}

const echoTool = {
  name: process.env.FAKE_TOOL_NAME || "echo",
  description: "Echo a message.",
  inputSchema: {
    type: "object",
    properties: {
      msg: { type: "string" },
    },
  },
};

function handle(message) {
  if (!("id" in message)) return;
  if (message.method === "initialize") {
    if (process.env.FAKE_FAIL_MODE === "initialize_error_alive") {
      sendError(message.id, -32000, "initialize failed but server stayed alive");
      return;
    }
    sendResult(message.id, {
      protocolVersion: "2025-11-25",
      capabilities: { tools: {} },
      serverInfo: { name: "fake-mcp-server", version: "0.1.0" },
    });
    return;
  }
  if (message.method === "tools/list") {
    if (process.env.FAKE_FAIL_MODE === "toolslist") {
      sendError(message.id, -32000, "tools/list failed");
      return;
    }
    sendResult(message.id, { tools: [echoTool] });
    return;
  }
  if (message.method === "tools/call") {
    if (process.env.FAKE_FAIL_MODE === "die_on_call") {
      process.exit(43);
    }
    sendResult(message.id, {
      content: [{ type: "text", text: String(message.params?.arguments?.msg ?? "") }],
    });
    return;
  }
  sendError(message.id, -32601, `method not found: ${message.method}`);
}

process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  for (;;) {
    const index = buffer.indexOf("\n");
    if (index === -1) return;
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    handle(JSON.parse(line));
  }
});
