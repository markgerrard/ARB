import test from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BROKER = fileURLToPath(new URL("./cell-broker.mjs", import.meta.url));

test("stdin EOF destroys the broker connection and exits cleanly", async () => {
  let connectionClosed = false;
  const root = mkdtempSync(join(tmpdir(), "pi-cell-broker-"));
  const socketPath = join(root, "broker.sock");
  const server = net.createServer((socket) => {
    socket.once("data", () => socket.write('{"ok":true,"identity":"cell/attempt"}\n'));
    socket.once("close", () => { connectionClosed = true; });
  });
  server.listen(socketPath);
  await once(server, "listening");
  const child = spawn(process.execPath, [BROKER], {
    env: {
      ...process.env,
      PI_SDK_BROKER_SOCKET: socketPath,
      PI_SDK_BROKER_TOKEN: "token",
      PI_SDK_BROKER_IDENTITY: "cell/attempt",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  await once(server, "connection");
  child.stdin.end();
  const [code] = await once(child, "close");
  server.close();
  await once(server, "close");
  rmSync(root, { recursive: true, force: true });
  assert.equal(code, 0);
  assert.equal(connectionClosed, true);
});
