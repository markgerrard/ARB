from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from typing import Any
from uuid import uuid4

from .brief_ref import BriefRefError, is_brief_ref_shape, parse_brief_ref


ALLOWED_KINDS = {"cancel", "hello", "request", "reply", "steer", "notify"}

# Dual-accept default. Stage 1d-vi canary flips this seat-by-seat; strings then
# fail as invalid-payload-task-ref before the claim gate.
TASK_REF_REQUIRED_ENV = "BRIDGE_TASK_REF_REQUIRED"


class EnvelopeError(ValueError):
    """Envelope parse failure.

    ``header`` is set only when header validation fully succeeded and a later
    kind-/run_id-specific check failed. Earlier failures leave it ``None``.
    """

    def __init__(self, reason: str, *, header: "EnvelopeHeader | None" = None) -> None:
        super().__init__(reason)
        self.header = header


def task_ref_required(*, env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(TASK_REF_REQUIRED_ENV, "0")).lower() in {"1", "true", "yes"}


def validate_request_task(task: Any, *, ref_required: bool | None = None) -> None:
    """Dual-accept (default) or ref-required validation for payload.task.

    - legacy nonblank string OR exact BriefRef object when dual-accept
    - ref-required: only exact BriefRef; strings fail invalid-payload-task-ref
    - malformed ref-shaped objects always fail invalid-payload-task-ref
    - missing/blank non-ref task fails missing-payload-task
    """
    required = task_ref_required() if ref_required is None else ref_required
    if is_brief_ref_shape(task) or isinstance(task, dict):
        try:
            parse_brief_ref(task)
        except BriefRefError as exc:
            raise EnvelopeError(exc.reason) from exc
        return
    if required:
        # Strings and other non-ref shapes are refused once ref-required is on.
        raise EnvelopeError("invalid-payload-task-ref")
    if not isinstance(task, str) or not task.strip():
        raise EnvelopeError("missing-payload-task")


@dataclass(frozen=True)
class EnvelopeHeader:
    """Validated wire header + payload dict (pre kind-specific checks).

    Field names mirror ``Envelope`` (sender/recipient), not the JSON keys.
    """

    id: str
    sender: str
    branch: str
    recipient: str
    kind: str
    sent_at: str
    payload: dict[str, Any]
    in_reply_to: str | None = None


def _load_envelope_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"invalid-json: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise EnvelopeError("envelope-not-object")
    return value


def _header_from_value(value: dict[str, Any]) -> EnvelopeHeader:
    """Validate header fields through in_reply_to coherence (design §8 item 1)."""
    required = ("id", "from", "branch", "to", "kind", "sent_at", "payload")
    for field in required:
        if field not in value:
            raise EnvelopeError(f"missing-{field}")

    for field in ("id", "from", "branch", "to", "kind", "sent_at"):
        if not isinstance(value[field], str) or not value[field]:
            raise EnvelopeError(f"invalid-{field}")

    if value["kind"] not in ALLOWED_KINDS:
        raise EnvelopeError(f"invalid-kind:{value['kind']}")

    if not isinstance(value["payload"], dict):
        raise EnvelopeError("invalid-payload")

    in_reply_to = value.get("in_reply_to")
    if value["kind"] == "reply":
        if not isinstance(in_reply_to, str) or not in_reply_to:
            raise EnvelopeError("missing-in_reply_to")
    elif in_reply_to is not None:
        raise EnvelopeError("unexpected-in_reply_to")

    return EnvelopeHeader(
        id=value["id"],
        sender=value["from"],
        branch=value["branch"],
        recipient=value["to"],
        kind=value["kind"],
        sent_at=value["sent_at"],
        payload=value["payload"],
        in_reply_to=in_reply_to,
    )


def parse_header(raw: str) -> EnvelopeHeader:
    """Parse and validate envelope header fields through in_reply_to coherence.

    Kind-specific payload rules and run_id checks are NOT applied here.
    """
    return _header_from_value(_load_envelope_object(raw))


