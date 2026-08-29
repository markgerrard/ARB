#!/usr/bin/env python3
"""Soak: does a long-lived codex app-server leak resources across many discarded threads?

The repo-agnostic worker pool (docs/repo-agnostic-worker-pool.md) keeps ONE app-server warm and
starts a fresh thread per job (cold context), never tearing the old thread down. This probe checks
whether those abandoned threads accumulate RSS / open file descriptors / OS threads in the
app-server process over N cycles.

Each cycle: thread/start (new thread) -> one trivial text turn -> discard (don't reuse). Samples the
app-server process's VmRSS, open-fd count, and task(thread) count each cycle.

NOTE: spawns a LIVE codex app-server (needs `codex` on PATH) and runs N real turns -> costs tokens.
Trivial single-word turns keep it cheap. Read-only: no edits, no tool calls requested.

Usage: python scripts/codex_thread_soak.py [N]   (default N=30)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from agent_redis_bridge.engines.codex import CodexEngine  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CWD = REPO_ROOT
PROMPT = "Reply with exactly the two characters: OK"


def sample(pid):
    rss = 0
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])  # kB
                break
    fds = len(os.listdir(f"/proc/{pid}/fd"))
    threads = len(os.listdir(f"/proc/{pid}/task"))
    return rss, fds, threads


def main():
    eng = CodexEngine(
        cwd=CWD, model="gpt-5.5", approval_policy="never",
        sandbox="workspace-write", bypass_approvals_and_sandbox=True,
    )
    eng.start()
    pid = eng.process.pid
    base = sample(pid)
    print(f"app-server pid={pid}  N={N}", flush=True)
    print(f"baseline: rss={base[0]}kB fds={base[1]} os_threads={base[2]}", flush=True)
    print(f"{'iter':>4} {'rss_kB':>9} {'d_rss':>8} {'fds':>5} {'os_thr':>7} ok", flush=True)

    rss_series = []
    fails = 0
    for i in range(1, N + 1):
        eng.cwd = CWD
        resp = eng.request("thread/start", eng.thread_start_params(), timeout=30)
        eng.thread_id = resp["thread"]["id"]
        r = eng.run_turn_with_progress(PROMPT, timeout=60, policy="trusted", on_event=None)
        if not r.ok:
            fails += 1
        rss, fds, threads = sample(pid)
        rss_series.append(rss)
        print(f"{i:>4} {rss:>9} {rss - base[0]:>+8} {fds:>5} {threads:>7} {r.ok}", flush=True)

    fin = sample(pid)
    k = max(1, N // 6)
    first_avg = sum(rss_series[:k]) / k
    last_avg = sum(rss_series[-k:]) / k
    print("\nSUMMARY", flush=True)
    print(f"threads created/discarded: {N}  (turn failures: {fails})", flush=True)
    print(f"rss  base={base[0]}kB final={fin[0]}kB  delta={fin[0] - base[0]:+}kB"
          f"  ({(fin[0] - base[0]) / N:+.1f} kB/thread)", flush=True)
    print(f"     first-{k}-avg={first_avg:.0f}kB  last-{k}-avg={last_avg:.0f}kB"
          f"  trend={last_avg - first_avg:+.0f}kB", flush=True)
    print(f"fds  base={base[1]} final={fin[1]}  delta={fin[1] - base[1]:+}", flush=True)
    print(f"thr  base={base[2]} final={fin[2]}  delta={fin[2] - base[2]:+}", flush=True)
    eng.stop()
    print("done", flush=True)


if __name__ == "__main__":
    main()
