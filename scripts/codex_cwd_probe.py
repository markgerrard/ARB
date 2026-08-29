#!/usr/bin/env python3
"""Probe: can one warm codex app-server serve turns in different repos / cold contexts?

Spawns ONE codex app-server and runs three turns to characterise cwd + thread behaviour:
  T1  thread@A, turn cwd=A                 -> baseline (expect repo A)
  T2  SAME thread@A, turn cwd=B            -> tests per-turn cwd override
  T3  NEW thread/start @ B, same server    -> tests warm new-thread (cold context) path

Each turn asks codex to print `git rev-parse --show-toplevel`, so the reply reveals the
working directory it actually operated in. Evidence behind docs/repo-agnostic-worker-pool.md.

NOTE: this spawns a LIVE codex app-server (needs `codex` on PATH) and runs real model turns,
so it costs tokens. Read-only-ish: it only prints a path, makes no edits.

Usage:  python scripts/codex_cwd_probe.py [REPO_A] [REPO_B]
        REPO_A defaults to this repo; REPO_B defaults to /srv/project-g-laravel/ProjectGLaravel.
        Both must be existing git checkouts.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from agent_redis_bridge.engines.codex import CodexEngine  # noqa: E402

REPO_A = sys.argv[1] if len(sys.argv) > 1 else REPO_ROOT
REPO_B = sys.argv[2] if len(sys.argv) > 2 else "/srv/project-g-laravel/ProjectGLaravel"
PROMPT = (
    "Run the shell command `git rev-parse --show-toplevel` and reply with ONLY the "
    "resulting absolute path on a single line. Run no other commands; add no explanation."
)


def banner(t):
    print(f"\n===== {t} =====", flush=True)


def turn(eng, label):
    r = eng.run_turn_with_progress(PROMPT, timeout=180, policy="trusted", on_event=None)
    print(f"[{label}] ok={r.ok} result={r.result!r} err={r.error}", flush=True)
    return r


def main():
    eng = CodexEngine(
        cwd=REPO_A, model="gpt-5.5", approval_policy="never",
        sandbox="workspace-write", bypass_approvals_and_sandbox=True,
    )
    banner(f"start app-server + thread/start @ {REPO_A}")
    eng.start()
    print("pid:", eng.process.pid, "thread_id:", eng.thread_id, flush=True)

    banner("TURN 1: thread@A, turn cwd=A  (expect REPO_A)")
    r1 = turn(eng, "T1 thread@A turn@A")

    banner("TURN 2: SAME thread@A, turn cwd=B  (tests per-turn cwd override)")
    eng.cwd = REPO_B
    r2 = turn(eng, "T2 thread@A turn@B")

    banner("TURN 3: NEW thread/start @ REPO_B on the SAME warm app-server")
    eng.cwd = REPO_B
    resp = eng.request("thread/start", eng.thread_start_params(), timeout=30)
    eng.thread_id = resp["thread"]["id"]
    print("same pid:", eng.process.pid, "new thread_id:", eng.thread_id, flush=True)
    r3 = turn(eng, "T3 newthread@B")

    banner("SUMMARY")
    norm = lambda r: (r.result or "").strip() if r.ok else f"FAIL: {r.error}"  # noqa: E731
    print("T1 thread A / turn A :", norm(r1), flush=True)
    print("T2 thread A / turn B :", norm(r2), flush=True)
    print("T3 new thread @ B    :", norm(r3), flush=True)
    print("\nexpected A:", REPO_A, flush=True)
    print("expected B:", REPO_B, flush=True)
    eng.stop()
    print("done", flush=True)


if __name__ == "__main__":
    main()
