from __future__ import annotations

import sys

from .common import (
    evict_stale_seat_records,
    evict_stale_seat_redis_records,
    fail_soft,
    load_hook_payload,
    mirror_cold_outputs,
    project_workspace,
    redis_client,
    registry_path,
    required_str,
    upsert_redis_record,
    upsert_registry_record,
)


def build_record(payload: dict) -> dict[str, str]:
    session_id = required_str(payload, "session_id")
    transcript_path = required_str(payload, "transcript_path", "transcriptPath")
    project, workspace = project_workspace(payload)
    seat_id = f"claude-{project}-{workspace}"
    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "seat_id": seat_id,
        "run_id": session_id,
    }


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    record = build_record(payload)
    path = registry_path()
    if path is not None:
        evicted = evict_stale_seat_records(path, record["seat_id"], record["session_id"])
        upsert_registry_record(path, record)
    else:
        client = redis_client()
        if client is None:
            raise ValueError("ARB_CLAUDE_TAIL_REGISTRY_PATH or AGENT_REDIS_URL is required")
        evicted = evict_stale_seat_redis_records(client, record["seat_id"], record["session_id"])
        upsert_redis_record(client, record)
    for stale_id in evicted:
        print(f"[claude-tail] evicted stale registry entry {stale_id} for seat {record['seat_id']}", file=sys.stderr)
    mirror_cold_outputs(payload)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("session_start", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
