"""Helpers shared by the coordination canaries.

The system under test is always a REAL script from ``scripts/`` driven through a
subprocess against a real bus. Nothing here reimplements watcher or envelope
behaviour: a canary that models the consumer proves things about the model.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RELIABLE = REPO / "scripts" / "agent-inbox-watcher-reliable"
SPLIT = REPO / "scripts" / "agent-inbox-watcher-split"
BLPOP_WATCHER = REPO / "scripts" / "agent-inbox-watcher"

META_RE = re.compile(r"^\[inbox-meta\] id=(?P<id>\S+) from=(?P<frm>\S+) kind=(?P<kind>\S+)")


def envelope(
    *, frm: str, to: str, kind: str = "notify", event: str = "canary",
    eid: str | None = None, data: dict | None = None,
) -> dict:
    """A wire-valid envelope. Shape mirrors envelope.py's required fields."""
    return {
        "id": eid or str(uuid.uuid4()),
        "from": frm,
        "branch": "dev",
        "to": to,
        "kind": kind,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"event": event, "data": data or {}},
    }


def run_reliable_watcher(
    plane, agent_id: str, inbox_dir: Path, *, iterations: int = 1,
    blmove_timeout: int = 1, timeout: int = 60, **extra_env,
) -> subprocess.CompletedProcess:
    """Run the BLMOVE-based reliable watcher for a bounded number of iterations."""
    env = plane.script_env(
        agent_id, inbox_dir,
        WATCHER_MAX_ITERATIONS=iterations,
        WATCHER_BLMOVE_TIMEOUT=blmove_timeout,
        WATCHER_HEARTBEAT_INTERVAL=1,
        **extra_env,
    )
    return subprocess.run(
        ["python3", str(RELIABLE)], env=env, text=True,
        capture_output=True, timeout=timeout, check=False,
    )


def spawn_reliable_watcher(plane, agent_id: str, inbox_dir: Path, *, blmove_timeout: int = 5,
                           **extra_env) -> subprocess.Popen:
    """Start an UNBOUNDED reliable watcher, for canaries that kill it mid-flight."""
    env = plane.script_env(
        agent_id, inbox_dir,
        WATCHER_BLMOVE_TIMEOUT=blmove_timeout,
        WATCHER_HEARTBEAT_INTERVAL=1,
        **extra_env,
    )
    return subprocess.Popen(
        ["python3", str(RELIABLE)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Its OWN process group. Without this, kill_hard's killpg resolves to the
        # pytest runner's group and kills the test session instead of the watcher
        # — the same shape as the standing "kill by PID, never by pattern" rule.
        start_new_session=True,
    )


def spawn_split_watcher(plane, agent_id: str, inbox_dir: Path, *, blpop_timeout: int = 2,
                        **extra_env) -> subprocess.Popen:
    """Start the OPERATIONAL BLPOP split watcher (bash; loops forever, so the
    caller must kill it). This is the consumer actually armed on arb-buzz, which
    is why its behaviour is worth pinning separately from the reliable daemon."""
    env = plane.script_env(agent_id, inbox_dir, AGENT_BLPOP_TIMEOUT=blpop_timeout, **extra_env)
    return subprocess.Popen(
        ["bash", str(SPLIT)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def run_split_watcher(plane, agent_id: str, inbox_dir: Path, *, iterations: int = 1,
                      blpop_timeout: int = 1, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run the BLPOP split watcher — the consumer actually armed operationally."""
    env = plane.script_env(
        agent_id, inbox_dir,
        AGENT_WATCHER_MAX_ITERATIONS=iterations,
        WATCHER_MAX_ITERATIONS=iterations,
        AGENT_BLPOP_TIMEOUT=blpop_timeout,
    )
    return subprocess.run(
        ["bash", str(SPLIT)], env=env, text=True,
        capture_output=True, timeout=timeout, check=False,
    )


def meta_ids(stdout: str) -> list[str]:
    """Envelope ids the watcher SURFACED — i.e. agent wakes, not disk writes.

    The distinction is the whole point of the dual-plane canary: the reliable
    watcher dedups the disk write by id but emits the meta line unconditionally
    (agent-inbox-watcher-reliable startup_redrain / consume paths), so counting
    files understates how many times an agent is woken.
    """
    return [m.group("id") for m in (META_RE.match(line) for line in stdout.splitlines()) if m]


def disk_ids(inbox_dir: Path) -> set[str]:
    return {p.stem for p in inbox_dir.glob("*.json")}


def kill_hard(proc: subprocess.Popen, *, grace: float = 0.0) -> None:
    """SIGKILL the watcher and its redis-cli child.

    Deliberately SIGKILL, not SIGTERM: the durability claim under test is that a
    consumer which dies WITHOUT running any cleanup still loses nothing, because
    BLMOVE already committed the element to :processing. A graceful stop would
    let the script tidy up and prove nothing about crash-safety.
    """
    if grace:
        time.sleep(grace)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def drain_then_kill(proc: subprocess.Popen, plane, agent_id: str, *,
                    timeout: float = 20.0) -> str:
    """Let a long-running watcher drain the inbox, then kill it and return stdout."""
    wait_until(lambda: plane.depth(agent_id) == 0, timeout=timeout)
    time.sleep(0.5)  # let the final meta line flush before the pipe dies
    kill_hard(proc)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        out = ""
    return out or ""


def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.1) -> bool:
    """Poll until true. Returns the outcome rather than asserting, so callers
    state their own failure message."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def read_envelope(inbox_dir: Path, eid: str) -> dict:
    return json.loads((inbox_dir / f"{eid}.json").read_text())
