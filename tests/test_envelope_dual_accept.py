"""Bidirectional mixed-version dual-accept tests (Slice 1d-iv Task 5 Step 1).

With BRIDGE_CLAIM_GATE=0 and BRIDGE_TASK_REF_REQUIRED=0, envelopes accept exact
refs and legacy nonblank strings for ordinary requests/worktree_run; arm/release
remain taskless. Malformed refs fail exactly invalid-payload-task-ref.
"""

from __future__ import annotations

import json

import pytest

from agent_redis_bridge.envelope import Envelope, EnvelopeError, validate_request_task


def _raw(payload: dict, *, run_id: str | None = None) -> str:
    body = {
        "id": "e-1",
        "from": "claude-x",
        "branch": "dev",
        "to": "codex-x",
        "kind": "request",
        "sent_at": "2026-07-28T00:00:00+00:00",
        "payload": payload,
    }
    if run_id is not None:
        body["run_id"] = run_id
    return json.dumps(body)


def test_dual_accept_legacy_string():
    env = Envelope.from_json(_raw({"task": "run git status"}), ref_required=False)
    assert env.payload["task"] == "run git status"


def test_dual_accept_exact_ref():
    ref = {"artefact_id": "art-brief-1", "version": 2}
    env = Envelope.from_json(_raw({"task": ref}), ref_required=False)
    assert env.payload["task"] == ref


def test_worktree_run_accepts_ref_and_string():
    for task in ("legacy body", {"artefact_id": "a", "version": 1}):
        env = Envelope.from_json(
            _raw({"operation": "worktree_run", "task": task, "worktree_lease": "L1"}),
            ref_required=False,
        )
        assert env.payload["task"] == task


def test_arm_release_remain_taskless():
    arm = Envelope.from_json(
        _raw(
            {
                "operation": "worktree_arm",
                "worktree": {"name": "w", "cleanup": "keep"},
            }
        ),
        ref_required=False,
    )
    assert "task" not in arm.payload or arm.payload.get("task") in (None, "")
    rel = Envelope.from_json(
        _raw({"operation": "worktree_release", "worktree_lease": "L1"}),
        ref_required=False,
    )
    assert rel.payload["operation"] == "worktree_release"


@pytest.mark.parametrize(
    "bad",
    [
        {"artefact_id": "", "version": 1},
        {"artefact_id": "a", "version": 0},
        {"artefact_id": "a", "version": -1},
        {"artefact_id": "a", "version": True},
        {"artefact_id": "a", "version": "1"},
        {"artefact_id": "a"},
        {"version": 1},  # missing artefact_id but still ref-shaped via is_brief_ref? no — no artefact_id key
        {"artefact_id": 1, "version": 1},
    ],
)
def test_malformed_ref_is_invalid_payload_task_ref(bad):
    # Any mapping task is treated as a ref attempt under dual-accept — malformed
    # shapes fail exactly invalid-payload-task-ref (never missing-payload-task).
    with pytest.raises(EnvelopeError, match="invalid-payload-task-ref"):
        Envelope.from_json(_raw({"task": bad}), ref_required=False)


def test_blank_string_still_missing_payload_task():
    with pytest.raises(EnvelopeError, match="missing-payload-task"):
        Envelope.from_json(_raw({"task": "   "}), ref_required=False)


def test_ref_required_rejects_legacy_string():
    with pytest.raises(EnvelopeError, match="invalid-payload-task-ref"):
        Envelope.from_json(_raw({"task": "legacy"}), ref_required=True)


def test_ref_required_accepts_exact_ref():
    ref = {"artefact_id": "art-1", "version": 1}
    env = Envelope.from_json(_raw({"task": ref}), ref_required=True)
    assert env.payload["task"] == ref


def test_validate_request_task_shared_rules():
    validate_request_task("ok", ref_required=False)
    validate_request_task({"artefact_id": "a", "version": 1}, ref_required=False)
    with pytest.raises(EnvelopeError, match="invalid-payload-task-ref"):
        validate_request_task({"artefact_id": "a", "version": 0}, ref_required=False)
