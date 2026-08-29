#!/usr/bin/env python3
"""GROK-1 V1 protocol probe.

Drives `grok agent stdio` over raw JSON-RPC (does NOT use GrokAcpEngine) so we
control the exact reply shape to session/request_permission. Logs every line
both directions to a per-run JSONL, captures stderr, and records whether the
permission callback ever arrives and whether the file gets written.

Usage: python3 grok_probe.py <run_label>
Run labels: A B C D E E2 F  (see __main__)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

GROK = "/Users/<user>/.local/bin/grok"
SCRATCH = os.path.dirname(os.path.abspath(__file__))
OUTDIR = "/Users/<user>/<workspace>/docs/superpowers/probes/2026-07-10-grok1-v1-probe"
REPO = os.path.join(SCRATCH, "probe-repo")
OUT_OF_CWD = os.path.join(SCRATCH, "out-of-cwd")
SIBLING = os.path.join(SCRATCH, "sibling-read")

os.makedirs(OUTDIR, exist_ok=True)


class Probe:
    def __init__(self, label: str):
        self.label = label
        self.jsonl_path = os.path.join(OUTDIR, f"run-{label}.jsonl")
        self.stderr_path = os.path.join(OUTDIR, f"run-{label}.stderr.txt")
        self.log_f = open(self.jsonl_path, "w")
        self.stderr_f = open(self.stderr_path, "w")
        self.proc: subprocess.Popen | None = None
        self.next_id = 1
        self.lock = threading.Lock()
        self.inbox: list[dict] = []
        self.inbox_lock = threading.Lock()
        self.inbox_cv = threading.Condition(self.inbox_lock)
        self.permission_asks: list[dict] = []  # full params of every session/request_permission
        self.session_id: str | None = None

    def log(self, direction: str, msg):
        rec = {"ts": time.time(), "dir": direction, "msg": msg}
        self.log_f.write(json.dumps(rec) + "\n")
        self.log_f.flush()

    def start(self):
        self.proc = subprocess.Popen(
            [GROK, "agent", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=REPO,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                val = json.loads(line)
            except json.JSONDecodeError:
                self.log("in-raw", line[:500])
                continue
            self.log("in", val)
            with self.inbox_cv:
                self.inbox.append(val)
                self.inbox_cv.notify_all()

    def _read_stderr(self):
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_f.write(line)
            self.stderr_f.flush()

    def _send(self, payload: dict):
        assert self.proc and self.proc.stdin
        self.log("out", payload)
        with self.lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()

    def send_request(self, method: str, params: dict) -> int:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        self._send(payload)

    def wait_for_response(self, rid: int, timeout: float, permission_handler=None):
        """Wait for our own request `rid` to get a response. While waiting, handle
        any server-initiated requests (permission asks) via permission_handler.
        Returns (result_or_none, error_or_none, 'ok'|'timeout')."""
        deadline = time.monotonic() + timeout
        idx = 0
        while time.monotonic() < deadline:
            with self.inbox_cv:
                while idx >= len(self.inbox):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, None, "timeout"
                    self.inbox_cv.wait(timeout=min(1.0, remaining))
                msg = self.inbox[idx]
                idx += 1
            # a response to our request
            if msg.get("id") == rid and "method" not in msg:
                return msg.get("result"), msg.get("error"), "ok"
            # a server-initiated request
            if "method" in msg and "id" in msg:
                self._dispatch_server_request(msg, permission_handler)
        return None, None, "timeout"

    def wait_for_prompt_completion(self, prompt_id: int, timeout: float, permission_handler=None):
        """Wait for the session/prompt response (the turn end). Handles server
        requests inline. Returns (result, error, status)."""
        return self.wait_for_response(prompt_id, timeout, permission_handler)

    def _dispatch_server_request(self, msg: dict, permission_handler):
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            self.permission_asks.append(params)
            if permission_handler is not None:
                permission_handler(self, rid, params)
            else:
                # default: cancel
                self.respond(rid, result={"outcome": {"outcome": "cancelled"}})
        else:
            # unknown server method -> -32601 (mirror engine default), except we
            # must still respond so grok isn't left hanging
            self.respond(rid, error={"code": -32601, "message": f"probe: unsupported {method}"})

    def initialize(self, fs_read=False, fs_write=False):
        rid = self.send_request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "grok1-probe", "version": "0.1.0"},
            "clientCapabilities": {
                "auth": {"terminal": False},
                "fs": {"readTextFile": fs_read, "writeTextFile": fs_write},
                "terminal": False,
            },
        })
        result, error, status = self.wait_for_response(rid, 20)
        self.log("summary", {"initialize": {"result": result, "error": error, "status": status}})
        return result

    def session_new(self):
        rid = self.send_request("session/new", {"cwd": REPO, "mcpServers": []})
        result, error, status = self.wait_for_response(rid, 30)
        self.log("summary", {"session/new": {"result": result, "error": error, "status": status}})
        if result and isinstance(result.get("sessionId"), str):
            self.session_id = result["sessionId"]
        # capture advertised modes
        modes = None
        if result:
            modes = result.get("modes") or result.get("availableModes")
        self.log("summary", {"advertised_modes": modes, "full_session_new_result_keys": list(result.keys()) if result else None})
        return result

    def set_mode(self, mode_id: str):
        rid = self.send_request("session/set_mode", {"sessionId": self.session_id, "modeId": mode_id})
        result, error, status = self.wait_for_response(rid, 15)
        self.log("summary", {"set_mode": {"modeId": mode_id, "result": result, "error": error, "status": status}})
        return result, error

    def prompt(self, text: str, timeout: float, permission_handler=None):
        rid = self.send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        })
        result, error, status = self.wait_for_prompt_completion(rid, timeout, permission_handler)
        self.log("summary", {"prompt_result": {"result": result, "error": error, "status": status,
                                               "num_permission_asks": len(self.permission_asks)}})
        return result, error, status

    def close(self):
        try:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        self.log_f.close()
        self.stderr_f.close()


# ---- permission handlers (reply shapes under test) ----

def handler_approved(probe: Probe, rid, params):
    """Run A: CURRENT bridge shape."""
    probe.respond(rid, result={"outcome": {"outcome": "approved"}})


def _pick_allow_option(params: dict):
    options = params.get("options")
    if not isinstance(options, list):
        return None
    valid = [o for o in options if isinstance(o, dict) and isinstance(o.get("optionId"), str)]
    for pref in ("allow_once", "allow_always"):
        for o in valid:
            if o.get("kind") == pref:
                return o["optionId"]
    for o in valid:
        tok = f"{o['optionId']} {o.get('kind') or ''}".lower()
        if "allow" in tok and not any(m in tok for m in ("disallow", "reject", "deny", "cancel", "not")):
            return o["optionId"]
    return None


def handler_selected(probe: Probe, rid, params):
    """Run B / E: spec-correct accept via selected+optionId."""
    opt = _pick_allow_option(params)
    probe.log("summary", {"picked_allow_option": opt, "offered_options": params.get("options")})
    if opt is not None:
        probe.respond(rid, result={"outcome": {"outcome": "selected", "optionId": opt}})
    else:
        probe.respond(rid, result={"outcome": {"outcome": "cancelled"}})


def handler_cancelled(probe: Probe, rid, params):
    """Run C: spec-correct deny."""
    probe.respond(rid, result={"outcome": {"outcome": "cancelled"}})


def handler_error(probe: Probe, rid, params):
    """Run D: JSON-RPC error reply (fail-closed check)."""
    probe.respond(rid, error={"code": -32601, "message": "test"})


# ---- run definitions ----

def sentinel_path(label):
    return os.path.join(OUT_OF_CWD, f"probe-{label}.txt")


def clean_sentinel(label):
    p = sentinel_path(label)
    if os.path.exists(p):
        os.remove(p)
    return p


def run(label, *, modes, handler, task=None, fs_read=False, fs_write=False, timeout=180):
    p = Probe(label)
    p.start()
    p.initialize(fs_read=fs_read, fs_write=fs_write)
    p.session_new()
    for m in modes:
        res, err = p.set_mode(m)
        if err is None:
            p.log("summary", {"mode_accepted": m})
            break
        else:
            p.log("summary", {"mode_rejected": m, "error": err})
    sp = clean_sentinel(label)
    if task is None:
        task = f"Write the single word 'probe' to the file {sp} . Use whatever tool is needed. Then stop."
    result, error, status = p.prompt(task, timeout, permission_handler=handler)
    file_written = os.path.exists(sp)
    file_content = None
    if file_written:
        try:
            with open(sp) as f:
                file_content = f.read()
        except Exception:
            pass
    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
    summary = {
        "label": label,
        "status": status,
        "stopReason": stop_reason,
        "error": error,
        "num_permission_asks": len(p.permission_asks),
        "permission_ask_methods_seen": True if p.permission_asks else False,
        "file_written": file_written,
        "file_content": file_content,
        "sentinel_path": sp,
    }
    p.log("summary", {"RUN_SUMMARY": summary})
    p.close()
    print(json.dumps(summary, indent=2))
    return summary


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "A"
    if label == "A":
        run("A", modes=["yolo", "bypassPermissions", "acceptEdits"], handler=handler_approved)
    elif label == "B":
        run("B", modes=["yolo", "bypassPermissions", "acceptEdits"], handler=handler_selected)
    elif label == "C":
        run("C", modes=["yolo", "bypassPermissions", "acceptEdits"], handler=handler_cancelled)
    elif label == "D":
        run("D", modes=["yolo", "bypassPermissions", "acceptEdits"], handler=handler_error)
    elif label == "E":
        # default mode, out-of-cwd write, spec-correct accept
        run("E", modes=["default"], handler=handler_selected)
    elif label == "E2":
        # default mode, sibling read (worktree-style out-of-cwd read)
        sib = os.path.join(SIBLING, "sibling.txt")
        run("E2", modes=["default"], handler=handler_selected,
            task=f"Read the file {sib} and tell me its exact contents. Then stop.")
    elif label == "F":
        # fs capabilities declared true at initialize, yolo, spec-correct accept
        run("F", modes=["yolo", "bypassPermissions", "acceptEdits"], handler=handler_selected,
            fs_read=True, fs_write=True)
    else:
        print(f"unknown run {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
