from __future__ import annotations

from dataclasses import dataclass
import re


_MARKER_RE = re.compile(r"\[ARB_RUN:(\S+)\s+ARB_SEAT:(\S+)(?:\s+ARB_ORCH:(\S+))?\]")

# Subagent-emitted completion signal. A cold-Opus seat ends its output with this marker so the
# daemon can finish the seat promptly and accurately (the symmetric counterpart to the brief's
# start marker), instead of inferring "done" from a 5-min idle heuristic.
_DONE_RE = re.compile(r"\[ARB_SEAT_DONE\]")


def has_done_marker(text: str) -> bool:
    return bool(text) and _DONE_RE.search(text) is not None


@dataclass(frozen=True)
class Identity:
    run_id: str
    task_id: str
    seat_id: str
    orchestrator: str


def parse_marker(first_user_text: str) -> dict[str, str] | None:
    match = _MARKER_RE.search(first_user_text)
    if match is None:
        return None
    return {"run_id": match.group(1), "seat_id": match.group(2), "orchestrator": match.group(3) or ""}


def warm_identity(session_id: str, project: str, workspace: str) -> Identity:
    seat_id = f"claude-{project}-{workspace}"
    return Identity(
        run_id=session_id,
        task_id=session_id,
        seat_id=seat_id,
        orchestrator=seat_id,
    )


def cold_identity(
    agent_id: str, session_id: str, marker_text: str, *, project: str = "", workspace: str = ""
) -> Identity:
    marker = parse_marker(marker_text)
    if marker is None:
        run_id = session_id
        # Bridge-seat parity (codex-<project>-<workspace>) when the daemon knows its own
        # project/workspace -- full parity, no agent_id suffix. Falls back to the agent-id GUID,
        # unchanged, when project/workspace aren't configured.
        #
        # ACCEPTED RISK, not an oversight: two cold-Opus seats sharing this seat_id only collide
        # in arb-watch's dedupSeatRuns() (tools/arb-watch-go/model.go) if a caller ALSO tags both
        # with the identical run_id -- distinct run_ids never collide regardless of seat_id (see
        # that function's own doc comment). An earlier revision of this fix appended an 8-char
        # agent_id slice specifically to close that gap (codex review, P1); reverted here on
        # explicit user direction, confirmed because in practice only one cold-Opus reviewer runs
        # per panel round -- multiple reviewers come from distinct bridge seats, never multiple
        # concurrent cold-Opus instances. If that usage pattern ever changes, revisit this.
        seat_id = f"cold-opus-{project}-{workspace}" if project and workspace else f"cold-opus-{agent_id}"
        orchestrator = ""
    else:
        run_id = marker["run_id"]
        seat_id = marker["seat_id"]
        orchestrator = marker["orchestrator"]
    return Identity(
        run_id=run_id,
        task_id=agent_id,
        seat_id=seat_id,
        orchestrator=orchestrator,
    )
