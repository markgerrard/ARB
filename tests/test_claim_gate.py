"""Tests for the bus-side gate's decision core.

Spec: docs/superpowers/specs/2026-07-26-bus-side-gate-design.md (art-8742dfc1ca4b8be8 v6,
hash 61b7332a01a1c97f), §5.1 refusal codes and §5.3 lane resolution.

No database. Every store fact arrives through a fake resolver, so each branch is reachable on
a bare checkout — the repo's Postgres-backed tests skip without ARB_MEMORY_DSN, and a skipped
test is a green test.
"""

import pytest

from agent_redis_bridge import claim_gate
from agent_redis_bridge.envelope import Envelope


def test_outcome_serialises_in_close_house_style():
    outcome = claim_gate.GateOutcome(
        code=claim_gate.MISSING_CLAIM_REF,
        gaps=["no claim_ref on payload", "legitimate lanes: exempt (build the probe first)"],
    )
    assert outcome.as_dict() == {
        "outcome": "missing_claim_ref",
        "exit_code": 5,
        "gaps": [
            "no claim_ref on payload",
            "legitimate lanes: exempt (build the probe first)",
        ],
    }


def test_error_string_names_the_code_and_the_gaps():
    outcome = claim_gate.GateOutcome(
        code=claim_gate.UNCONFIRMED_CLAIM, gaps=["claim c-1 is unconfirmed"]
    )
    error = outcome.as_error()
    assert "unconfirmed_claim" in error
    assert "claim c-1 is unconfirmed" in error


def test_error_string_survives_an_empty_gaps_list():
    assert claim_gate.GateOutcome(code=claim_gate.STORE_UNREACHABLE).as_error() == "store_unreachable"


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
    """Explicit fake: every answer must be configured, so no test passes by accident."""

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


def test_seat_not_posture_bearing_is_admitted():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "do a thing"}),
        seat_id="codex-x",
        resolver=FakeResolver(posture=False),
    ).outcome
    assert outcome is None


def test_posture_read_failure_refuses_and_never_admits():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(raises="posture"),
    ).outcome
    assert outcome is not None, "fail-closed: a store failure must never admit"
    assert outcome.code == claim_gate.STORE_UNREACHABLE


def test_store_unreachable_is_not_reported_as_a_confirmation_failure():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(raises="posture"),
    ).outcome
    assert outcome.code not in (claim_gate.UNCONFIRMED_CLAIM, claim_gate.UNKNOWN_CLAIM_REF)
    assert any("operator" in g or "unavailable" in g for g in outcome.gaps), (
        "a store outage must read as an operator page, not an accusation against the dispatcher"
    )


def test_store_confirmed_exempt_lease_admits_without_a_claim_ref():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "build the probe", "worktree_lease": "l-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(lanes={"l-1": "exempt"}),
    ).outcome
    assert outcome is None


def test_lane_read_failure_refuses():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "worktree_lease": "l-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(raises="lane"),
    ).outcome
    assert outcome is not None
    assert outcome.code == claim_gate.STORE_UNREACHABLE


def test_envelope_declaring_exempt_against_a_gated_lease_is_refused_by_name():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "worktree_lease": "l-1", "lane": "exempt"}),
        seat_id="codex-x",
        resolver=FakeResolver(lanes={"l-1": "gated"}),
    ).outcome
    assert outcome.code == claim_gate.LANE_NOT_ARMED_EXEMPT
    assert any("consumer" in g for g in outcome.gaps), (
        "gaps must route: lanes are armed by the consumer, not asserted by the dispatcher"
    )


def test_envelope_declaring_exempt_cannot_admit_by_itself():
    """The self-attestation hole: a declared lane must never be sufficient to admit."""
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "lane": "exempt"}),
        seat_id="codex-x",
        resolver=FakeResolver(lanes={}),
    ).outcome
    assert outcome is not None
    assert outcome.code == claim_gate.LANE_NOT_ARMED_EXEMPT


def test_unknown_lease_does_not_admit_as_exempt():
    """Spec §5.3 rule 1: a lease the store does not record is gated traffic, never exempt."""
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "worktree_lease": "l-unknown", "lane": "exempt"}),
        seat_id="codex-x",
        resolver=FakeResolver(lanes={}),
    ).outcome
    assert outcome.code == claim_gate.LANE_NOT_ARMED_EXEMPT


def facts(confirmed=True, attested=True, provenance="wire"):
    return claim_gate.ClaimFacts(
        confirmed_now=confirmed, attested=attested, decorrelation_provenance=provenance
    )


def test_admissible_claim_is_admitted():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "fix it", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts()}),
    ).outcome
    assert outcome is None


def test_missing_claim_ref_routes_to_the_probe_lane():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "fix it"}), seat_id="codex-x", resolver=FakeResolver()
    ).outcome
    assert outcome.code == claim_gate.MISSING_CLAIM_REF
    assert any("exempt" in g for g in outcome.gaps), (
        "gaps must name the lane that WOULD be legitimate"
    )


def test_no_lease_reference_is_gated_traffic_not_exempt():
    """Spec §5.3 rule 1, observed end-to-end: silence lands on a claim-ref refusal."""
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t"}), seat_id="codex-x", resolver=FakeResolver()
    ).outcome
    assert outcome is not None
    assert outcome.code == claim_gate.MISSING_CLAIM_REF


def test_unresolvable_ref_is_distinct_from_unconfirmed():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-typo"}),
        seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts()}),
    ).outcome
    assert outcome.code == claim_gate.UNKNOWN_CLAIM_REF


def test_unconfirmed_claim_routes_to_the_probe_lane():
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts(confirmed=False)}),
    ).outcome
    assert outcome.code == claim_gate.UNCONFIRMED_CLAIM
    assert any("exempt" in g for g in outcome.gaps)


def test_unattested_claim_routes_to_verification_not_the_probe_lane():
    """Spec §5.1: the dispatcher's next action differs — that is why the codes are distinct."""
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts(attested=False)}),
    ).outcome
    assert outcome.code == claim_gate.UNATTESTED_CLAIM
    assert any("verification" in g.lower() for g in outcome.gaps)
    assert not any("build the probe" in g for g in outcome.gaps), (
        "an unattested claim already HAS its probe; routing it to the probe lane misinstructs"
    )


@pytest.mark.parametrize("failing_call", ["posture", "lane", "claim"])
def test_any_store_failure_refuses_and_never_admits(failing_call):
    outcome = claim_gate.evaluate(
        make_envelope({"task": "t", "worktree_lease": "l-1", "claim_ref": "c-1"}),
        seat_id="codex-x",
        resolver=FakeResolver(raises=failing_call),
    ).outcome
    assert outcome is not None, f"fail-closed: a {failing_call} failure must never admit"
    assert outcome.code == claim_gate.STORE_UNREACHABLE


def test_all_six_spec_codes_exist_and_are_distinct():
    codes = {
        claim_gate.MISSING_CLAIM_REF,
        claim_gate.UNCONFIRMED_CLAIM,
        claim_gate.UNATTESTED_CLAIM,
        claim_gate.UNKNOWN_CLAIM_REF,
        claim_gate.LANE_NOT_ARMED_EXEMPT,
        claim_gate.STORE_UNREACHABLE,
    }
    assert len(codes) == 6, "spec §5.1 requires six DISTINCT refusal codes"
