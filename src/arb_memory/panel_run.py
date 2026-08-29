"""Orchestrate one panel-audit run: dispatch manifest -> per-seat votes -> reconciled verdict.

Thin assembly over the already-built pieces: `arb_memory.audit.AuditRun` (emit),
`arb_memory.panel_audit.reconcile` (the anti-laundering gate), `arb_memory.stance.parse_stance`.

Division of labour the orchestrator follows:
  - BRIDGE seats (codex/agy/...) self-emit their vote when dispatched with
    `agent-dispatch --run-id <id> --audit-panel` (their reply carries the fenced ```vote``` block
    that `scripts/review-brief` now mandates).
  - NON-BRIDGE reviewers (cold-Opus, an in-session Agent subagent the warm session spawns) are
    captured here via `record_vote(reply_text=...)` — the helper parses the same fenced block from
    the reviewer's returned report.
  - The orchestrator emits the dispatch manifest (`start_run`) and the reconciled verdict (`finalize`).
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Mapping

from arb_memory.audit import AuditRun
from arb_memory.panel_audit import reconcile
from arb_memory.stance import parse_stance


def start_run(redis, roster: Iterable[str], *, run_id: str | None = None,
              source: str = "orchestrator", prefix: str = "", note: str = "") -> str:
    """Emit the dispatch manifest (the seq-1 row declaring the roster). Returns the run_id.

    Must be called BEFORE any seat votes — reconcile rejects a run whose manifest does not precede
    every vote, and whose roster is empty.
    """
    run_id = run_id or uuid.uuid4().hex
    payload: dict[str, Any] = {"kind": "dispatch", "roster": list(roster)}
    if note:
        payload["note"] = note
    AuditRun(redis, run_id, prefix=prefix).emit(source, "dispatch", payload)
    return run_id


def record_vote(redis, run_id: str, actor: str, *, reply_text: str | None = None,
                timed_out: bool = False, prefix: str = "", require_fence: bool = True) -> dict:
    """Capture a NON-BRIDGE reviewer's vote from its reply text and emit it. Fail-loud: a reply with
    no parseable fenced stance raises `StanceError` (never invent a vote). `actor` is the roster id
    (e.g. ``seat:cold-opus``). Bridge seats do NOT use this — they self-emit via `--audit-panel`."""
    if timed_out:
        stance = {"stance": "timed-out", "severity": "none", "refs": [], "note": "no reply"}
    else:
        stance = parse_stance(reply_text or "", require_fence=require_fence)
    payload = {"kind": "vote", "actor": actor, **stance}
    AuditRun(redis, run_id, prefix=prefix).emit("orchestrator", "vote", payload)
    return stance


def finalize(conn, redis, run_id: str, *, roster: Iterable[str], stances: Mapping[str, str],
             source: str = "orchestrator", prefix: str = "", overall: str | None = None) -> dict:
    """Reconcile the proposed verdict against the recorded dispatch+votes (anti-laundering), then emit
    the verdict ONLY if it reconciles. Returns the reconcile result ``{ok, incomplete, gaps}``.

    A non-ok result means the verdict does not match the recorded votes (a missing seat vote, an
    unrostered/duplicate vote, or a claimed stance that no vote row supports) — the verdict is NOT
    emitted, and the caller must resolve the gap rather than launder a verdict.
    """
    verdict_payload: dict[str, Any] = {"kind": "verdict", "roster": list(roster), "stances": dict(stances)}
    if overall is not None:
        verdict_payload["overall"] = overall
    result = reconcile(conn, run_id, verdict_payload, redis=redis)
    if result["ok"]:
        AuditRun(redis, run_id, prefix=prefix).emit(source, "verdict", verdict_payload)
    return result
