// Stdio MCP server for the scored Pi SDK cell broker.
// The bridge owns the authenticated broker; this child only forwards one
// namespaced tool over the per-cell Unix socket.

import { createInterface } from "node:readline";
import net from "node:net";
import { createLineReader } from "./line-reader.mjs";

const socketPath = process.env.PI_SDK_BROKER_SOCKET;
const token = process.env.PI_SDK_BROKER_TOKEN;
const identity = process.env.PI_SDK_BROKER_IDENTITY;
if (!socketPath || !token || !identity) process.exit(2);

function write(value) {
  process.stdout.write(JSON.stringify(value) + "\n");
}

function response(id, result, error) {
  if (error) write({ jsonrpc: "2.0", id, error: { code: -32001, message: error } });
  else write({ jsonrpc: "2.0", id, result });
}

const broker = await new Promise((resolve, reject) => {
  const socket = net.createConnection(socketPath, () => resolve(socket));
  socket.once("error", reject);
});
const socketReader = createLineReader(broker);
broker.write(JSON.stringify({ token, identity }) + "\n");
const auth = JSON.parse(await socketReader.readLine());
if (auth?.ok !== true || auth.identity !== identity) {
  socketReader.close();
  broker.destroy();
  process.exit(3);
}

let queue = Promise.resolve();
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  queue = queue.then(async () => {
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      return;
    }
    if (request.id === undefined) return;
    if (request.method === "initialize") {
      response(request.id, {
        protocolVersion: "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "cell-broker", version: "1" },
      });
      return;
    }
    if (request.method === "tools/list") {
      response(request.id, { tools: [
        { name: "read", description: "Read a UTF-8 file in the scored cell.", inputSchema: { type: "object", additionalProperties: false, required: ["path"], properties: { path: { type: "string" } } } },
        { name: "write", description: "Write a UTF-8 file in the scored cell.", inputSchema: { type: "object", additionalProperties: false, required: ["path", "content"], properties: { path: { type: "string" }, content: { type: "string" } } } },
        { name: "edit", description: "Replace one exact UTF-8 fragment in a scored-cell file.", inputSchema: { type: "object", additionalProperties: false, required: ["path", "old_text", "new_text"], properties: { path: { type: "string" }, old_text: { type: "string" }, new_text: { type: "string" } } } },
        { name: "bash", description: "Run a bounded non-Git shell command in the scored cell.", inputSchema: { type: "object", additionalProperties: false, required: ["command"], properties: { command: { type: "string" } } } },
        { name: "cell_git", description: "Use the authenticated scored cell Git service.", inputSchema: { type: "object", additionalProperties: false, required: ["op"], properties: { op: { enum: ["status", "add", "commit"] }, paths: { type: "array", items: { type: "string" } }, message: { type: "string" } } } },
      ] });
      return;
    }
    if (request.method === "tools/call") {
      const params = request.params;
      if (!params || !["read", "write", "edit", "bash", "cell_git"].includes(params.name)) {
        response(request.id, null, "unknown cell broker tool");
        return;
      }
      const args = params.arguments ?? {};
      const forwarded = params.name === "cell_git" ? args : { op: params.name, ...args };
      broker.write(JSON.stringify({ kind: "tool", params: forwarded }) + "\n");
      try {
        const result = JSON.parse(await socketReader.readLine());
        if (result?.ok !== true) {
          response(request.id, { content: [{ type: "text", text: String(result?.error ?? "broker call failed") }], isError: true });
        } else {
          response(request.id, { content: [{ type: "text", text: JSON.stringify(result.result) }] });
        }
      } catch (err) {
        response(request.id, null, String(err?.message ?? err));
      }
      return;
    }
    if (request.method === "ping") {
      response(request.id, {});
      return;
    }
    response(request.id, null, `unsupported MCP method: ${request.method}`);
  }).catch((err) => {
    process.stderr.write(`[cell-broker] ${String(err?.message ?? err)}\n`);
  });
});

input.on("close", () => {
  socketReader.close();
  broker.destroy();
  process.exit(0);
});
