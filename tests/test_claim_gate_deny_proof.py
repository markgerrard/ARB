"""Inject-revert proofs for the claim gate's refusal matrix.

A green gate test is just another green test
(`docs/defect-classes/deny-proofs-need-adversarial-verification.md`). These assert the gate
leaves NO open door across every non-admissible state, and that admission has exactly the
three doors the spec names.

WHY THE MATRIX ASSERTS A CODE, NOT JUST "some refusal"
------------------------------------------------------
The first version of this file asserted only `outcome is not None`. Inject-revert showed that
to be VACUOUS for the lane mechanism: disabling `if declared_lane == "exempt":` left the suite
green, because control fell through to claim resolution and refused with `missing_claim_ref`
instead. The gate still said no — for an unrelated reason. That is precisely the
"a falsifier can itself be vacuous" limit the spec records at §9.4, caught in this repo's own
deny-proof. Asserting the CODE binds each case to the mechanism that is supposed to produce it.

INJECT-REVERT RESULTS, run 2026-07-26 against the code-asserting version — all reverted:
  `if not found.attested:`          -> `if False:`  =>  WRONG DOOR: expected unattested_claim,
                                                        got admitted
  `if declared_lane == "exempt":`   -> `if False:`  =>  WRONG DOOR: expected
                                                        lane_not_armed_exempt, got
                                                        missing_claim_ref
  `if lane == "exempt":`            -> `if False:`  =>  test_admission_has_exactly_the_three
                                                        _doors failed (exempt lease refused)
"""

from agent_redis_bridge import claim_gate
from agent_redis_bridge.envelope import Envelope


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
    def __init__(self, *, posture=True, lanes=None, claims=None):
        self.posture = posture
        self.lanes = lanes or {}
        self.claims = claims or {}

    def seat_requires_claim_ref(self, seat_id):
        return self.posture

    def lease_lane(self, lease_id):
        return self.lanes.get(lease_id)

    def claim(self, claim_ref):
        return self.claims.get(claim_ref)


def facts(confirmed=True, attested=True):
    return claim_gate.ClaimFacts(
        confirmed_now=confirmed, attested=attested, decorrelation_provenance="wire"
    )


def test_every_non_admissible_state_refuses_with_its_own_mechanism():
    """Each case must produce the refusal its OWN mechanism generates.

    Asserting only `is not None` would let one mechanism's failure be masked by another's
    refusal — see the module docstring for the inject-revert that proved this.
    """
    non_admissible = [
        ({"task": "t"}, {}, claim_gate.MISSING_CLAIM_REF),
        ({"task": "t", "claim_ref": "c-x"}, {}, claim_gate.UNKNOWN_CLAIM_REF),
        (
            {"task": "t", "claim_ref": "c-1"},
            {"c-1": facts(confirmed=False)},
            claim_gate.UNCONFIRMED_CLAIM,
        ),
        (
            {"task": "t", "claim_ref": "c-1"},
            {"c-1": facts(attested=False)},
            claim_gate.UNATTESTED_CLAIM,
        ),
        ({"task": "t", "lane": "exempt"}, {}, claim_gate.LANE_NOT_ARMED_EXEMPT),
        (
            {"task": "t", "worktree_lease": "l-unknown", "lane": "exempt"},
            {},
            claim_gate.LANE_NOT_ARMED_EXEMPT,
        ),
    ]
    for payload, claims, expected in non_admissible:
        outcome = claim_gate.evaluate(
            make_envelope(payload), seat_id="codex-x", resolver=FakeResolver(claims=claims)
        ).outcome
        actual = outcome.code if outcome is not None else "admitted"
        assert actual == expected, f"WRONG DOOR: {payload} expected {expected}, got {actual}"


def test_admission_has_exactly_the_three_doors_the_spec_names():
    """No posture, store-armed exempt lease, or an admissible claim. Nothing else."""
    assert (
        claim_gate.evaluate(
            make_envelope({"task": "t"}), seat_id="s", resolver=FakeResolver(posture=False)
        ).outcome
        is None
    )
    assert (
        claim_gate.evaluate(
            make_envelope({"task": "t", "worktree_lease": "l-1"}),
            seat_id="s",
            resolver=FakeResolver(lanes={"l-1": "exempt"}),
        ).outcome
        is None
    )
    assert (
        claim_gate.evaluate(
            make_envelope({"task": "t", "claim_ref": "c-1"}),
            seat_id="s",
            resolver=FakeResolver(claims={"c-1": facts()}),
        ).outcome
        is None
    )
