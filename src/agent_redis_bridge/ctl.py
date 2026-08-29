from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4

from .bridge import DEFAULT_ENV_FILE, DEFAULT_WORKDIR, ENGINE_TO_TOOL, git_branch
from .envelope import Envelope, iso_now
from .redis_io import RedisCli, RedisConfig


DEFAULT_SENDER = "human-codexctl"
DEFAULT_PROJECT = "project-c"
DEFAULT_WORKSPACE = "dev"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    redis_config = RedisConfig.from_env_file(
        Path(args.env_file),
        {
            "AGENT_REDIS_HOST": args.redis_host,
            "AGENT_REDIS_PORT": args.redis_port,
            "AGENT_REDIS_DB": args.redis_db,
            "AGENT_REDIS_PREFIX": args.redis_prefix,
        },
    )
    redis = RedisCli(redis_config)

    if args.command == "send":
        task_id = send_task(redis, args)
        print(task_id)
        return 0
    if args.command == "steer":
        send_control(redis, args, "steer")
        return 0
    if args.command == "cancel":
        send_control(redis, args, "cancel")
        return 0
    if args.command == "watch":
        watch_task(redis_config, args.task_id, from_start=args.from_start)
        return 0
    if args.command == "status":
        print_status(redis, args.task_id)
        return 0
    if args.command == "result":
        print_result(redis, args.task_id)
        return 0
    if args.command == "chat":
        return chat(redis, redis_config, args)

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Terminal client for agent-redis-bridge.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    # DEFAULT_WORKDIR is None when AGENT_WORKDIR isn't set in the shell (post-8e301e8).
    # ctl uses --workdir only for git_branch() reporting; fall back to cwd so we never
    # pass the literal string "None" to Path().
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR or Path.cwd()))
    parser.add_argument("--redis-host")
    parser.add_argument("--redis-port")
    parser.add_argument("--redis-db")
    parser.add_argument("--redis-prefix")
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    parser.add_argument("--engine", choices=sorted(ENGINE_TO_TOOL), default="codex")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--role")
    parser.add_argument("--target")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser(
        "send",
        help="Send an ordinary request via dispatch_authority (pre-minted ref+receipt+brief).",
    )
    send.add_argument(
        "task",
        nargs="*",
        help="deprecated positional (ignored); use --brief with pre-minted flags",
    )
    send.add_argument("--artefact-id", required=True, dest="artefact_id")
    send.add_argument("--version", type=int, required=True, dest="artefact_version")
    send.add_argument("--receipt", type=Path, required=True, help="path to target-bound receipt JSON")
    send.add_argument("--brief", type=Path, required=True, help="path to original brief bytes")
    send.add_argument(
        "--worktree",
        metavar="NAME",
        help="Run in a fresh bridge-created git worktree <workdir>/.claude/worktrees/NAME "
        "(isolated; the completion gate then evaluates THIS worktree, not the base checkout).",
    )
    send.add_argument("--worktree-base", metavar="REF", default="HEAD", help="Base ref for the worktree (default HEAD).")
    send.add_argument(
        "--worktree-cleanup",
        choices=["keep", "auto"],
        default="keep",
        help="keep (default): leave the worktree after the turn; auto: remove it (skipped if the gate bounces).",
    )
    send.add_argument(
        "--expected-artifact",
        action="append",
        dest="expected_artifacts",
        metavar="PATH",
        help="A file the task must produce (repeatable). Lets the drive-to-completion loop "
        "re-prompt a continuation-capable engine until all are present + committed.",
    )
    send.add_argument(
        "--commit-message",
        metavar="MSG",
        help="Exact message the orchestrator-commit uses (over the task-derived default).",
    )
    send.add_argument(
        "--allowed-path",
        action="append",
        dest="allowed_paths",
        metavar="PREFIX",
        help="Path prefix the task may touch beyond its exact --expected-artifacts "
        "(repeatable; e.g. database/migrations/ for a timestamped file). Files committed "
        "or left dirty outside the expected+allowed set fail the run.",
    )
    send.add_argument(
        "--expect-structured",
        action="store_true",
        help="Ask the bridge to request and parse a structured status block in the reply.",
    )
    send.add_argument(
        "--fresh-context",
        action="store_true",
        help="Ask the bridge to reset engine conversation context before this request.",
    )
    send.add_argument(
        "--thread-id",
        metavar="ID",
        help="Ask a supporting bridge engine to resume this existing conversation thread.",
    )
    send.add_argument(
        "--fork-thread-id",
        metavar="ID",
        help="Ask a supporting bridge engine to fork this existing conversation thread.",
    )

    steer = subparsers.add_parser("steer", help="Steer the active task.")
    steer.add_argument("message", nargs="+")
    steer.add_argument("--task-id")

    cancel = subparsers.add_parser("cancel", help="Cancel the active task.")
    cancel.add_argument("--task-id")

    watch = subparsers.add_parser("watch", help="Tail a task event stream.")
    watch.add_argument("task_id")
    watch.add_argument("--from-start", action="store_true")

    status = subparsers.add_parser("status", help="Read compact task status.")
    status.add_argument("task_id")

    result = subparsers.add_parser("result", help="Read final task result.")
    result.add_argument("task_id")

    chat_parser = subparsers.add_parser("chat", help="Interactive terminal chat.")
    chat_parser.add_argument("--no-watch", action="store_true")
    return parser


