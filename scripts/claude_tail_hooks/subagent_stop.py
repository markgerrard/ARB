from __future__ import annotations

import json
import sys

from .common import cold_dir, fail_soft, load_hook_payload, required_str, write_json_atomic


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    agent_id = required_str(payload, "agent_id")

    sidecar = cold_dir() / f"{agent_id}.arb-tail.json"
    if not sidecar.exists():
        return 0

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0

    data["completed"] = True
    write_json_atomic(sidecar, data)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("subagent_stop", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
