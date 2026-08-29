from __future__ import annotations

import sys
from pathlib import Path

from .common import cold_agent_types, cold_dir, fail_soft, load_hook_payload, lookup_registry_record, required_str, write_json_atomic


def _main(args: list[str] | None = None) -> int:
    payload = load_hook_payload(args)
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str) or agent_type not in cold_agent_types():
        return 0

    session_id = required_str(payload, "session_id")
    agent_id = required_str(payload, "agent_id")

    parent = lookup_registry_record(session_id)
    if parent is None:
        return 0
    parent_transcript = parent.get("transcript_path")
    if not isinstance(parent_transcript, str) or not parent_transcript:
        return 0

    # Claude Code nests a session's subagent transcripts under a directory matching the SESSION
    # ID itself -- a sibling of the flat <session_id>.jsonl parent transcript, not a child of its
    # parent directory. with_suffix("") turns ".../<session_id>.jsonl" into ".../<session_id>"
    # (a directory path of the same name); .parent here would silently drop the session_id
    # segment entirely and produce a dangling symlink (found during implementation review).
    subagent_transcript = Path(parent_transcript).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    directory = cold_dir()

    # Sidecar first, atomically -- see the write-order note above. Discovery only ever looks for
    # the symlink, so the sidecar must already be complete by the time the symlink can be seen.
    sidecar = directory / f"{agent_id}.arb-tail.json"
    write_json_atomic(sidecar, {"orchestrator": parent.get("seat_id") or "", "completed": False})

    output_link = directory / f"{agent_id}.output"
    if output_link.is_symlink() or output_link.exists():
        output_link.unlink()
    output_link.symlink_to(subagent_transcript)
    return 0


def main(args: list[str] | None = None) -> int:
    return fail_soft("subagent_start", _main, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
