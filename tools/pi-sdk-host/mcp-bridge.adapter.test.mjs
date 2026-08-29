// Refresh tools/pi-sdk-host/fixtures/arb-memory-tools.json from a live local server with:
//   cd tools/pi-sdk-host
//   node --input-type=module -e "import {Client} from '@modelcontextprotocol/sdk/client/index.js'; import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js'; const t=new StdioClientTransport({command:'arb-memory-local-mcp',args:[],env:process.env}); const c=new Client({name:'pi-sdk-host-fixture-refresh',version:'0.1.0'},{capabilities:{}}); await c.connect(t); try { const listed=await c.listTools(); const names=new Set(['memory_search','memory_get','memory_recent']); console.log(JSON.stringify(listed.tools.filter((tool)=>names.has(tool.name)).map(({name,description,inputSchema})=>({name,description,inputSchema})).sort((a,b)=>a.name.localeCompare(b.name)),null,2)); } finally { await c.close().catch(()=>{}); await t.close().catch(()=>{}); }" > fixtures/arb-memory-tools.json
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
// pi-ai >=0.80 dropped the "./base" subpath export; validateToolArguments
// is exported from the package root.
import { validateToolArguments } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { Compile } from "typebox/compile";
import { sanitizeComponent, composeToolName, buildToolDefinition } from "./mcp-bridge.mjs";

const ARB_MEMORY_TOOLS_FIXTURE = new URL("./fixtures/arb-memory-tools.json", import.meta.url);

test("sanitizeComponent maps non-alnum to underscore", () => {
  assert.equal(sanitizeComponent("arb-memory-local"), "arb_memory_local");
  assert.equal(sanitizeComponent("mem/search:v2 "), "mem_search_v2_");
});

test("composeToolName namespaces both components and stays <=64", () => {
  assert.equal(
    composeToolName("arb-memory-local", "memory_recent"),
    "mcp__arb_memory_local__memory_recent",
  );
  const long = composeToolName("a".repeat(40), "b".repeat(40));
  assert.ok(long.length <= 64, `len ${long.length}`);
  assert.match(long, /^[A-Za-z0-9_-]{1,64}$/);
  assert.equal(long, composeToolName("a".repeat(40), "b".repeat(40)));
  assert.notEqual(long, composeToolName("a".repeat(40), "c".repeat(40)));
});

test("buildToolDefinition wraps schema with Type.Unsafe, sets promptSnippet, parallel mode", async () => {
  const mcpTool = {
    name: "memory_recent",
    description: "List recent artefacts.",
    inputSchema: { type: "object", properties: { limit: { type: "integer" } } },
  };
  let captured;
  const client = {
    callTool: async (req) => {
      captured = req;
      return { content: [{ type: "text", text: "ok" }] };
    },
  };
  const td = buildToolDefinition("arb-memory-local", mcpTool, client);
  assert.equal(td.name, "mcp__arb_memory_local__memory_recent");
  assert.equal(td.promptSnippet, "List recent artefacts.");
  assert.equal(td.executionMode, "parallel");
  assert.deepEqual(JSON.parse(JSON.stringify(td.parameters)), mcpTool.inputSchema);

  const res = await td.execute("call-1", { limit: 3 }, undefined);
  assert.equal(captured.name, "memory_recent");
  assert.deepEqual(captured.arguments, { limit: 3 });
  assert.deepEqual(res, { content: [{ type: "text", text: "ok" }], details: undefined });
});

test("buildToolDefinition falls back to tool name when MCP description is blank", () => {
  const td = buildToolDefinition(
    "arb-memory-local",
    { name: "memory_recent", description: "", inputSchema: { type: "object" } },
    { callTool: async () => ({ content: [] }) },
  );
  assert.equal(td.description, "memory_recent");
  assert.equal(td.promptSnippet, "memory_recent");
});

test("buildToolDefinition stringifies non-text MCP blocks into diagnostic text", async () => {
  const client = {
    callTool: async () => ({
      content: [
        { type: "text", text: "before" },
        { type: "resource", resource: { uri: "memory://x", text: "payload" } },
      ],
    }),
  };
  const td = buildToolDefinition("s", { name: "t", description: "d", inputSchema: { type: "object" } }, client);
  const res = await td.execute("c", {}, undefined);
  assert.match(res.content[0].text, /before/);
  assert.match(res.content[0].text, /resource block/);
  assert.match(res.content[0].text, /memory:\/\/x/);
});

test("buildToolDefinition: MCP isError -> execute THROWS (not a returned isError)", async () => {
  const client = {
    callTool: async () => ({ content: [{ type: "text", text: "boom" }], isError: true }),
  };
  const td = buildToolDefinition("s", { name: "t", description: "d", inputSchema: { type: "object" } }, client);
  await assert.rejects(td.execute("c", {}, undefined), /boom|MCP tool/i);
});

test("buildToolDefinition: callTool rejection (dead server) -> execute THROWS", async () => {
  const client = {
    callTool: async () => {
      throw new Error("transport closed");
    },
  };
  const td = buildToolDefinition("s", { name: "t", description: "d", inputSchema: { type: "object" } }, client);
  await assert.rejects(td.execute("c", {}, undefined), /transport closed|MCP tool/i);
});

function loadRealArbMemoryToolsList() {
  return JSON.parse(readFileSync(ARB_MEMORY_TOOLS_FIXTURE, "utf8"));
}

test("real arb-memory schemas fixture: Type.Unsafe compiles and pi validation rejects bad args", () => {
  const tools = loadRealArbMemoryToolsList();
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["memory_get", "memory_recent", "memory_search"],
  );
  const search = tools.find((t) => t.name === "memory_search");
  assert.ok(search, "memory_search schema is present in real tools/list");

  const checker = Compile(Type.Unsafe(search.inputSchema));
  assert.equal(checker.Check({ query: "x", k: 8 }), true);
  assert.equal(checker.Check({ query: 123 }), false);

  const td = buildToolDefinition("arb-memory-local", search, { callTool: async () => ({ content: [] }) });
  assert.throws(
    () => validateToolArguments(td, { name: td.name, arguments: { query: "x", k: "bad" } }),
    /Validation failed for tool "mcp__arb_memory_local__memory_search"/,
  );
});
