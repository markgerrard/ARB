"""Measure GitHub repo-activity-API detection latency.

Answers a design question from a companion costing note: how long after a ref
actually moves does the activity API report the event?

Method. Snapshot the set of known event ids, perform the mutation, capture a
monotonic clock reading the instant the git/gh command returns, then poll the
activity API until an event appears whose id is new AND whose activity_type
matches what was performed. Each poll records its own start and return time, so
the reported latency is a BRACKET -- the true detection moment lies between
(poll_start - t_action) and (poll_return - t_action). Reporting only the poll
return would overstate latency by up to one API call duration (~0.3s).
"""
import json
import subprocess
import sys
import time

REPO = "example-org/arb-scratch-activity-latency-probe"
WORK = sys.argv[1]
POLL_TIMEOUT = 180.0


def sh(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p


def activity(per_page=30):
    p = sh(["gh", "api", f"repos/{REPO}/activity?per_page={per_page}"], check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def known_ids():
    evs = activity() or []
    return {e["id"] for e in evs}


def wait_for(seen, want_type, t_action):
    """Poll until a new event of want_type shows up. Returns a latency bracket."""
    polls = 0
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        evs = activity()
        t1 = time.monotonic()
        polls += 1
        if evs is None:
            continue
        for e in evs:
            if e["id"] not in seen and e["activity_type"] == want_type:
                return {
                    "detected": True,
                    "lo": round(t0 - t_action, 3),
                    "hi": round(t1 - t_action, 3),
                    "polls": polls,
                    "server_ts": e["timestamp"],
                    "ref": e["ref"],
                    "after": e["after"][:8],
                    "actor": e["actor"]["login"],
                }
    return {"detected": False, "polls": polls, "timeout_s": POLL_TIMEOUT}


results = []

# ---- setup: init repo, first push (also establishes the default branch) ----
sh(["git", "init", "-q", "-b", "main", WORK])
sh(["git", "-C", WORK, "remote", "add", "origin",
    f"git@github.com:{REPO}.git"])
open(f"{WORK}/README.md", "w").write("throwaway: activity-api latency probe\n")
sh(["git", "-C", WORK, "add", "-A"])
sh(["git", "-C", WORK, "commit", "-qm", "initial"])

seen = known_ids()
sh(["git", "-C", WORK, "push", "-q", "-u", "origin", "main"])
t = time.monotonic()
r = wait_for(seen, "push", t)
r["trial"] = "push-initial"
results.append(r)
print(f"push-initial: {r}", flush=True)

# ---- push trials ----
for i in range(1, 6):
    open(f"{WORK}/f{i}.txt", "w").write(f"trial {i}\n")
    sh(["git", "-C", WORK, "add", "-A"])
    sh(["git", "-C", WORK, "commit", "-qm", f"push trial {i}"])
    seen = known_ids()
    sh(["git", "-C", WORK, "push", "-q", "origin", "main"])
    t = time.monotonic()
    r = wait_for(seen, "push", t)
    r["trial"] = f"push-{i}"
    results.append(r)
    print(f"push-{i}: {r}", flush=True)

# ---- pr_merge trials ----
for i in range(1, 3):
    br = f"pr-trial-{i}"
    sh(["git", "-C", WORK, "checkout", "-q", "-b", br])
    open(f"{WORK}/pr{i}.txt", "w").write(f"pr trial {i}\n")
    sh(["git", "-C", WORK, "add", "-A"])
    sh(["git", "-C", WORK, "commit", "-qm", f"pr trial {i}"])
    sh(["git", "-C", WORK, "push", "-q", "-u", "origin", br])
    sh(["gh", "pr", "create", "--repo", REPO, "--base", "main", "--head", br,
        "--title", f"pr trial {i}", "--body", "throwaway"], cwd=WORK)
    # GitHub needs a moment to compute mergeability; retry rather than guess.
    for attempt in range(20):
        seen = known_ids()
        p = sh(["gh", "pr", "merge", br, "--repo", REPO, "--merge"],
               cwd=WORK, check=False)
        t = time.monotonic()
        if p.returncode == 0:
            break
        time.sleep(1.0)
    else:
        results.append({"trial": f"pr_merge-{i}", "detected": False,
                        "error": "merge never became possible"})
        continue
    r = wait_for(seen, "pr_merge", t)
    r["trial"] = f"pr_merge-{i}"
    r["merge_attempts"] = attempt + 1
    results.append(r)
    print(f"pr_merge-{i}: {r}", flush=True)
    sh(["git", "-C", WORK, "checkout", "-q", "main"])
    sh(["git", "-C", WORK, "pull", "-q", "--ff-only", "origin", "main"],
       check=False)

# ---- force_push trial ----
sh(["git", "-C", WORK, "checkout", "-q", "-B", "force-trial"])
open(f"{WORK}/force.txt", "w").write("a\n")
sh(["git", "-C", WORK, "add", "-A"])
sh(["git", "-C", WORK, "commit", "-qm", "force base"])
sh(["git", "-C", WORK, "push", "-q", "-u", "origin", "force-trial"])
time.sleep(2)
open(f"{WORK}/force.txt", "w").write("b\n")
sh(["git", "-C", WORK, "add", "-A"])
sh(["git", "-C", WORK, "commit", "-q", "--amend", "-m", "force rewritten"])
seen = known_ids()
sh(["git", "-C", WORK, "push", "-q", "--force", "origin", "force-trial"])
t = time.monotonic()
r = wait_for(seen, "force_push", t)
r["trial"] = "force_push-1"
results.append(r)
print(f"force_push-1: {r}", flush=True)

print("\n=== RAW ===")
print(json.dumps(results, indent=2))

ok = [r for r in results if r.get("detected")]
print(f"\n=== SUMMARY === detected {len(ok)}/{len(results)}")
if ok:
    los = [r["lo"] for r in ok]
    his = [r["hi"] for r in ok]
    print(f"latency bracket across all trials: {min(los):.3f}s .. {max(his):.3f}s")
    print(f"worst-case lower bound: {max(los):.3f}s")
    print(f"polls needed: min={min(r['polls'] for r in ok)} max={max(r['polls'] for r in ok)}")
for r in results:
    if not r.get("detected"):
        print(f"NOT DETECTED: {r}")
