from __future__ import annotations

import sys

from .common import (
    copy_redis_record_to_draining,
    fail_soft,
    load_hook_payload,
    redis_client,
    registry_path,
    remove_redis_record,
    remove_registry_record,
    required_str,
)


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    session_id = required_str(payload, "session_id")
    path = registry_path()
    if path is not None:
        # File-registry mode relies on the daemon-side fallback for draining handoff.
        remove_registry_record(path, session_id)
    else:
        client = redis_client()
        if client is None:
            raise ValueError("ARB_CLAUDE_TAIL_REGISTRY_PATH or AGENT_REDIS_URL is required")
        copy_redis_record_to_draining(client, session_id)
        remove_redis_record(client, session_id)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("session_end", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
