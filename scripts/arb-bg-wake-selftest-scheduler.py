#!/usr/bin/env python3
"""Detached: sleep to each offset, write marker + inbox notify for interactive pi."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    run_dir = Path(sys.argv[1])
    base = Path(sys.argv[2])
    notify = Path(sys.argv[3])
    offsets = [int(x) for x in sys.argv[4:]]
    run_dir.mkdir(parents=True, exist_ok=True)
    log = open(run_dir / "scheduler.log", "a", buffering=1)
    (run_dir / "scheduler.pid").write_text(str(os.getpid()) + "\n")
    log.write(f"scheduler pid={os.getpid()} offsets={offsets}\n")
    t0 = time.time()
    for o in sorted(offsets):
        delay = o - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        msg = (
            f"# Selftest interactive orch wake t+{o}s\n\n"
            f"- run_dir: `{run_dir}`\n"
            f"- offset_s: {o}\n"
            f"- at: {at}\n\n"
            f"You are the **interactive orch CLI**. A new turn should open now.\n"
            f"Reply with exactly: `SAW_WAKE t+{o}s`\n"
        )
        (run_dir / f"fire-{o}s.marker").write_text(msg)
        env = os.environ.copy()
        env["ARB_BG_WAKE_DIR"] = str(base)
        p = subprocess.run(
            [str(notify), "--stdin"],
            input=msg.encode(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.write(f"t+{o}s notify_exit={p.returncode}\n")
    log.write("scheduler done\n")
    (run_dir / "scheduler.done").write_text("ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
