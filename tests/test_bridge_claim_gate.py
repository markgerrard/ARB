"""Exact-code request-path integration for the bus-side claim gate (Slice 1c).

No Postgres. Inject fake resolvers; every refusal must pin the Slice 1a code
prefix and routing gaps. Bare `ok is False` is ambient and not sufficient.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from agent_redis_bridge import claim_gate
from agent_redis_bridge.claim_resolver import PsycopgClaimResolver
from test_bridge_handle_raw import FakeRedis, make_bridge, request_json


class FixedResolver:
    def __init__(self, *, posture=True, lanes=None, claims=None, raises=None):
        self.posture = posture
        self.lanes = lanes or {}
        self.claims = claims or {}
        self.raises = raises
        self.calls: list[str] = []

    def seat_requires_claim_ref(self, seat_id):
        self.calls.append("posture")
        if self.raises == "posture":
            raise claim_gate.StoreUnreachable("posture read failed")
        return self.posture

    def lease_lane(self, lease_id):
        self.calls.append("lane")
        if self.raises == "lane":
            raise claim_gate.StoreUnreachable("lane read failed")
        return self.lanes.get(lease_id)

    def claim(self, claim_ref):
        self.calls.append("claim")
        if self.raises == "claim":
            raise claim_gate.StoreUnreachable("claim read failed")
        return self.claims.get(claim_ref)


class ResolverThatRaises:
    def __init__(self, exc: BaseException):
        self.exc = exc

    def seat_requires_claim_ref(self, seat_id):
        raise self.exc

    def lease_lane(self, lease_id):
        raise self.exc

    def claim(self, claim_ref):
        raise self.exc


class NonPostureBearingResolver:
    def __init__(self, order: list[str]):
        self.order = order

    def seat_requires_claim_ref(self, seat_id):
        self.order.append("posture")
        return False

    def lease_lane(self, lease_id):
        raise AssertionError("open seat must not consult lanes")

    def claim(self, claim_ref):
        raise AssertionError("open seat must not consult claims")


def _enabled_bridge(resolver):
    bridge = make_bridge("--no-enforce-completion")
    fake = FakeRedis()
    bridge.redis = fake  # type: ignore[assignment]
    bridge.claim_gate_enabled = True
    bridge.claim_resolver = resolver
    return bridge, fake


@pytest.mark.parametrize(
    ("payload", "resolver", "expected_code", "route_fragments"),
    [
        (
            {"task": "t"},
            FixedResolver(posture=True),
            claim_gate.MISSING_CLAIM_REF,
            ("exempt", "build the probe"),
        ),
        (
            {"task": "t", "claim_ref": "no-such"},
            FixedResolver(posture=True, claims={}),
            claim_gate.UNKNOWN_CLAIM_REF,
            ("typo", "stale reference"),
        ),
        (
            {"task": "t", "claim_ref": "c-1"},
            FixedResolver(
                posture=True,
                claims={
                    "c-1": claim_gate.ClaimFacts(
                        confirmed_now=False, attested=False, decorrelation_provenance="none"
                    )
                },
            ),
            claim_gate.UNCONFIRMED_CLAIM,
            ("exempt", "build the probe"),
        ),
        (
            {"task": "t", "claim_ref": "c-1"},
            FixedResolver(
                posture=True,
                claims={
                    "c-1": claim_gate.ClaimFacts(
                        confirmed_now=True, attested=False, decorrelation_provenance="none"
                    )
                },
            ),
            claim_gate.UNATTESTED_CLAIM,
            ("verification",),
        ),
        (
            {
                "task": "t",
                "worktree_lease": "unknown-lease",
                "lane": "exempt",
            },
            FixedResolver(posture=True, lanes={}),
            claim_gate.LANE_NOT_ARMED_EXEMPT,
            ("armed by the consumer",),
        ),
        (
            {"task": "t", "claim_ref": "c-1"},
            FixedResolver(raises="posture"),
            claim_gate.STORE_UNREACHABLE,
            ("operator", "authority is unavailable"),
        ),
    ],
)
def test_gate_refusal_pins_exact_code_and_routing(
    payload, resolver, expected_code, route_fragments
):
    bridge, fake = _enabled_bridge(resolver)
    bridge.pool.acquire = Mock(
        side_effect=AssertionError("engine work must not start")
    )

    bridge.handle_raw(request_json("req-gate", payload=payload))

    # Admission audit may emit gate_evaluation (and live-tee mirrors) before refusal
    # reply; no engine work / task_started.
    engine_like = [
        fields
        for _, fields in fake.events
        if fields.get("type") in {"task_started", "task_finished", "command_started"}
        or fields.get("event_type") in {"task_started", "task_finished", "command_started"}
    ]
    assert engine_like == []
    assert any(
        fields.get("type") == "gate_evaluation" or fields.get("event_type") == "gate_evaluation"
        for _, fields in fake.events
    )
    assert len(fake.replies) == 1
    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["ok"] is False
    error = reply["payload"]["error"]
    assert error.split(":", 1)[0] == expected_code
    assert ":" in error
    assert any(fragment in error for fragment in route_fragments)

    if expected_code == claim_gate.STORE_UNREACHABLE:
        assert not error.startswith("unconfirmed_claim")
        assert not error.startswith("unattested_claim")
        assert not error.startswith("unknown_claim_ref")


def test_malformed_falsey_posture_at_request_boundary_is_store_unreachable():
    """Truthiness mutation must fail closed at the actual dispatcher boundary."""

    class Factory:
        def __call__(self, dsn, **kwargs):
            class Conn:
                closed = False

                def execute(self, statement, params=None):
                    class R:
                        def fetchone(self_inner):
                            return {"requires_claim_ref": 0}

                    return R()

                def close(self):
                    self.closed = True

            return Conn()

    resolver = PsycopgClaimResolver("unused", connect=Factory())
    bridge, fake = _enabled_bridge(resolver)
    bridge.pool.acquire = Mock(
        side_effect=AssertionError("engine work must not start")
    )

    bridge.handle_raw(request_json("req-falsey", payload={"task": "t"}))

    reply = json.loads(fake.replies[0][1])
    error = reply["payload"]["error"]
    assert error.split(":", 1)[0] == claim_gate.STORE_UNREACHABLE
    assert "operator" in error or "authority is unavailable" in error
    engine_like = [
        fields
        for _, fields in fake.events
        if fields.get("type") in {"task_started", "task_finished"}
        or fields.get("event_type") in {"task_started", "task_finished"}
    ]
    assert engine_like == []


def test_rejected_sender_never_calls_the_store():
    bridge = make_bridge("--no-enforce-completion", "--unknown-sender-policy", "reject")
    fake = FakeRedis()
    bridge.redis = fake  # type: ignore[assignment]
    bridge.claim_gate_enabled = True
    bridge.claim_resolver = ResolverThatRaises(
        AssertionError("gate must not run")
    )

    bridge.handle_raw(request_json("req-rej", sender="unknown-agent"))

    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["error"] == "sender rejected: unknown-agent"


def test_invalid_turn_timeout_never_calls_the_store():
    bridge = make_bridge(
        "--no-enforce-completion",
        "--turn-timeout",
        "60",
        "--turn-timeout-max",
        "300",
    )
    fake = FakeRedis()
    bridge.redis = fake  # type: ignore[assignment]
    bridge.claim_gate_enabled = True
    bridge.claim_resolver = ResolverThatRaises(
        AssertionError("gate must not run")
    )

    bridge.handle_raw(
        request_json(
            "req-tt",
            payload={"task": "t", "turn_timeout": 99999},
        )
    )

    reply = json.loads(fake.replies[0][1])
    assert "turn_timeout" in reply["payload"]["error"]


def test_gate_refusal_precedes_duplicate_and_budget():
    bridge, fake = _enabled_bridge(FixedResolver(posture=True))
    bridge.is_duplicate = Mock(
        side_effect=AssertionError("duplicate check must be later")
    )
    bridge.check_usage_budget = Mock(
        side_effect=AssertionError("budget check must be later")
    )

    bridge.handle_raw(request_json("req-order", payload={"task": "t"}))

    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["error"].startswith("missing_claim_ref")


def test_gate_admission_reaches_duplicate_then_budget():
    order: list[str] = []
    bridge, fake = _enabled_bridge(NonPostureBearingResolver(order))
    bridge.is_duplicate = lambda _id: order.append("duplicate") or False
    bridge.check_usage_budget = lambda: order.append("budget") or "budget-stop"

    bridge.handle_raw(request_json("req-admit", payload={"task": "t"}))

    assert order == ["posture", "duplicate", "budget"]
    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["error"] == "budget-stop"


def test_gate_off_request_path_never_calls_the_resolver():
    bridge = make_bridge("--no-enforce-completion")
    fake = FakeRedis()
    bridge.redis = fake  # type: ignore[assignment]
    bridge.claim_gate_enabled = False
    bridge.claim_resolver = ResolverThatRaises(
        AssertionError("disabled gate touched resolver")
    )
    bridge.check_usage_budget = lambda: "budget-stop"

    bridge.handle_raw(request_json("req-off", payload={"task": "t"}))

    reply = json.loads(fake.replies[0][1])
    assert reply["payload"]["error"] == "budget-stop"


def test_missing_resolver_when_enabled_is_store_unreachable_not_admit():
    bridge, fake = _enabled_bridge(None)
    bridge.claim_resolver = None
    bridge.pool.acquire = Mock(
        side_effect=AssertionError("engine work must not start")
    )

    bridge.handle_raw(request_json("req-none", payload={"task": "t"}))

    error = json.loads(fake.replies[0][1])["payload"]["error"]
    assert error.split(":", 1)[0] == claim_gate.STORE_UNREACHABLE
