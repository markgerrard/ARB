"""Executable controller attempt-service seam shared by scored-dispatch tests."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from implbench.harness.git_service import AttemptGitServiceServer, GitService


def lifecycle(tmp_path: Path, base: Any | None = None, dispatch_result: dict[str, Any] | None = None) -> Any:
    """Return a real, terminally-closed controller Git RPC lifecycle test double."""
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="attempt-service-", dir=tmp_path))
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "fixture"], check=True)
    tracked = repo / "fixture.txt"
    tracked.write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    fixture = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    holder: dict[str, AttemptGitServiceServer] = {}
    events: list[dict[str, Any]] = []

    def open_attempt_git_service(attempt_id: str, *, allowed_paths: tuple[str, ...]) -> dict[str, str]:
        events.append({"op": "open", "attempt_id": attempt_id, "allowed_paths": allowed_paths})
        server = AttemptGitServiceServer(
            GitService(repo, fixture_root_oid=fixture, allowed_paths=allowed_paths or ("*",), tool_gid=os.getgid()),
            root=root, attempt_id=attempt_id, tool_gid=os.getgid(), peer_uids=(os.getuid(),),
        )
        holder["server"] = server
        return server.start()

    def close_attempt_git_service() -> None:
        server = holder.pop("server", None)
        if server is not None:
            server.close()

    class AttemptLifecycle:
        def __init__(self) -> None:
            self.events = events

        def open_attempt_git_service(self, attempt_id: str, *, allowed_paths: tuple[str, ...]) -> dict[str, str]:
            return open_attempt_git_service(attempt_id, allowed_paths=allowed_paths)

        def close_attempt_git_service(self) -> None:
            close_attempt_git_service()

        def start_attempt_planes(self, binding: dict[str, str]) -> None:
            events.append({"op": "start", "endpoint": binding["endpoint"]})

        def dispatch_through_control(self, task: Any, engine: str, *, timeout: int) -> dict[str, Any]:
            events.append({"op": "dispatch", "task": task.brief, "engine": engine, "timeout": timeout})
            if dispatch_result is not None:
                return dict(dispatch_result)
            return {
                "status": "failed", "timed_out": False, "structured": {}, "text": "",
                "completion": {
                    "mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": [],
                    "dirty": False, "seal_complete": False, "receipts_authenticated": False,
                    "infrastructure_failure": "dispatch-failed",
                },
            }

        def __getattr__(self, name: str) -> Any:
            if base is not None:
                return getattr(base, name)
            raise AttributeError(name)

    return AttemptLifecycle()
