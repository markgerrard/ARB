# Spike: drive the real AgentSdkEngine with a 60s run_in_background Bash task and confirm the
# bridge turn holds at the interim ResultMessage (turn_continued) and returns the post-notification
# result. Observed 2026-08-24 (macOS, haiku-4.5 subscription seat): one turn, tool_calls=2,
# result "FINAL: SPIKE_DONE" at 67.5s. Needs CLAUDE_CODE_OAUTH_TOKEN + SEAT_ENABLED=1 in env.
# Usage: .venv/bin/python <this> <scratch-cwd>   Record: ARB Memory art-d17b2c72afaf7b15 v2.

import sys, time, json
from pathlib import Path
from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
cwd = sys.argv[1]
T0 = time.time()
def on_event(kind, payload):
    if kind in ("turn_started","turn_continued","turn_completed","turn_timeout","command_started"):
        print(f"{time.time()-T0:6.1f}s {kind:16} {json.dumps({k:payload.get(k) for k in ('command','pending_tasks','ok','stop_reason')})}", flush=True)
eng = AgentSdkEngine(cwd=cwd, model="haiku-4.5", tool_ceiling="Read,Bash", key="", session_root=Path(cwd)/"sess", startup_probe=False)
eng.start()
try:
    r = eng.run_turn_with_progress(
        "Run exactly this shell command using the Bash tool with run_in_background=true: "
        "`sleep 60 && echo SPIKE_DONE`. Then immediately reply with the single word STARTED and stop. "
        "Do not poll or wait. When you are later notified the task completed, read its output file and reply with "
        "'FINAL: ' followed by the output.", timeout=300, policy="trusted", on_event=on_event)
    print(f"{time.time()-T0:6.1f}s RESULT ok={r.ok} tool_calls={r.tool_calls} stop={r.stop_reason} err={r.error}\n  result={r.result!r}")
finally:
    eng.stop()
