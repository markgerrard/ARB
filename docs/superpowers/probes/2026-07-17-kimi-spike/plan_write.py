import sys, os
sys.path.insert(0, '/Users/<user>/<workspace>/scratchpad')
sys.argv = ['kimi_spike.py', 'none']
import kimi_spike as K

if os.path.exists(K.WRITE_PROBE):
    os.remove(K.WRITE_PROBE)

r = K.run("plan", K.WRITE_STIM, K.CANCEL, "plan + OUT-OF-CWD WRITE  [decisive]")

print("\n=== VERDICT plan/out-of-cwd-write ===")
print("stopReason  :", r["stopReason"])
print("tool_calls  :", r["tool_calls"])
print("asks raised :", r["asks"])
print("file exists :", os.path.exists(K.WRITE_PROBE))
if os.path.exists(K.WRITE_PROBE):
    print("content     :", repr(open(K.WRITE_PROBE).read()))
    os.remove(K.WRITE_PROBE)
print("agent text  :", repr(r["text"][:400]))
print("DONE-COMPLETE")