def send_task(redis: RedisCli, args: argparse.Namespace) -> str:
    """Ordinary send: pre-minted source through dispatch_authority only.

    No independent envelope construction or rpush for ordinary requests.
    """
    import os

    from .dispatch_authority import HarnessPublishReceipt
    from .dispatch_cli import enqueue_pre_minted

    receipt_raw = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    brief_text = Path(args.brief).read_text(encoding="utf-8")
    extra_payload: dict[str, Any] = {}
    worktree = None
    if getattr(args, "worktree", None):
        worktree = {
            "name": args.worktree,
            "base_ref": args.worktree_base,
            "cleanup": args.worktree_cleanup,
        }
    if getattr(args, "expected_artifacts", None):
        extra_payload["expected_artifacts"] = args.expected_artifacts
    if getattr(args, "commit_message", None):
        extra_payload["commit_message"] = args.commit_message
    if getattr(args, "allowed_paths", None):
        extra_payload["allowed_paths"] = args.allowed_paths
    if getattr(args, "expect_structured", False):
        extra_payload["expect_structured"] = True
    if getattr(args, "fresh_context", False):
        extra_payload["fresh_context"] = True
    if getattr(args, "thread_id", None):
        extra_payload["thread_id"] = args.thread_id
    if getattr(args, "fork_thread_id", None):
        extra_payload["fork_from_thread_id"] = args.fork_thread_id

    from .dispatch_authority import filter_publish_env

    safe_env = filter_publish_env(os.environ)
    result = enqueue_pre_minted(
        target_agent_id=target_agent_id(args),
        artefact_id=args.artefact_id,
        version=args.artefact_version,
        receipt=HarnessPublishReceipt.from_dict(receipt_raw)
        if isinstance(receipt_raw, dict)
        else receipt_raw,
        brief_text=brief_text,
        caller_identity="ctl",
        sender=args.sender,
        branch=git_branch(Path(args.workdir)),
        run_id=getattr(args, "run_id", "") or "",
        redis_cli=redis,
        worktree=worktree,
        extra_payload=extra_payload or None,
        env=safe_env,
    )
    return str(result["request_id"])


def send_control(redis: RedisCli, args: argparse.Namespace, kind: str) -> None:
    payload: dict[str, Any] = {}
    if getattr(args, "task_id", None):
        payload["task_id"] = args.task_id
    if kind == "steer":
        payload["message"] = " ".join(args.message)

    envelope = Envelope(
        id=str(uuid4()),
        sender=args.sender,
        branch=git_branch(Path(args.workdir)),
        recipient=target_agent_id(args),
        kind=kind,
        sent_at=iso_now(),
        payload=payload,
    )
    redis.lpush_control(target_agent_id(args), envelope.to_json())
    print(envelope.id)


def watch_task(config: RedisConfig, task_id: str, *, from_start: bool) -> None:
    key = config.task_events_key(task_id)
    last_id = "0" if from_start else "$"
    print(f"watching {key}", flush=True)
    while True:
        events = xread(config, key, last_id)
        for event_id, fields in events:
            last_id = event_id
            print(format_event(event_id, fields), flush=True)


