# Probe: does the bundled CLI re-invoke the model when a run_in_background Bash task completes
# inside one ClaudeSDKClient session? Observed 2026-08-24 (macOS, SDK 0.2.117): YES — a second
# init/AssistantMessage/ResultMessage arrives unprompted ~0.2s after task_notification.
# Record: ARB Memory art-d17b2c72afaf7b15 v2. Usage: .venv/bin/python <this> <cwd>

import asyncio, time, json, sys
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage, AssistantMessage, SystemMessage

def log(tag, msg):
    extra = {k: getattr(msg, k) for k in ("subtype","task_id","status","summary","output_file","patch") if hasattr(msg, k)}
    if isinstance(msg, AssistantMessage):
        extra["blocks"] = [getattr(b,"name",None) or getattr(b,"text","")[:80] for b in msg.content]
    print(f"{time.time()-T0:7.1f}s {tag:6} {type(msg).__name__} {json.dumps(extra, default=str)[:300]}", flush=True)

async def main():
    global T0
    opts = ClaudeAgentOptions(permission_mode="bypassPermissions", cwd=sys.argv[1], max_turns=6,
                              allowed_tools=["Bash"])
    async with ClaudeSDKClient(opts) as c:
        T0 = time.time()
        await c.query("Run exactly this shell command using the Bash tool with run_in_background=true: "
                      "`sleep 45 && echo PROBE_DONE`. Then immediately reply with the single word STARTED and stop. "
                      "Do not poll, do not wait, do not run any other command.")
        async for m in c.receive_response():
            log("TURN1", m)
        print("--- ResultMessage seen; now listening on receive_messages() for 100s ---", flush=True)
        try:
            async with asyncio.timeout(100):
                async for m in c.receive_messages():
                    log("AFTER", m)
        except TimeoutError:
            print("--- 100s listen window closed ---", flush=True)
asyncio.run(main())
