import test from "node:test";
import assert from "node:assert/strict";

test("typebox resolves directly and exposes Type.Unsafe", async () => {
  const { Type } = await import("typebox");
  assert.equal(typeof Type.Unsafe, "function");
});

test("@modelcontextprotocol/sdk client + stdio transport resolve", async () => {
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StdioClientTransport } = await import("@modelcontextprotocol/sdk/client/stdio.js");
  assert.equal(typeof Client, "function");
  assert.equal(typeof StdioClientTransport, "function");
});