@dataclass(frozen=True)
class Envelope:
    id: str
    sender: str
    branch: str
    recipient: str
    kind: str
    sent_at: str
    payload: dict[str, Any]
    in_reply_to: str | None = None
    run_id: str | None = None

    @classmethod
    def from_json(cls, raw: str, *, ref_required: bool | None = None) -> "Envelope":
        value = _load_envelope_object(raw)
        header = _header_from_value(value)
        value_kind = header.kind
        payload = header.payload

        try:
            if value_kind == "request":
                task = payload.get("task")
                operation = payload.get("operation")
                taskless_operation = operation in {"worktree_arm", "worktree_release"}
                if not taskless_operation:
                    validate_request_task(task, ref_required=ref_required)
                thread_id = payload.get("thread_id")
                if "thread_id" in payload and (not isinstance(thread_id, str) or not thread_id.strip()):
                    raise EnvelopeError("invalid-payload-thread_id")
                fork_from_thread_id = payload.get("fork_from_thread_id")
                if "fork_from_thread_id" in payload and (
                    not isinstance(fork_from_thread_id, str) or not fork_from_thread_id.strip()
                ):
                    raise EnvelopeError("invalid-payload-fork_from_thread_id")
                # Gate fields (spec §5.1/§5.3): TYPE checks only. Admissibility is decided by
                # claim_gate against the store, and `lane` is routing metadata whose meaning lives
                # in lease_lanes — validating its VALUE here would put lane semantics in the one
                # layer the dispatcher controls.
                for gate_field in ("claim_ref", "lane"):
                    if gate_field in payload:
                        supplied = payload[gate_field]
                        if not isinstance(supplied, str) or not supplied.strip():
                            raise EnvelopeError(f"invalid-payload-{gate_field}")
                has_thread_id = isinstance(thread_id, str) and bool(thread_id.strip())
                has_fork_from_thread_id = isinstance(fork_from_thread_id, str) and bool(
                    fork_from_thread_id.strip()
                )
                if has_thread_id and has_fork_from_thread_id:
                    raise EnvelopeError("contradictory-context")
                if (has_thread_id or has_fork_from_thread_id) and payload.get("fresh_context") is True:
                    raise EnvelopeError("contradictory-context")
            elif value_kind == "steer":
                message = payload.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise EnvelopeError("missing-payload-message")

            # run_id sits outside the kind block but still post-header; attach header on
            # failure so allowlisted invalid-run_id is addressable (design §2 / guard 2).
            run_id = value.get("run_id")
            if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
                raise EnvelopeError("invalid-run_id")
        except EnvelopeError as exc:
            if exc.header is None:
                raise EnvelopeError(str(exc), header=header) from exc
            raise

        return cls(
            id=header.id,
            sender=header.sender,
            branch=header.branch,
            recipient=header.recipient,
            kind=header.kind,
            sent_at=header.sent_at,
            payload=header.payload,
            in_reply_to=header.in_reply_to,
            run_id=run_id if isinstance(run_id, str) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "from": self.sender,
            "branch": self.branch,
            "to": self.recipient,
            "kind": self.kind,
            "sent_at": self.sent_at,
            "payload": self.payload,
        }
        if self.in_reply_to is not None:
            data["in_reply_to"] = self.in_reply_to
        if self.run_id is not None:
            data["run_id"] = self.run_id

        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_reply(
    *,
    sender: str,
    recipient: str,
    branch: str,
    in_reply_to: str,
    payload: dict[str, Any],
) -> Envelope:
    return Envelope(
        id=str(uuid4()),
        sender=sender,
        branch=branch,
        recipient=recipient,
        kind="reply",
        in_reply_to=in_reply_to,
        sent_at=iso_now(),
        payload=payload,
    )


def make_notify(
    *,
    sender: str,
    recipient: str,
    branch: str,
    event: str,
    data: dict[str, Any],
) -> Envelope:
    return Envelope(
        id=str(uuid4()),
        sender=sender,
        branch=branch,
        recipient=recipient,
        kind="notify",
        sent_at=iso_now(),
        payload={"event": event, "data": data},
    )
