from __future__ import annotations

from implbench.harness.quarantine import census_evidence_digest


def test_census_digest_is_canonical_and_private() -> None:
    payload = {
        "phase": "export", "gate_id": "G12",
        "expected_ref_digest": "1" * 64, "observed_ref_digest": "2" * 64,
        "expected_object_digest": "3" * 64, "observed_object_digest": "4" * 64,
        "expected_ref_count": 2, "observed_ref_count": 2,
        "expected_object_count": 4, "observed_object_count": 4,
        "violation": "EXTRA_REF",
    }
    assert census_evidence_digest(payload) == census_evidence_digest(dict(reversed(list(payload.items()))))
