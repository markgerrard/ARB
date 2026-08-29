import sys; sys.path.insert(0, "src")
from agent_redis_bridge.engines.grok_acp import GrokAcpEngine
# Pin to grok-4.5 (NOT the config default 4.6) — proves the pin engages.
for want in ("grok-4.5", "grok-4.6"):
    e = GrokAcpEngine(cwd="/tmp", model=want)
    try:
        e.start()
        print(f"PIN {want}: OK (start() succeeded, model verified={want})")
    except Exception as ex:
        print(f"PIN {want}: FAILED -> {ex}")
    finally:
        try:
            if e.process: e.process.terminate()
        except Exception:
            pass
# Negative control: a bogus model must now RAISE (not silently pass).
e = GrokAcpEngine(cwd="/tmp", model="grok-nonexistent-9")
try:
    e.start()
    print("PIN bogus: WRONG — start() should have raised")
except Exception as ex:
    print(f"PIN bogus: correctly raised -> {type(ex).__name__}: {str(ex)[:80]}")
finally:
    try:
        if e.process: e.process.terminate()
    except Exception:
        pass
