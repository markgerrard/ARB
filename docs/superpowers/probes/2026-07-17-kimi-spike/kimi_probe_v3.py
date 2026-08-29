"""Handshake-only probe of `kimi acp`. NO session/prompt is ever sent."""
import json, subprocess, sys, threading, queue, time

CMD = ["/Users/<user>/.kimi-code/bin/kimi", "acp"]
p = subprocess.Popen(CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1,
                     cwd="/Users/<user>/<workspace>")
q: queue.Queue = queue.Queue()

def rd():
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            q.put(json.loads(line))
        except json.JSONDecodeError:
            pass
threading.Thread(target=rd, daemon=True).start()
def rde():
    for line in p.stderr:
        sys.stderr.write("[stderr] " + line)
threading.Thread(target=rde, daemon=True).start()

_id = [0]
def req(method, params, timeout=40):
    _id[0] += 1
    rid = _id[0]
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            m = q.get(timeout=1)
        except queue.Empty:
            continue
        if m.get("id") == rid and "method" not in m:
            return m
        print("  [async]", json.dumps(m)[:200])
    return {"error": {"message": "TIMEOUT"}}

print("=== initialize ===")
r = req("initialize", {"protocolVersion": 1,
                       "clientInfo": {"name": "agent-redis-bridge", "version": "0.1.0"},
                       "clientCapabilities": {"auth": {"terminal": False},
                                              "fs": {"readTextFile": False, "writeTextFile": False},
                                              "terminal": False}})
print(json.dumps(r, indent=2)[:3000])

print("\n=== session/new ===")
r = req("session/new", {"cwd": "/Users/<user>/<workspace>", "mcpServers": []})
print(json.dumps(r, indent=2)[:6000])
sid = (r.get("result") or {}).get("sessionId")
print("sessionId:", sid)

if sid:
    for mode in ["plan", "auto", "default", "yolo", "bogus-mode-xyz"]:
        print(f"\n=== session/set_mode {mode} ===")
        print(json.dumps(req("session/set_mode", {"sessionId": sid, "modeId": mode}))[:900])
    for model in ["kimi-code/k3", "kimi-code/nope"]:
        print(f"\n=== session/set_model {model} ===")
        print(json.dumps(req("session/set_model", {"sessionId": sid, "modelId": model}))[:900])
    print("\n=== back to auto ===")
    print(json.dumps(req("session/set_mode", {"sessionId": sid, "modeId": "auto"}))[:500])

p.terminate()
