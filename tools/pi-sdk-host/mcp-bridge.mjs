import { createHash } from "node:crypto";
import { defineTool } from "@earendil-works/pi-coding-agent";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { Type } from "typebox";

export const BUILTIN_TOOL_NAMES = new Set(["read", "bash", "edit", "write", "grep", "find", "ls"]);
const MAX_TOOL_NAME = 64;
const HANDSHAKE_MS = 15_000;
const CHILD_EXIT_WAIT_MS = 2_000;

export function sanitizeComponent(s) {
  return String(s).replace(/[^A-Za-z0-9_]/g, "_");
}

export function composeToolName(server, tool) {
  const full = `mcp__${sanitizeComponent(server)}__${sanitizeComponent(tool)}`;
  if (full.length <= MAX_TOOL_NAME) return full;
  const hash = createHash("sha256").update(full).digest("hex").slice(0, 8);
  return `${full.slice(0, MAX_TOOL_NAME - 9)}_${hash}`;
}

function blocksToText(callToolResult) {
  const blocks = Array.isArray(callToolResult?.content) ? callToolResult.content : [];
  return blocks
    .map((block) => {
      if (block?.type === "text") return block.text;
      return `[${block?.type ?? "unknown"} block: ${JSON.stringify(block)}]`;
    })
    .join("\n");
}

export function buildToolDefinition(serverName, mcpTool, client) {
  const description = mcpTool.description || mcpTool.name;
  return defineTool({
    name: composeToolName(serverName, mcpTool.name),
    label: mcpTool.name,
    description,
    promptSnippet: description,
    parameters: Type.Unsafe(mcpTool.inputSchema ?? { type: "object" }),
    executionMode: "parallel",
    async execute(_toolCallId, params, signal) {
      let res;
      try {
        res = await client.callTool({ name: mcpTool.name, arguments: params ?? {} }, undefined, { signal });
      } catch (err) {
        throw new Error(`MCP tool ${mcpTool.name} failed: ${String(err?.message || err)}`);
      }
      if (res?.isError === true) {
        throw new Error(`MCP tool ${mcpTool.name} returned error: ${blocksToText(res)}`);
      }
      return { content: [{ type: "text", text: blocksToText(res) }], details: undefined };
    },
  });
}

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([
    promise.finally(() => {
      clearTimeout(timer);
    }),
    timeout,
  ]);
}

function childExited(processToCheck) {
  return processToCheck?.exitCode !== null || processToCheck?.signalCode !== null;
}

async function waitForChildExit(processToWait, ms = CHILD_EXIT_WAIT_MS) {
  if (!processToWait || childExited(processToWait)) return true;
  const pid = processToWait.pid;
  const closePromise = new Promise((resolve) => {
    processToWait.once?.("close", () => resolve(true));
  });
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (childExited(processToWait)) return true;
    if (pid) {
      try {
        process.kill(pid, 0);
      } catch {
        return true;
      }
    }
    const remaining = Math.max(0, Math.min(25, end - Date.now()));
    const closed = await Promise.race([
      closePromise,
      new Promise((resolve) => setTimeout(() => resolve(false), remaining)),
    ]);
    if (closed || childExited(processToWait)) return true;
  }
  return childExited(processToWait);
}

async function killChild(processToKill) {
  if (!processToKill || childExited(processToKill)) return;
  try {
    processToKill.kill("SIGTERM");
  } catch {}
  await waitForChildExit(processToKill, 300);
  if (!childExited(processToKill)) {
    try {
      processToKill.kill("SIGKILL");
    } catch {}
  }
  if (!(await waitForChildExit(processToKill))) {
    throw new Error(`child process ${processToKill.pid ?? "<unknown>"} did not exit after SIGKILL`);
  }
}

async function disposeAll(entries) {
  const results = await Promise.allSettled(
    entries.map(async ({ client, transport, process: childProcess }) => {
      const processToKill = childProcess ?? transport?._process;
      try {
        await transport?.close?.();
      } catch {}
      try {
        await client?.close?.();
      } catch {}
      await killChild(processToKill);
    }),
  );
  const rejected = results.find((result) => result.status === "rejected");
  if (rejected) throw rejected.reason;
}

export async function startMcpBridge(serverSpecs, { handshakeMs = HANDSHAKE_MS } = {}) {
  const entries = [];
  try {
    const customTools = [];
    for (const spec of serverSpecs) {
      const transport = new StdioClientTransport({
        command: spec.command,
        args: spec.args ?? [],
        env: { ...process.env, ...(spec.env ?? {}) },
      });
      const client = new Client({ name: "pi-sdk-host", version: "0.1.0" }, { capabilities: {} });
      const entry = { client, transport, process: undefined };
      entries.push(entry);
      await withTimeout(client.connect(transport), handshakeMs, `server ${spec.name} handshake timed out`);
      entry.process = transport._process;
      const listed = await withTimeout(client.listTools(), handshakeMs, `server ${spec.name} tools/list timed out`);
      for (const tool of listed.tools ?? []) {
        customTools.push(buildToolDefinition(spec.name, tool, client));
      }
    }

    const toolNames = customTools.map((tool) => tool.name);
    const seen = new Set();
    for (const name of toolNames) {
      if (BUILTIN_TOOL_NAMES.has(name)) {
        // Defensive only: generated MCP tool names are always mcp__-prefixed.
        throw new Error(`MCP tool name collides with built-in: ${name}`);
      }
      if (seen.has(name)) {
        throw new Error(`duplicate MCP tool name after namespacing: ${name}`);
      }
      seen.add(name);
    }

    let disposed = false;
    const dispose = async () => {
      if (disposed) return;
      disposed = true;
      await disposeAll(entries);
    };
    return { customTools, toolNames, dispose };
  } catch (err) {
    try {
      await disposeAll(entries);
    } catch {}
    throw err;
  }
}
