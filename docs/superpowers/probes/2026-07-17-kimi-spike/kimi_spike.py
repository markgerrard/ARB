"""Throwaway spike harness: drive `kimi acp` with REAL prompts to settle UV-1..UV-10.

NOT production code. Answers the behavioural questions v3 defers to Gate 1:
  UV-1/UV-7  does `auto` let reads through? (is the 6-week-old E13 docstring still true?)
  UV-2       does `auto` auto-approve a write?
  UV-3       what does kimi's session/request_permission actually offer? (options[].kind)
  UV-5       does replying reject_once let the turn SURVIVE, or kill it?
  UV-6       does `plan` mode permit reads?
  UV-10      cold-spawn wall time

Usage: .venv/bin/python scratchpad/kimi_spike.py <mode>
"""
import json, subprocess, sys, threading, queue, time, os

KIMI = "/Users/<user>/.kimi-code/bin/kimi"
CWD = "/Users/<user>/<workspace>"
WRITE_PROBE = "/tmp/kimi-gate-write-probe.txt"


class Acp:
    def __init__(self, permission_policy):
        """permission_policy: fn(params) -> dict outcome. Called on every ask."""
        self.policy = permission_policy
        self.asks = []          # raw permission ask payloads (UV-3 evidence)
        self.updates = []       # session/update notifications
        self.tool_calls = []
        self.text = []
        t0 = time.monotonic()
        self.p = subprocess.Popen(
            [KIMI, "acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=CWD)
        self.spawn_s = time.monotonic() - t0
        self.q: queue.Queue = queue.Queue()
        self._id = 0
        threading.Thread(target=self._rd, daemon=True).start()
        threading.Thread(target=self._rde, daemon=True).start()

    def _rd(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Inbound REQUEST from the agent (has method AND id) -> we must reply.
            if "method" in m and "id" in m:
                self._handle_request(m)
            elif "method" in m:
                self._handle_notify(m)
            else:
                self.q.put(m)

    def _rde(self):
        for line in self.p.stderr:
            sys.stderr.write("[stderr] " + line)

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _handle_request(self, m):
        method = m.get("method")
        if method == "session/request_permission":
            params = m.get("params") or {}
            self.asks.append(params)
            print("\n  !! PERMISSION ASK (raw):")
            print("  " + json.dumps(params, indent=2)[:2000].replace("\n", "\n  "))
            outcome = self.policy(params)
            print(f"  -> replying: {json.dumps(outcome)}")
            self._send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": outcome}})
        else:
            # Unknown inbound request: refuse explicitly rather than hang the turn.
            self._send({"jsonrpc": "2.0", "id": m["id"],
                        "error": {"code": -32601, "message": f"unhandled {method}"}})

    def _handle_notify(self, m):
        if m.get("method") == "session/update":
            u = (m.get("params") or {}).get("update") or {}
            kind = u.get("sessionUpdate")
            self.updates.append(kind)
            if kind == "tool_call":
                self.tool_calls.append(u)
                print(f"  [tool_call] {u.get('title') or u.get('kind')}")
            elif kind == "agent_message_chunk":
                c = (u.get("content") or {}).get("text") or ""
                self.text.append(c)

    def req(self, method, params, timeout=120):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                m = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if m.get("id") == rid:
                return m
        return {"error": {"message": "TIMEOUT"}}


def run(mode, stimulus, policy, label, prompt_timeout=180):
    print(f"\n{'='*70}\n### {label}  (mode={mode})\n{'='*70}")
    a = Acp(policy)
    print(f"cold-spawn: {a.spawn_s:.2f}s   [UV-10]")
    a.req("initialize", {"protocolVersion": 1,
                         "clientInfo": {"name": "kimi-spike", "version": "0.0.1"},
                         "clientCapabilities": {"auth": {"terminal": False},
                                                "fs": {"readTextFile": False,
                                                       "writeTextFile": False},
                                                "terminal": False}})
    r = a.req("session/new", {"cwd": CWD, "mcpServers": []})
    sid = (r.get("result") or {}).get("sessionId")
    if not sid:
        print("FAIL: no sessionId", json.dumps(r)[:400])
        a.p.terminate()
        return
    sm = a.req("session/set_mode", {"sessionId": sid, "modeId": mode})
    print(f"set_mode {mode}: {json.dumps(sm)[:200]}")

    t0 = time.monotonic()
    pr = a.req("session/prompt",
               {"sessionId": sid, "prompt": [{"type": "text", "text": stimulus}]},
               timeout=prompt_timeout)
    dur = time.monotonic() - t0
    stop = (pr.get("result") or {}).get("stopReason")
    print(f"\n--- RESULT ({dur:.1f}s)")
    print(f"stopReason : {stop}")
    print(f"tool_calls : {len(a.tool_calls)}")
    print(f"asks       : {len(a.asks)}")
    print(f"text       : {''.join(a.text)[:600]!r}")
    if pr.get("error"):
        print(f"error      : {json.dumps(pr['error'])[:300]}")
    a.p.terminate()
    return {"stopReason": stop, "tool_calls": len(a.tool_calls),
            "asks": len(a.asks), "text": "".join(a.text), "spawn_s": a.spawn_s}


ALLOW = lambda p: {"outcome": "selected", "optionId": _pick(p, ("allow_once", "allow_always"))}
CANCEL = lambda p: {"outcome": "cancelled"}


def _pick(params, kinds):
    for k in kinds:
        for o in params.get("options") or []:
            if o.get("kind") == k:
                return o["optionId"]
    return (params.get("options") or [{}])[0].get("optionId")


def REJECT(p):
    """The shape v3 says exists nowhere in the repo: select a reject_once option."""
    oid = _pick(p, ("reject_once", "reject_always"))
    if oid is None:
        print("  !! no reject option offered -> falling back to cancelled [UV-3 answer: NO]")
        return {"outcome": "cancelled"}
    return {"outcome": "selected", "optionId": oid}


READ_STIM = ("Read the file src/agent_redis_bridge/engines/_acp.py and reply with the exact "
             "text of line 28 and nothing else.")
WRITE_STIM = f"Write the single word probe to the file {WRITE_PROBE}"

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if os.path.exists(WRITE_PROBE):
        os.remove(WRITE_PROBE)

    if which in ("all", "1A"):
        run("auto", READ_STIM, CANCEL, "Gate 1.A — does `auto` give READS? [UV-1,UV-7]")

    if which in ("all", "1B"):
        run("auto", WRITE_STIM, CANCEL, "Gate 1.B — does `auto` ASK on out-of-cwd write? [UV-2,UV-3]")
        print(f"\nwrite probe exists? {os.path.exists(WRITE_PROBE)}  "
              f"(True => HARD FAIL, seat is write-capable)")
        if os.path.exists(WRITE_PROBE):
            os.remove(WRITE_PROBE)

    if which in ("all", "1C"):
        run("auto", WRITE_STIM, REJECT, "Gate 1.C — does reject_once let the turn SURVIVE? [UV-5]")
        print(f"\nwrite probe exists? {os.path.exists(WRITE_PROBE)}  (True => HARD FAIL)")
        if os.path.exists(WRITE_PROBE):
            os.remove(WRITE_PROBE)

    if which in ("all", "1D"):
        run("plan", READ_STIM, CANCEL, "Gate 1.D — does `plan` give READS? [UV-6]")