def print_status(redis: RedisCli, task_id: str) -> None:
    status = redis.hgetall(redis.config.task_status_key(task_id))
    print(json.dumps(status, indent=2, sort_keys=True))


def print_result(redis: RedisCli, task_id: str) -> None:
    body = redis.get_str(redis.config.task_result_key(task_id))
    if not body:
        print("{}")
        return
    print(json.dumps(json.loads(body), indent=2, sort_keys=True))


def chat(redis: RedisCli, config: RedisConfig, args: argparse.Namespace) -> int:
    print("codexctl chat. Type a task, /steer <text>, /cancel, /status, /result, /watch, or /quit.")
    print(f"sender={args.sender} target={target_agent_id(args)}")
    active_task_id: str | None = None
    watcher_stop = threading.Event()
    watcher: threading.Thread | None = None

    while True:
        try:
            line = input("codex> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line in {"/quit", "/exit"}:
            break
        if line == "/cancel":
            if active_task_id is None:
                print("no active task")
                continue
            send_control(redis, namespace_args(args, task_id=active_task_id), "cancel")
            continue
        if line.startswith("/steer "):
            if active_task_id is None:
                print("no active task")
                continue
            send_control(redis, namespace_args(args, task_id=active_task_id, message=line.removeprefix("/steer ").split()), "steer")
            continue
        if line == "/status":
            if active_task_id is None:
                print("no active task")
                continue
            print_status(redis, active_task_id)
            continue
        if line == "/result":
            if active_task_id is None:
                print("no active task")
                continue
            print_result(redis, active_task_id)
            continue
        if line == "/watch":
            if active_task_id is None:
                print("no active task")
                continue
            print(f"task_id={active_task_id}")
            continue

        # Slice 1d-iv: free-form chat dispatch removed. Ordinary enqueue requires
        # a pre-minted ref+receipt+brief via `ctl send --artefact-id ...`.
        print(
            "ordinary free-form chat dispatch removed (Slice 1d-iv). "
            "Use: ctl send --artefact-id ID --version N --receipt PATH --brief PATH "
            "[optional turn flags]. Publish first with arb-memory-harness-publish."
        )
        continue

    watcher_stop.set()
    if watcher is not None:
        watcher.join(timeout=1)
    return 0


def watch_task_until_done(config: RedisConfig, task_id: str, stop: threading.Event) -> None:
    key = config.task_events_key(task_id)
    last_id = "0"
    while not stop.is_set():
        events = xread(config, key, last_id, block_ms=1000)
        for event_id, fields in events:
            last_id = event_id
            print(format_event(event_id, fields), flush=True)
            if fields.get("type") == "task_finished":
                return


def xread(config: RedisConfig, key: str, last_id: str, *, block_ms: int = 1000) -> list[tuple[str, dict[str, str]]]:
    result = subprocess.run(
        [
            "redis-cli",
            "--raw",
            "-h",
            config.host,
            "-p",
            config.port,
            "-n",
            config.db,
            "XREAD",
            "BLOCK",
            str(block_ms),
            "STREAMS",
            key,
            last_id,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    lines = result.stdout.splitlines()
    events: list[tuple[str, dict[str, str]]] = []
    index = 1 if lines and lines[0] == key else 0
    while index < len(lines):
        event_id = lines[index]
        index += 1
        fields: dict[str, str] = {}
        while index + 1 < len(lines) and not looks_like_stream_id(lines[index]):
            fields[lines[index]] = lines[index + 1]
            index += 2
        events.append((event_id, fields))
    return events


def format_event(event_id: str, fields: dict[str, str]) -> str:
    event_type = fields.get("type", "event")
    data = fields.get("data")
    if data:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
    else:
        parsed = {}
    return f"[{event_type}] {event_id} {parsed}"


def looks_like_stream_id(value: str) -> bool:
    left, _, right = value.partition("-")
    return bool(left.isdigit() and right.isdigit())


def namespace_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    data = vars(args).copy()
    data.update(overrides)
    return argparse.Namespace(**data)


def target_agent_id(args: argparse.Namespace) -> str:
    if args.target:
        return args.target
    agent_id = f"{ENGINE_TO_TOOL[args.engine]}-{args.project}-{args.workspace}"
    if args.role:
        return f"{agent_id}-{args.role}"
    return agent_id


if __name__ == "__main__":
    raise SystemExit(main())
