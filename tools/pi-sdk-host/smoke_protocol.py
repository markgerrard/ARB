#!/usr/bin/env python3
"""End-to-end smoke for host.mjs's JSON-RPC protocol layer.

Drives the harness through:
  1. initialize
  2. thread/start  (qwen3-coder-next via OpenRouter)
  3. turn/start    (no-tool prompt; verify final text)
  4. turn/start    (tool prompt; verify tool events + final text)
  5. shutdown

Each request waits for its matching response by id; notifications
between requests are logged but not blocked on.

Requires OPENROUTER_API_KEY in env. Run via:
    set -a && . run/pi-rpc-qwen3codernext.env && set +a && \\
      python3 tools/pi-sdk-host/smoke_protocol.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue


REPO = Path(__file__).resolve().parents[2]
HOST_PATH = Path(__file__).resolve().parent / "host.mjs"
TIMEOUT_INIT = 15
TIMEOUT_THREAD = 30
TIMEOUT_TURN = 90


class Harness:
    def __init__(self, *, event_log: str | None = None) -> None:
        env = os.environ.copy()
        if event_log is not None:
            env["BRIDGE_PI_SDK_EVENT_LOG"] = event_log
        self.proc = subprocess.Popen(
            ["node", str(HOST_PATH)],
            cwd=str(REPO),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self.responses: dict[int, dict] = {}
        self.notifications: Queue[dict] = Queue()
        self.lock = threading.Lock()
        self._next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.write(f"[parse-error] {line!r}\n")
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    self.responses[msg["id"]] = msg
            else:
                self.notifications.put(msg)
                method = msg.get("method", "?")
                params = msg.get("params", {}) or {}
                if method == "turn/textDelta":
                    sys.stderr.write(
                        f"[delta] {params.get('delta', '')!r}\n"
                    )
                else:
                    sys.stderr.write(f"[notify] {method} {json.dumps(params)}\n")

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            sys.stderr.write(f"[host-stderr] {line.rstrip()}\n")

    def request(self, method: str, params: dict, timeout: float) -> dict:
        with self.lock:
            req_id = self._next_id
            self._next_id += 1
        payload = json.dumps({"id": req_id, "method": method, "params": params})
        assert self.proc.stdin is not None
        sys.stderr.write(f"[send] {payload}\n")
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                resp = self.responses.pop(req_id, None)
            if resp is not None:
                if "error" in resp:
                    raise RuntimeError(f"{method} failed: {resp['error']}")
                return resp["result"]
            time.sleep(0.05)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def wait_for_turn_completed(self, turn_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.notifications.get(timeout=min(0.5, deadline - time.monotonic()))
            except Empty:
                continue
            if (
                msg.get("method") == "turn/completed"
                and (msg.get("params") or {}).get("turnId") == turn_id
            ):
                return msg["params"]
        raise TimeoutError(f"turn/completed for {turn_id} did not arrive within {timeout}s")

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=5)
        except Exception as exc:
            sys.stderr.write(f"[shutdown-fail] {exc}\n")
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def main() -> int:
    if "OPENROUTER_API_KEY" not in os.environ:
        sys.stderr.write("OPENROUTER_API_KEY not set; source run/pi-rpc-qwen3codernext.env first\n")
        return 2

    event_log_path = f"/tmp/pi-sdk-smoke-events-{os.getpid()}.ndjson"
    if os.path.exists(event_log_path):
        os.remove(event_log_path)
    h = Harness(event_log=event_log_path)
    try:
        sys.stderr.write("=== initialize ===\n")
        r = h.request(
            "initialize",
            {"clientInfo": {"name": "smoke-py", "version": "0.0.0"}, "capabilities": {}},
            timeout=TIMEOUT_INIT,
        )
        sys.stderr.write(f"[init result] {r}\n")
        assert r.get("serverInfo", {}).get("name") == "pi-sdk-host", r

        sys.stderr.write("=== thread/start ===\n")
        r = h.request(
            "thread/start",
            {
                "cwd": str(REPO),
                "model": "openrouter/qwen/qwen3-coder-next",
                "tools": ["read", "grep", "find", "ls"],
                "thinkingLevel": "off",
            },
            timeout=TIMEOUT_THREAD,
        )
        thread_id = r["thread"]["id"]
        sys.stderr.write(f"[thread] {thread_id}\n")

        sys.stderr.write("=== turn/start (no-tool) ===\n")
        r = h.request(
            "turn/start",
            {"threadId": thread_id, "message": "Reply with exactly: PI_SDK_PROTOCOL_OK"},
            timeout=TIMEOUT_TURN,
        )
        turn_id = r["turn"]["id"]
        completion = h.wait_for_turn_completed(turn_id, timeout=TIMEOUT_TURN)
        sys.stderr.write(f"[turn completion] {json.dumps(completion)}\n")
        assert completion["ok"] is True, completion
        assert "PI_SDK_PROTOCOL_OK" in completion["finalText"], completion
        assert completion["toolCalls"] == 0, completion

        sys.stderr.write("=== turn/start (tool) ===\n")
        r = h.request(
            "turn/start",
            {
                "threadId": thread_id,
                "message": (
                    "Use the ls tool to list the top-level entries in this directory. "
                    "Then reply on a final line with exactly: PI_SDK_TOOL_PROTOCOL_OK"
                ),
            },
            timeout=TIMEOUT_TURN,
        )
        turn_id = r["turn"]["id"]
        completion = h.wait_for_turn_completed(turn_id, timeout=TIMEOUT_TURN)
        sys.stderr.write(f"[turn completion] {json.dumps(completion)}\n")
        assert completion["ok"] is True, completion
        assert "PI_SDK_TOOL_PROTOCOL_OK" in completion["finalText"], completion
        assert completion["toolCalls"] >= 1, completion
        # New field after tri-model rewrite: stopReason from agent_end's
        # AssistantMessage. "stop" for a final answer, "toolUse" if the
        # turn ended on a tool call (shouldn't happen here — we asked for
        # text after the tool), null only if agent_end never fired.
        assert "stopReason" in completion, completion
        assert completion["stopReason"] in ("stop", "toolUse"), completion

        print(json.dumps({"ok": True, "thread": thread_id}))
        return 0
    finally:
        h.close()
        # Verify the event log: must exist, contain valid NDJSON, and
        # include at least one tool_execution_end (proves the canonical
        # raw-SDK event stream made it onto disk, not just our protocol
        # notifications).
        if not os.path.exists(event_log_path):
            sys.stderr.write(f"FAIL: event log not created at {event_log_path}\n")
            return  # noqa: pretty-print would lose return code; raise instead
        with open(event_log_path) as f:
            lines = [line for line in f if line.strip()]
        sys.stderr.write(f"[event-log] {len(lines)} lines at {event_log_path}\n")
        assert len(lines) > 0, f"event log empty: {event_log_path}"
        parsed = [json.loads(line) for line in lines]
        assert any(
            (entry.get("event") or {}).get("type") == "tool_execution_end"
            for entry in parsed
        ), f"no tool_execution_end in event log {event_log_path}"
        # Every entry must have a turnId during a turn (sanity check).
        turn_entries = [
            e for e in parsed
            if (e.get("event") or {}).get("type") in {"message_update", "tool_execution_start", "tool_execution_end", "agent_end"}
        ]
        assert all(e.get("turnId") for e in turn_entries), \
            "event log entries during a turn missing turnId"


if __name__ == "__main__":
    raise SystemExit(main())
