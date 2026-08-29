"""Dedicated, uncredentialed child entry point for scored Git RPC."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from .git_service import AttemptGitServiceServer, GitRPCError, GitService, encode_frame
from .receipts import make_git_receipt


_MAX_REQUEST_BYTES = 65536


def _read_request(fd: int) -> dict:
    raw = bytearray()
    while len(raw) <= _MAX_REQUEST_BYTES:
        chunk = os.read(fd, _MAX_REQUEST_BYTES - len(raw) + 1)
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise GitRPCError("child Git service request exceeds its bound")
    value = json.loads(bytes(raw))
    required = {
        "version", "run_id", "cell_id", "attempt_id", "root", "nonce", "repo", "git_dir", "worktree",
        "fixture_root_oid", "allowed_paths", "endpoint", "capability", "tool_gid", "peer_uids", "control_fd",
        "effective_tool_gid",
        "profile_digest", "template_digest", "expected_uid", "expected_gid",
        "structural_identity",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != "implbench-git-child-v1":
        raise GitRPCError("child Git service request is not closed")
    return value


def _absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value:
        raise GitRPCError(f"child Git service {label} is invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-fd", type=int, required=True)
    args = parser.parse_args(argv)
    server: AttemptGitServiceServer | None = None
    try:
        value = _read_request(args.request_fd)
        if not isinstance(value["structural_identity"], bool):
            raise GitRPCError("child Git service structural identity mode is malformed")
        if (not value["structural_identity"]
                and (value["expected_uid"] != os.getuid() or value["expected_gid"] != os.getgid())):
            raise GitRPCError("child Git service identity does not match the launch binding")
        root = _absolute_path(value["root"], "root")
        repo = _absolute_path(value["repo"], "repository")
        git_dir = _absolute_path(value["git_dir"], "Git directory")
        worktree = _absolute_path(value["worktree"], "worktree")
        endpoint = _absolute_path(value["endpoint"], "endpoint")
        if (not isinstance(value["run_id"], str) or not isinstance(value["cell_id"], str)
                or not isinstance(value["attempt_id"], str) or not isinstance(value["nonce"], str)
                or not isinstance(value["capability"], str) or not isinstance(value["allowed_paths"], list)
                or not isinstance(value["peer_uids"], list)):
            raise GitRPCError("child Git service bindings are malformed")
        control = socket.socket(fileno=int(value["control_fd"]))

        def receipt(result: dict) -> None:
            candidate = make_git_receipt(
                cell_id=value["cell_id"],
                attempt_id=value["attempt_id"],
                fixture_root_oid=result["fixture_root_oid"],
                ordered_parent_oids=result["ordered_parent_oids"],
                commit_oid=result["commit_oid"],
                tree_oid=result["tree_oid"],
                changed_paths=result["changed_paths"],
                tree_digest=result["tree_digest"],
                head_oid=result["head_oid"],
                dirty=result["dirty"],
                controller_sequence=None,
            )
            control.sendall(encode_frame({"kind": "git-receipt", "payload": candidate}))
            if control.recv(1) != b"1":
                raise GitRPCError("controller rejected receipt candidate")

        def infrastructure(candidate: dict) -> None:
            control.sendall(encode_frame({"kind": "infrastructure-failure", "payload": candidate}))
            if control.recv(1) != b"1":
                raise GitRPCError("controller rejected infrastructure candidate")

        service = GitService(
            repo,
            fixture_root_oid=value["fixture_root_oid"],
            allowed_paths=tuple(value["allowed_paths"]),
            git_dir=git_dir,
            worktree=worktree,
            tool_gid=value["tool_gid"],
            receipt_authorizer=receipt,
            infrastructure_authorizer=infrastructure,
        )
        server = AttemptGitServiceServer(
            service,
            root=Path(root),
            attempt_id=value["attempt_id"],
            tool_gid=value["effective_tool_gid"],
            peer_uids=tuple(value["peer_uids"]),
        )
        server.endpoint = Path(endpoint)
        server.capability = value["capability"]
        server.start()
        response = {
            "version": "implbench-git-child-v1", "ok": True, "pid": os.getpid(),
            "uid": os.getuid(), "gid": os.getgid(), "run_id": value["run_id"],
            "cell_id": value["cell_id"], "attempt_id": value["attempt_id"], "root": root,
            "nonce": value["nonce"], "repo": repo, "git_dir": git_dir, "worktree": worktree,
            "endpoint": endpoint, "profile_digest": value["profile_digest"],
            "template_digest": value["template_digest"],
        }
        sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        while True:
            time.sleep(1)
    except Exception:
        return 1
    finally:
        # Normal terminal close arrives as SIGTERM from the controller, but this
        # makes malformed request and future cooperative exits unlink the endpoint.
        if server is not None:
            server.close()


if __name__ == "__main__":
    raise SystemExit(main())
