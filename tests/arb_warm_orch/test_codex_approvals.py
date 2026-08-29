"""Codex app-server approval policy — the codex-side merge/close gate seam.

Why this exists as its own layer: codex approvals are BLOCKING server->client
requests. The harness stalls until the client answers, so unlike a Claude
PreToolUse hook there is no ambient permission layer behind us — "no opinion"
is not an available answer on the wire. Every request must be answered, and the
answer must be derivable from policy.

Two generations of approval request are live in the protocol (verified against
`codex app-server generate-json-schema`, codex-cli 0.146.0):

  new-gen  item/commandExecution/requestApproval  decision: accept|decline|...
           item/fileChange/requestApproval        decision: accept|decline|...
  v1       execCommandApproval                    decision: approved|{denied:{rejection}}|...
           applyPatchApproval                     decision: approved|{denied:{rejection}}|...

The shipping reference adapter (repos/codex-acp CodexAppServerClient.ts:101,107)
subscribes only to the new-gen pair, so that is the live path; v1 is handled too
because a version skew must not silently open the gated hole.

ASYMMETRY WORTH KNOWING (pinned by test): the v1 `denied` variant carries a
`rejection` string, so the specific refusal code travels ON THE WIRE to the
model. The new-gen `decline` is a bare enum string with nowhere to put a reason
— `CommandExecutionRequestApprovalResponse` has exactly one property. So on the
live path the code CANNOT reach the model, and the honest response is to record
it in a decision log rather than pretend a bare decline is a coded refusal
(refusal-is-ambient-assert-the-code). One test per named behavior, red-first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from arb_warm_orch.codex_approvals import (
    ApproveNonGated,
    CodexApprovalPolicy,
    DenyNonGated,
)
from arb_warm_orch.gates import EvidenceCheck


@dataclass
class StubEvidenceResolver:
    check: EvidenceCheck = field(default_factory=lambda: EvidenceCheck(resolvable=True))
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def resolve(self, tool_name: str, tool_input: dict) -> EvidenceCheck:
        self.calls.append((tool_name, tool_input))
        return self.check


def _policy(check: EvidenceCheck | None = None, base=None) -> CodexApprovalPolicy:
    return CodexApprovalPolicy(
        evidence=StubEvidenceResolver(check or EvidenceCheck(resolvable=True)),
        base_policy=base or ApproveNonGated(),
    )


def _newgen_exec(command: str) -> dict:
    return {
        "itemId": "item-1",
        "threadId": "th-1",
        "turnId": "turn-1",
        "startedAtMs": 0,
        "command": command,
    }


def _v1_exec(command: list[str]) -> dict:
    return {
        "callId": "call-1",
        "command": command,
        "conversationId": "th-1",
        "cwd": "/tmp",
        "parsedCmd": [],
    }


UNRESOLVED = EvidenceCheck(resolvable=False, detail="no close record")


# ------------------------------------------------- new-gen: the live path


def test_newgen_non_gated_command_is_accepted_under_broad_capability():
    answer = _policy().answer("item/commandExecution/requestApproval", _newgen_exec("ls -la"))
    assert answer.result == {"decision": "accept"}


def test_newgen_unresolved_merge_is_declined():
    answer = _policy(UNRESOLVED).answer(
        "item/commandExecution/requestApproval", _newgen_exec("git merge dev")
    )
    assert answer.result == {"decision": "decline"}


def test_newgen_decline_records_the_specific_code_the_wire_cannot_carry():
    policy = _policy(UNRESOLVED)
    policy.answer("item/commandExecution/requestApproval", _newgen_exec("git merge dev"))
    record = policy.records[-1]
    assert record.denied is True
    assert record.code == "merge-close-evidence-unresolved"
    assert record.detail == "no close record"
    assert record.method == "item/commandExecution/requestApproval"


def test_newgen_file_change_is_not_command_shaped_so_base_policy_answers():
    answer = _policy(UNRESOLVED).answer(
        "item/fileChange/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0},
    )
    assert answer.result == {"decision": "accept"}


def test_newgen_resolver_crash_declines_under_its_own_code():
    class ExplodingResolver:
        def resolve(self, tool_name, tool_input):
            raise RuntimeError("store unreachable")

    policy = CodexApprovalPolicy(
        evidence=ExplodingResolver(), base_policy=ApproveNonGated()
    )
    answer = policy.answer(
        "item/commandExecution/requestApproval", _newgen_exec("git merge dev")
    )
    assert answer.result == {"decision": "decline"}
    assert policy.records[-1].code == "merge-close-evidence-check-failed"
    assert "store unreachable" in policy.records[-1].detail


# --------------------------------------------------------- v1: legacy path


def test_v1_unresolved_merge_denies_with_the_code_on_the_wire():
    answer = _policy(UNRESOLVED).answer(
        "execCommandApproval", _v1_exec(["git", "merge", "dev"])
    )
    rejection = answer.result["decision"]["denied"]["rejection"]
    assert "merge-close-evidence-unresolved" in rejection
    assert "no close record" in rejection


def test_v1_joins_the_command_array_so_the_gate_pattern_can_match():
    policy = _policy(UNRESOLVED)
    policy.answer("execCommandApproval", _v1_exec(["git", "merge", "dev"]))
    assert policy.evidence.calls == [("Bash", {"command": "git merge dev"})]


def test_v1_non_gated_command_is_approved_under_broad_capability():
    answer = _policy().answer("execCommandApproval", _v1_exec(["ls", "-la"]))
    assert answer.result == {"decision": "approved"}


def test_v1_apply_patch_is_not_command_shaped_so_base_policy_answers():
    answer = _policy(UNRESOLVED).answer(
        "applyPatchApproval",
        {"callId": "c", "conversationId": "t", "fileChanges": {}},
    )
    assert answer.result == {"decision": "approved"}


# ------------------------------------------------------------ base policy


def test_deny_base_policy_refuses_non_gated_work_with_a_specific_code():
    policy = _policy(base=DenyNonGated())
    answer = policy.answer("item/commandExecution/requestApproval", _newgen_exec("ls"))
    assert answer.result == {"decision": "decline"}
    assert policy.records[-1].code == "codex-approval-base-policy-deny"


def test_gate_denial_outranks_an_approving_base_policy():
    policy = _policy(UNRESOLVED, base=ApproveNonGated())
    answer = policy.answer(
        "item/commandExecution/requestApproval", _newgen_exec("git merge dev")
    )
    assert answer.result == {"decision": "decline"}


# ----------------------------------------------------- must always answer


def test_unhandled_approval_method_errors_with_a_specific_code():
    answer = _policy().answer("item/permissions/requestApproval", {})
    assert answer.result is None
    assert "codex-approval-unhandled-method" in answer.error["message"]


def test_unhandled_method_error_names_the_method_it_refused():
    answer = _policy().answer("item/permissions/requestApproval", {})
    assert "item/permissions/requestApproval" in answer.error["message"]


def test_every_answer_is_recorded_including_approvals():
    policy = _policy()
    policy.answer("item/commandExecution/requestApproval", _newgen_exec("ls"))
    assert len(policy.records) == 1
    assert policy.records[0].denied is False


def test_policy_handles_exactly_the_four_decision_shaped_methods():
    from arb_warm_orch.codex_approvals import APPROVAL_METHODS

    assert APPROVAL_METHODS == frozenset(
        {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }
    )


# ------------------------------- panel remediation: unreadable command text
#
# Panel finding (codex F2 / cold-Opus P2-4). `command` is OPTIONAL in the
# protocol — CommandExecutionRequestApprovalParams requires only itemId,
# startedAtMs, threadId and turnId — so a protocol-valid approval can arrive
# with no command text. The old code turned that into "", found no merge
# pattern, and let the base policy ACCEPT it.
#
# The owner ruling is "approve non-GATED actions". An action whose command text
# cannot be read is not demonstrably non-gated, so treating unreadable as
# ungated is a fail-OPEN at exactly the point the gate exists to cover.
# `commandActions` is documented as "best-effort parsed ... for friendly
# display", so reconstructing from it would rebuild the same hole one layer
# down: the answer is to refuse.


def test_command_approval_without_command_text_is_refused():
    answer = _policy().answer(
        "item/commandExecution/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0},
    )
    assert answer.result == {"decision": "decline"}


def test_unreadable_command_refusal_carries_its_own_code():
    policy = _policy()
    policy.answer(
        "item/commandExecution/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0},
    )
    assert policy.records[-1].code == "codex-approval-command-unreadable"


def test_explicitly_null_command_is_refused_too():
    answer = _policy().answer(
        "item/commandExecution/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0, "command": None},
    )
    assert answer.result == {"decision": "decline"}


def test_blank_command_is_refused_too():
    answer = _policy().answer(
        "item/commandExecution/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0, "command": "   "},
    )
    assert answer.result == {"decision": "decline"}


def test_v1_exec_approval_with_empty_argv_is_refused():
    answer = _policy().answer(
        "execCommandApproval",
        {"callId": "c", "command": [], "conversationId": "t", "cwd": "/tmp", "parsedCmd": []},
    )
    assert answer.result["decision"]["denied"]["rejection"].startswith(
        "codex-approval-command-unreadable"
    )


def test_file_change_approval_still_answers_without_command_text():
    # Only COMMAND approvals carry command text; a file-change approval has no
    # command by design and must not be swept up by the unreadable-command rule.
    answer = _policy().answer(
        "item/fileChange/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0},
    )
    assert answer.result == {"decision": "accept"}


# ------------------------------ panel remediation: record EVERY answer given
#
# Panel finding (codex F3 / cold-Opus P2-1): the module docstring promised every
# answer is recorded, but the unhandled-method branch returned before the
# append. Overclaiming prose is a defect exactly as an overclaiming test is.


def test_unhandled_method_answer_is_recorded_too():
    policy = _policy()
    policy.answer("item/permissions/requestApproval", {})
    assert len(policy.records) == 1
    assert policy.records[0].denied is True
    assert policy.records[0].code == "codex-approval-unhandled-method"
    assert policy.records[0].method == "item/permissions/requestApproval"
