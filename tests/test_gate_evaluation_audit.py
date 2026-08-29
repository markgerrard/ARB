"""One-pass resolution-audit tests (Slice 1d-v Task 6 Step 3).

GateEvaluation.audit records facts from that evaluation only: no body, no DSN.
check() is deleted; every caller uses evaluate().
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_redis_bridge import claim_gate
from agent_redis_bridge.envelope import Envelope
from test_bridge_handle_raw import FakeRedis, make_bridge, request_json
from test_bridge_claim_gate import FixedResolver, _enabled_bridge


def make_envelope(payload):
    return Envelope(
        id="e-1",
        sender="claude-x",
        branch="dev",
        recipient="codex-x",
        kind="request",
        sent_at="2026-07-26T00:00:00+00:00",
        payload=payload,
    )


class FakeResolver:
    def __init__(self, *, posture=True, lanes=None, claims=None, raises=None):
        self.posture = posture
        self.lanes = lanes or {}
        self.claims = claims or {}
        self.raises = raises

    def seat_requires_claim_ref(self, seat_id):
        if self.raises == "posture":
            raise claim_gate.StoreUnreachable("posture read failed")
        return self.posture

    def lease_lane(self, lease_id):
        if self.raises == "lane":
            raise claim_gate.StoreUnreachable("lane read failed")
        return self.lanes.get(lease_id)

    def claim(self, claim_ref):
        if self.raises == "claim":
            raise claim_gate.StoreUnreachable("claim read failed")
        return self.claims.get(claim_ref)


def test_check_is_removed():
    assert not hasattr(claim_gate, "check") or not callable(
        getattr(claim_gate, "check", None)
    )
    # Prefer hard absence:
    assert getattr(claim_gate, "check", None) is None


def test_evaluate_audit_shape_no_body_no_dsn():
    ref = {"artefact_id": "art-audit-1", "version": 3}
    evaluation = claim_gate.evaluate(
        make_envelope(
            {
                "task": ref,
                "claim_ref": "c-1",
                "worktree_lease": "lease-1",
            }
        ),
        seat_id="codex-x",
        resolver=FakeResolver(
            posture=True,
            lanes={"lease-1": "gated"},
            claims={
                "c-1": claim_gate.ClaimFacts(
                    confirmed_now=True, attested=True, decorrelation_provenance="wire"
                )
            },
        ),
    )
    assert evaluation.outcome is None
    audit = evaluation.audit
    assert isinstance(audit, dict)
    assert "body" not in audit
    assert "dsn" not in json.dumps(audit).lower()
    assert "password" not in json.dumps(audit).lower()
    assert audit["decision"] == "admit"
    assert audit["brief_ref"] == ref
    assert audit["posture_required"] is True
    assert audit["lease_id"] == "lease-1"
    assert audit["resolved_lane"] == "gated"
    assert audit["claim_ref"] == "c-1"
    assert audit["claim_facts"]["confirmed_now"] is True
    assert audit["claim_facts"]["attested"] is True


def test_evaluate_refusal_carries_audit_decision_code():
    evaluation = claim_gate.evaluate(
        make_envelope({"task": "t"}),
        seat_id="codex-x",
        resolver=FakeResolver(posture=True),
    )
    assert evaluation.outcome is not None
    assert evaluation.outcome.code == claim_gate.MISSING_CLAIM_REF
    assert evaluation.audit["decision"] == claim_gate.MISSING_CLAIM_REF


def test_static_no_direct_resolver_admission_outside_evaluate():
    """Bridge sources must not call seat_requires_claim_ref/lease_lane/claim outside evaluate."""
    root = Path(__file__).resolve().parents[1] / "src" / "agent_redis_bridge"
    forbidden = {"seat_requires_claim_ref", "lease_lane"}
    # claim() is too common a name — restrict to resolver.claim style attribute on known files.
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "claim_gate.py":
            continue  # evaluate itself calls the resolver
        if path.name == "claim_resolver.py":
            continue  # defines the methods
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden:
                offenders.append(f"{path.name}:{node.lineno}:{func.attr}")
            # Match claim on any receiver shape (Name or Attribute chain), not only
            # bare `resolver.claim` — the live bridge handle is
            # `self.claim_resolver.claim(...)` whose receiver is an Attribute.
            if isinstance(func, ast.Attribute) and func.attr == "claim":
                if path.name == "bridge.py":
                    offenders.append(f"{path.name}:{node.lineno}:claim")
    assert offenders == [], f"direct resolver admission calls outside evaluate: {offenders}"


def test_static_no_claim_gate_check_callsites():
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "claim_gate.check" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"claim_gate.check still referenced: {offenders}"


def test_dynamic_exactly_one_evaluate_per_gated_turn():
    calls: list[str] = []
    real_evaluate = claim_gate.evaluate

    def counting_evaluate(*a, **k):
        calls.append("evaluate")
        return real_evaluate(*a, **k)

    bridge, fake = _enabled_bridge(
        FixedResolver(
            posture=False,  # admit without claim
        )
    )
    bridge.check_usage_budget = lambda: "budget-stop"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(claim_gate, "evaluate", counting_evaluate)
        # Also patch the name bound in bridge module.
        mp.setattr("agent_redis_bridge.bridge.claim_gate.evaluate", counting_evaluate)
        bridge.handle_raw(request_json("req-one-eval", payload={"task": "t"}))
    assert calls == ["evaluate"]
    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["error"] == "budget-stop"


def test_audit_after_envelope_sender_before_engine():
    order: list[str] = []
    real_evaluate = claim_gate.evaluate

    def ordered_evaluate(*a, **k):
        order.append("evaluate")
        return real_evaluate(*a, **k)

    bridge, fake = _enabled_bridge(FixedResolver(posture=False))
    bridge.pool.acquire = Mock(side_effect=lambda *a, **k: order.append("engine") or (_ for _ in ()).throw(AssertionError("stop")))
    # Force path far enough that engine would start after gate — budget stops us instead.
    bridge.check_usage_budget = lambda: (order.append("budget") or "budget-stop")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_redis_bridge.bridge.claim_gate.evaluate", ordered_evaluate)
        bridge.handle_raw(request_json("req-order-audit", payload={"task": "t"}))
    assert order[0] == "evaluate"
    assert "engine" not in order
    assert "budget" in order


def test_stale_audit_injection_cannot_admit():
    """Four-step: admit + retain audit; change facts; inject stale audit; require unconfirmed_claim."""
    resolver = FakeResolver(
        posture=True,
        claims={
            "c-1": claim_gate.ClaimFacts(
                confirmed_now=True, attested=True, decorrelation_provenance="wire"
            )
        },
    )
    # 1. admit and retain audit
    first = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=resolver,
    )
    assert first.outcome is None
    stale_audit = first.audit
    assert stale_audit["decision"] == "admit"

    # 2. change resolver facts to unconfirmed
    resolver.claims["c-1"] = claim_gate.ClaimFacts(
        confirmed_now=False, attested=True, decorrelation_provenance="wire"
    )

    # 3. evaluate new dispatch while injecting stale audit into task metadata
    env = make_envelope(
        {
            "task": "t",
            "claim_ref": "c-1",
            "gate_audit": stale_audit,  # injected prior audit — must be ignored
        }
    )
    second = claim_gate.evaluate(env, seat_id="codex-x", resolver=resolver)

    # 4. require exact unconfirmed_claim
    assert second.outcome is not None
    assert second.outcome.code == claim_gate.UNCONFIRMED_CLAIM
    assert second.audit["decision"] == claim_gate.UNCONFIRMED_CLAIM


def test_no_code_path_from_audit_to_admission():
    """Mutating evaluate to consult prior audit must be detectable — inject-only payload never admits."""
    # Empty claim_ref + only stale audit in payload: still missing_claim_ref.
    evaluation = claim_gate.evaluate(
        make_envelope(
            {
                "task": "t",
                "gate_audit": {
                    "decision": "admit",
                    "claim_ref": "c-forged",
                    "claim_facts": {"confirmed_now": True, "attested": True},
                },
            }
        ),
        seat_id="codex-x",
        resolver=FakeResolver(posture=True, claims={}),
    )
    assert evaluation.outcome is not None
    assert evaluation.outcome.code == claim_gate.MISSING_CLAIM_REF
