import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createLineReader } from "./line-reader.mjs";

test("newline reader joins fragmented socket data", async () => {
  const socket = new EventEmitter();
  const reader = createLineReader(socket);
  const line = reader.readLine();
  socket.emit("data", Buffer.from('{"ok":'));
  socket.emit("data", Buffer.from('true}\n'));
  assert.equal(await line, '{"ok":true}');
  reader.close();
});

test("newline reader preserves coalesced socket data for subsequent reads", async () => {
  const socket = new EventEmitter();
  const reader = createLineReader(socket);
  socket.emit("data", Buffer.from("first\nsecond\nremainder"));
  assert.equal(await reader.readLine(), "first");
  assert.equal(await reader.readLine(), "second");
  const third = reader.readLine();
  socket.emit("data", Buffer.from("\n"));
  assert.equal(await third, "remainder");
  reader.close();
});
