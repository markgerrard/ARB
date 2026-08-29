"""Codex app-server approval policy — the merge/close gate in codex's wire format.

Codex approvals are BLOCKING server->client requests: the harness stalls until
the client answers. That makes this a stronger control point than a Claude
PreToolUse hook (the harness cannot proceed without us) and simultaneously a
stricter obligation — there is no ambient permission layer behind us, so "no
opinion" is not an answerable value. Every request gets an answer, and the
answer comes from policy:

    1. the runtime-agnostic merge/close gate (`gates.evaluate_merge_close`)
    2. if the gate has no opinion, the injected base policy

The base policy is REQUIRED, never defaulted, so the posture is visible at the
call site. `ApproveNonGated` encodes the owner's ruling for the orchestrator
seat — capability broad, gates at the choke points — and is the exact codex
analogue of the Claude runner's broad `ORCHESTRATOR_TOOLS` grant. Narrowing it
is a doctrine decision, not a refactor.

Protocol facts below are verified against `codex app-server generate-json-schema`
(codex-cli 0.146.0), not against adapter prose.

WIRE ASYMMETRY — the honest limit of this layer. The v1 `denied` decision
carries a `rejection` string, so a specific refusal code reaches the model. The
new-gen `decline` is a bare enum: `CommandExecutionRequestApprovalResponse` has
exactly one property (`decision`), with nowhere to put a reason. On the live
path (the shipping adapter subscribes only to the new-gen pair) the code
therefore CANNOT travel to the model. Rather than let a bare decline pass as a
coded refusal, every answer is appended to `records` with its code — the code
lands somewhere falsifiable even when the protocol drops it. "Every answer"
includes the unhandled-method refusals, which are answered AND recorded.

FAIL CLOSED ON UNREADABLE COMMANDS. `command` is OPTIONAL in the protocol
(`CommandExecutionRequestApprovalParams` requires only itemId, startedAtMs,
threadId, turnId), so a command approval can arrive with nothing to inspect.
Such a request is REFUSED under `codex-approval-command-unreadable` rather than
handed to the base policy: the owner ruling is "approve non-GATED actions", and
an action nobody can read is not demonstrably non-gated. `commandActions` is
documented as best-effort parsing for friendly DISPLAY, so reconstructing the
command from it would rebuild the same hole one layer down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .gates import NO_OPINION, EvidenceResolver, GateDecision, evaluate_merge_close

# JSON-RPC method-not-found; the message carries our own specific code.
METHOD_NOT_FOUND = -32601

# The approvals whose response is decision-shaped and whose decision values are
# verified from the generated schema. `item/permissions/requestApproval` is
# deliberately NOT here: its response is a GrantedPermissionProfile, not a
# decision, so there is no honest deny without modelling that type. It falls
# through to the specific-coded error, which stalls the turn visibly instead of
# fabricating a grant.
NEWGEN_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
NEWGEN_FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
V1_EXEC_APPROVAL = "execCommandApproval"
V1_APPLY_PATCH_APPROVAL = "applyPatchApproval"

APPROVAL_METHODS = frozenset(
    {
        NEWGEN_COMMAND_APPROVAL,
        NEWGEN_FILE_CHANGE_APPROVAL,
        V1_EXEC_APPROVAL,
        V1_APPLY_PATCH_APPROVAL,
    }
)

_V1_METHODS = frozenset({V1_EXEC_APPROVAL, V1_APPLY_PATCH_APPROVAL})
_COMMAND_METHODS = frozenset({NEWGEN_COMMAND_APPROVAL, V1_EXEC_APPROVAL})


@dataclass(frozen=True)
class ApprovalAnswer:
    """Exactly one of `result` / `error` — a JSON-RPC response body."""

    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApprovalRecord:
    """One answered approval, with the code even when the wire drops it."""

    method: str
    denied: bool
    code: str = ""
    detail: str = ""


class BasePolicy(Protocol):
    def decide(self, method: str, params: dict[str, Any]) -> GateDecision: ...


@dataclass(frozen=True)
class ApproveNonGated:
    """Owner ruling: capability broad, control at the gates (not starvation)."""

    def decide(self, method: str, params: dict[str, Any]) -> GateDecision:
        return NO_OPINION


@dataclass(frozen=True)
class DenyNonGated:
    """Fail-closed posture: nothing runs unless some layer positively allows it."""

    code: str = "codex-approval-base-policy-deny"

    def decide(self, method: str, params: dict[str, Any]) -> GateDecision:
        return GateDecision(denied=True, code=self.code)


UNREADABLE_COMMAND_CODE = "codex-approval-command-unreadable"
UNHANDLED_METHOD_CODE = "codex-approval-unhandled-method"


def _command_text(method: str, params: dict[str, Any]) -> str:
    """The command as one string, so the gate's patterns can match it.

    v1 sends `command` as an argv array; the new-gen sends it as a string, and
    the field is OPTIONAL — `CommandExecutionRequestApprovalParams` requires
    only itemId, startedAtMs, threadId and turnId — so it may be absent or null.
    """
    command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    return str(command or "").strip()


class CodexApprovalPolicy:
    def __init__(self, *, evidence: EvidenceResolver, base_policy: BasePolicy) -> None:
        self.evidence = evidence
        self.base_policy = base_policy
        self.records: list[ApprovalRecord] = []

    # ------------------------------------------------------------ decide

    def _gate_decision(self, method: str, params: dict[str, Any]) -> GateDecision:
        if method not in _COMMAND_METHODS:
            return NO_OPINION
        command = _command_text(method, params)
        if not command:
            # Fail CLOSED. The owner ruling is "approve non-GATED actions"; an
            # action whose command text cannot be read is not demonstrably
            # non-gated, so treating unreadable as ungated would open a hole at
            # exactly the point the gate exists to cover. `commandActions` is
            # documented as "best-effort parsed ... for friendly display", so
            # reconstructing from it would rebuild the hole one layer down.
            return GateDecision(
                denied=True,
                code=UNREADABLE_COMMAND_CODE,
                detail=f"{method} carried no readable command text",
            )
        return evaluate_merge_close(self.evidence, "Bash", {"command": command})

    # ------------------------------------------------------------ answer

    def answer(self, method: str, params: dict[str, Any]) -> ApprovalAnswer:
        if method not in APPROVAL_METHODS:
            detail = f"{method} is not a decision-shaped approval this policy answers"
            # Recorded like any other answer — the docstring's "every answer" is
            # only true if the refusals are in the log too.
            self.records.append(
                ApprovalRecord(
                    method=method,
                    denied=True,
                    code=UNHANDLED_METHOD_CODE,
                    detail=detail,
                )
            )
            return ApprovalAnswer(
                error={
                    "code": METHOD_NOT_FOUND,
                    "message": f"{UNHANDLED_METHOD_CODE}: {detail}",
                }
            )

        decision = self._gate_decision(method, params)
        if not decision.denied:
            decision = self.base_policy.decide(method, params)

        self.records.append(
            ApprovalRecord(
                method=method,
                denied=decision.denied,
                code=decision.code,
                detail=decision.detail,
            )
        )
        return ApprovalAnswer(result=self._wire_decision(method, decision))

    def _wire_decision(self, method: str, decision: GateDecision) -> dict[str, Any]:
        if method in _V1_METHODS:
            if not decision.denied:
                return {"decision": "approved"}
            # v1 carries the code to the model; use it.
            return {"decision": {"denied": {"rejection": decision.reason}}}
        if not decision.denied:
            return {"decision": "accept"}
        # new-gen has nowhere for the reason. `decline` (not `cancel`) matches
        # the Claude hook's deny semantics: the agent continues the turn and
        # tries something else rather than the turn being interrupted.
        return {"decision": "decline"}
