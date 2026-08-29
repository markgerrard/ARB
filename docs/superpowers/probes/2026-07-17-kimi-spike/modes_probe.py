"""Probe `yolo` and `default` behaviour — luna's r3 P0-2.

v4 still describes yolo as "write-capable" and default as "empirically inert" from a
VENDOR LABEL and a 6-week-old docstring, in a document whose thesis is that label-derived
claims killed this arc. Neither has been probed by anyone. Close it with evidence.
"""
import sys, os, hashlib, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv = ['x', 'none']
import kimi_spike as K

WT = "/Users/<user>/<workspace>/.claude/worktrees/kimi-modes-probe"
K.CWD = WT
TRACKED = os.path.join(WT, "README.md")
STIM = "Edit README.md in the current directory: append a new final line containing exactly the word probe."

def sha(p): return hashlib.sha256(open(p,"rb").read()).hexdigest()[:16]
def reset(): subprocess.run(["git","-C",WT,"checkout","--","README.md"],capture_output=True,check=False)

def probe(mode):
    reset(); b = sha(TRACKED)
    r = K.run(mode, STIM, K.CANCEL, f"{mode} + MODIFY tracked (in-cwd)", prompt_timeout=300)
    a = sha(TRACKED); mut = a != b
    print(f"\n=== {mode}: mutated={mut} asks={r['asks']} stop={r['stopReason']} tools={r['tool_calls']}")
    print(f"    text: {r['text'][:180]!r}")
    reset()
    return (mode, mut, r['asks'], r['stopReason'])

if __name__ == "__main__":
    reset(); b = sha(TRACKED); open(TRACKED,"a").write("probe\n")
    assert sha(TRACKED) != b, "DETECTOR BLIND"; reset(); assert sha(TRACKED) == b, "RESET BROKEN"
    print("detector self-test: PASS\n")
    out = [probe("default"), probe("yolo")]
    print("\n" + "="*56 + "\nMODES SUMMARY\n" + "="*56)
    for m, mut, a, s in out:
        print(f"  {m:8s} mutated={str(mut):5s} asks={a} stop={s}")
    print("MODES-PROBE-COMPLETE")
