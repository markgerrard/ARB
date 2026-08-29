"""Muse Code JSONL envelope -> `turn_events` vocabulary.

Pure by design (spec `2026-08-06-muse-runner-design.md` §2.1): nothing here
spawns, signals, or reaps a process. All Muse-specific knowledge lives in this
module so it can be exercised offline from literal envelopes, which is what
keeps the live-capture budget small.

Evidence for the mapping is ARB Memory `findings-muse-code-seat-probe-20260806`
v1 §4, measured against `Muse Code 0.1.0 (0.1.0-R708.1)`.

Two rules here are not stylistic — they are recorded traps, and inverting either
produces a seat that looks like it works:

* `task.lifecycle.failed` does NOT decide the turn. The echo provider emits two
  of them on a run that terminates `completed`. Outcome comes from
  `run.terminal.*` alone.
* `task.lifecycle.status` is provider-retry telemetry ("opening meta model
  stream attempt 1/10"), NOT reasoning. Mapping it to `ReasoningDelta` would
  fill buzz's thought panel with retry chatter.
"""
from __future__ import annotations

import json
from typing import Any

from .turn_events import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEvent,
)

# Muse names its tools `tool.<verb>`; `turn_events.tool_kind` keys off Claude's
# tool names, so it cannot be reused directly. Mapping to the SAME kind
# vocabulary matters: buzz resets its 900s idle deadline on ToolCallStarted
# (turn_events.py:89-91) regardless of kind, but the kind is what the activity
# panel renders.
_MUSE_KINDS = {
    "tool.bash": "execute",
    "tool.shell": "execute",
    "tool.read": "read",
    "tool.write": "edit",
    "tool.edit": "edit",
    "tool.glob": "search",
    "tool.grep": "search",
}

_CALL_ID_PREFIX = "tool:"


def lifecycle_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a `task.lifecycle.*` payload's nested `event` object.

    MEASURED 2026-08-06 against a live capture, and NOT what the probe artefact
    implied. The envelope families nest differently:

        task.lifecycle.*  -> fields live under payload["event"]
                             (task_kind, idempotency_key, reason)
        run.* / tool.*    -> fields are FLAT on payload
                             (text, terminal, call_id, correlation_facts)

    The first implementation read the lifecycle fields flat. Every one resolved
    to None, so `scheduled` returned no id and the mapper emitted NO tool calls
    at all -- silently, because a missing key is not an error. Fixtures
    tests/fixtures/muse/*.jsonl are the evidence.
    """
    event = payload.get("event")
    return event if isinstance(event, dict) else {}


def muse_kind(task_kind: str | None) -> str:
    """Kind for a Muse `task_kind`, defaulting to `other` like `tool_kind`."""
    if not task_kind:
        return "other"
    return _MUSE_KINDS.get(task_kind, "other")


def normalise_call_id(raw: str) -> str:
    """Strip the one `tool:` prefix that `idempotency_key` carries.

    ASSUMPTION, gated (plan gate G6). `task.lifecycle.scheduled` carries
    `idempotency_key: "tool:call_019fd538..."` while `tool.result` carries
    `call_id: "call_019fd538..."`. Joining them by stripping the prefix is what
    lets a completion match its start. If the real ids do not correspond, every
    ToolCallCompleted misses its ToolCallStarted -- which is silent, because
    nothing raises. One live capture settles it; offline tests cannot.
    """
    if raw.startswith(_CALL_ID_PREFIX):
        return raw[len(_CALL_ID_PREFIX):]
    return raw


def parse_line(line: str) -> dict[str, Any] | None:
    """One stdout line -> envelope dict, or None if it is not one.

    `muse exec` prints a non-JSON preamble (`muse: workspace root: ...`) before
    the JSONL. That is a SKIP, not a parse error: raising here would make every
    successful turn report a failure. Malformed JSON is skipped for the same
    reason -- a truncated final line on a killed process is expected, not
    exceptional.
    """
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class MuseEventMapper:
    """Stateful across one turn; maps envelopes to TurnEvents.

    State is required and minimal: a tool call's KIND arrives on
    `task.lifecycle.proposed` while its stable ID arrives on
    `task.lifecycle.scheduled`, so the two must be joined by `task_id`.
    """

    def __init__(self) -> None:
        self.terminal: str | None = None
        self.terminal_text: str = ""
        self.terminal_reason: str | None = None
        self.failures: list[str] = []
        self._kind_by_task: dict[str, str] = {}

    def feed(self, envelope: dict[str, Any]) -> list[TurnEvent]:
        payload_type = envelope.get("payload_type")
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            return []

        if payload_type == "run.output.delta":
            text = payload.get("text")
            return [TextDelta(text=text)] if text else []

        if isinstance(payload_type, str) and payload_type.startswith(
            "run.terminal."
        ):
            self.terminal = payload.get("terminal") or payload_type.rsplit(
                ".", 1
            )[-1]
            self.terminal_text = payload.get("text") or ""
            self.terminal_reason = payload.get("reason")
            return []

        if payload_type == "task.lifecycle.proposed":
            event = lifecycle_event(payload)
            task_id = payload.get("task_id") or event.get("task_id")
            task_kind = event.get("task_kind")
            if task_id and task_kind:
                self._kind_by_task[str(task_id)] = str(task_kind)
            return []

        if payload_type == "task.lifecycle.scheduled":
            event = lifecycle_event(payload)
            key = event.get("idempotency_key")
            if not key:
                return []
            # NOT every scheduled task is a tool. MEASURED in one real turn:
            # three `scheduled` events, of which two were `model.meta.response`
            # carrying `idempotency_key="model:<run>:<task>"`. Emitting
            # ToolCallStarted for those would invent tool calls that never
            # complete -- they have no `tool.result` -- leaving unmatched
            # starts in buzz's activity panel and resetting its idle deadline
            # on model chatter. The `tool:` prefix IS the discriminator; that
            # is what it is for.
            key = str(key)
            if not key.startswith(_CALL_ID_PREFIX):
                return []
            task_id = str(payload.get("task_id") or event.get("task_id") or "")
            task_kind = self._kind_by_task.get(task_id)
            return [
                ToolCallStarted(
                    tool_call_id=normalise_call_id(key),
                    title=task_kind or "tool",
                    kind=muse_kind(task_kind),
                )
            ]

        if payload_type == "tool.result":
            call_id = payload.get("call_id")
            if not call_id:
                return []
            facts = payload.get("correlation_facts") or {}
            outcome = facts.get("outcome") if isinstance(facts, dict) else None
            # turn_events.py:105-106 requires a failing tool stay distinguishable.
            # `outcome` carries a real verdict, so this is a real mapping
            # rather than an optimistic constant.
            status = "completed" if outcome == "success" else "failed"
            return [
                ToolCallCompleted(
                    tool_call_id=normalise_call_id(str(call_id)),
                    status=status,
                )
            ]

        if payload_type == "task.lifecycle.failed":
            # Recorded as detail; deliberately NOT terminal. See module
            # docstring -- CONFIRMED on live bytes: the echo capture carries
            # two of these on a run whose terminal is `completed`.
            reason = lifecycle_event(payload).get("reason")
            if reason:
                self.failures.append(str(reason))
            return []

        # task.lifecycle.status (retry telemetry), task.lifecycle.output,
        # task.lifecycle.side_effect_intent (P2 approvals), and anything Muse
        # adds later all land here. Silence is correct: an unknown envelope is
        # not an error, and `status` must never become a ReasoningDelta.
        return []
