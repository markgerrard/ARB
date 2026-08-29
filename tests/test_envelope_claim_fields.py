"""Envelope-layer validation for the gate's two optional payload fields.

Type checks ONLY. The envelope layer never decides admissibility — that is claim_gate's job,
and `lane` in particular is routing metadata whose meaning lives in the store (spec §5.3).
"""

import json

import pytest

from agent_redis_bridge.envelope import Envelope, EnvelopeError


def raw(payload):
    return json.dumps(
        {
            "id": "e-1",
            "from": "claude-x",
            "branch": "dev",
            "to": "codex-x",
            "kind": "request",
            "sent_at": "2026-07-26T00:00:00+00:00",
            "payload": payload,
        }
    )


def test_absent_claim_ref_is_still_a_valid_envelope():
    """Absence is a GATE concern, not an envelope concern."""
    assert Envelope.from_json(raw({"task": "t"})).payload.get("claim_ref") is None


def test_valid_claim_ref_is_carried_through():
    assert Envelope.from_json(raw({"task": "t", "claim_ref": "c-1"})).payload["claim_ref"] == "c-1"


@pytest.mark.parametrize("bad", [42, "", "   ", None, [], {}, True])
def test_non_string_or_blank_claim_ref_is_rejected(bad):
    with pytest.raises(EnvelopeError, match="invalid-payload-claim_ref"):
        Envelope.from_json(raw({"task": "t", "claim_ref": bad}))


@pytest.mark.parametrize("bad", [42, "", "   ", None, [], {}])
def test_non_string_or_blank_lane_is_rejected(bad):
    with pytest.raises(EnvelopeError, match="invalid-payload-lane"):
        Envelope.from_json(raw({"task": "t", "lane": bad}))


def test_envelope_does_not_validate_lane_values():
    """The store decides what a lane means; the envelope only checks the type (spec §5.3)."""
    assert Envelope.from_json(raw({"task": "t", "lane": "not-a-real-lane"})).payload["lane"] == (
        "not-a-real-lane"
    )
