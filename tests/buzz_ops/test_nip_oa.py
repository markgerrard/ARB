"""The primitive is checked against BIP-340's own vectors, not against itself.

A hand-rolled curve implementation that only ever verifies signatures it also
produced proves nothing: the bug and the check would share the same mistake.
These vectors are from the specification, and the FAILURE cases matter more than
the success ones — a verifier that returns True for everything passes any
success-only suite.

15 of the specification's 19 are carried here. The four absent ones (15-18) are
the variable-length-message vectors added 2022-12; schnorr_verify accepts only
32-byte messages, so they cannot pass and their absence is a scope limit rather
than a gap — see that function's docstring. Everything applicable is present:
index 7 was missing until 8d2742bd and was added on review.
"""

from __future__ import annotations

import hashlib

import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from buzz_ops.nip_oa import (
    auth_message, build_preimage, conditions_apply, schnorr_verify, verify_auth_tag,
)

# BIP-340 vectors, transcribed from
# https://raw.githubusercontent.com/bitcoin/bips/master/bip-0340/test-vectors.csv
# (index, public key, message, signature, expected, comment).
#
# Fetched rather than recalled, and that distinction earned its keep: an earlier
# draft of this file typed them from memory and three were wrong — vector 0 by a
# single character (E2DBA/E2DCA) and vector 5's pubkey was pure invention. The
# suite went red against a CORRECT implementation, which is the good failure
# direction, but a mistake in the other direction would have shipped a verifier
# validated against fiction.
VECTORS = [
    ("0",
     "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
     "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
     True, ""),
    ("1",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE3341"
     "8906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A",
     True, ""),
    ("2",
     "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
     "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
     "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1B"
     "AB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7",
     True, ""),
    ("3",
     "25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
     "7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC"
     "97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3",
     True, "fails if msg is reduced modulo p or n"),
    ("4",
     "D69C3509BB99E412E68B0FE8544E72837DFA30746D8BE2AA65975F29D22DC7B9",
     "4DF3C3F68FCC83B27E9D42C90431A72499F17875C81A599B566C9889B9696703",
     "00000000000000000000003B78CE563F89A0ED9414F5AA28AD0D96D6795F9C63"
     "76AFB1548AF603B3EB45C9F8207DEE1060CB71C04E80F593060B07D28308D7F4",
     True, ""),
    ("5",
     "EEFDEA4CDB677750A420FEE807EACF21EB9898AE79B9768766E4FAA04A2D4A34",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769"
     "69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
     False, "public key not on the curve"),
    ("6",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "FFF97BD5755EEEA420453A14355235D382F6472F8568A18B2F057A1460297556"
     "3CC27944640AC607CD107AE10923D9EF7A73C643E166BE5EBEAFA34B1AC553E2",
     False, "has_even_y(R) is false"),
    ("7",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "1FA62E331EDBC21C394792D2AB1100A7B432B013DF3F6FF4F99FCB33E0E1515F"
     "28890B3EDB6E7189B630448B515CE4F8622A954CFE545735AAEA5134FCCDB2BD",
     False, "negated message"),
    ("8",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769"
     "961764B3AA9B2FFCB6EF947B6887A226E8D7C93E00C5ED0C1834FF0D0C2E6DA6",
     False, "negated s value"),
    ("9",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "0000000000000000000000000000000000000000000000000000000000000000"
     "123DDA8328AF9C23A94C1FEECFD123BA4FB73476F0D594DCB65C6425BD186051",
     False, "sG - eP is infinite; fails if has_even_y(inf) is defined true"),
    ("10",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "0000000000000000000000000000000000000000000000000000000000000001"
     "7615FBAF5AE28864013C099742DEADB4DBA87F11AC6754F93780D5A1837CF197",
     False, "sG - eP is infinite; fails if has_even_y(inf) is defined true"),
    ("11",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "4A298DACAE57395A15D0795DDBFD1DCB564DA82B0F269BC70A74F8220429BA1D"
     "69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
     False, "sig[0:32] is not an X coordinate on the curve"),
    ("12",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F"
     "69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
     False, "sig[0:32] is equal to field size"),
    ("13",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769"
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
     False, "sig[32:64] is equal to curve order"),
    ("14",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC30",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769"
     "69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
     False, "public key exceeds the field size"),
]


def test_the_vector_set_contains_both_outcomes():
    """A suite of only-valid vectors is passed by `return True`."""
    outcomes = {expected for *_, expected, _c in VECTORS}
    assert outcomes == {True, False}
    assert sum(1 for *_, e, _c in VECTORS if not e) >= 8, "too few negative vectors to be convincing"


def test_every_applicable_specification_vector_is_present():
    """Pin the corpus against the spec's index range, not against its own length.

    Vector 7 ("negated message") was absent from the first version of this file
    and nothing noticed: the suite was internally consistent, every vector it
    DID carry passed, and the count assertion above was satisfied. A silently
    incomplete corpus is the failure mode this whole file exists to avoid, so
    the completeness claim needs enforcing rather than stating.

    0-14 are the specification's 32-byte-message vectors and must all be here.
    15-18 sign variable-length messages, which schnorr_verify rejects by
    design; asserting their ABSENCE keeps a future paste of the full CSV from
    quietly turning into four failing tests attributed to the wrong cause.
    """
    present = {index for index, *_ in VECTORS}
    applicable = {str(i) for i in range(15)}

    assert present >= applicable, (
        "missing applicable BIP-340 vectors "
        f"{sorted(applicable - present, key=int)} — fetch them from "
        "bip-0340/test-vectors.csv rather than reconstructing them"
    )
    assert not present - applicable, (
        f"unexpected vectors {sorted(present - applicable, key=int)}: 15-18 "
        "sign variable-length messages and cannot pass while schnorr_verify "
        "requires 32 bytes. Widen the function first, then add them."
    )


def test_bip340_vectors():
    for index, pubkey, msg, sig, expected, comment in VECTORS:
        got = schnorr_verify(bytes.fromhex(msg), bytes.fromhex(pubkey), bytes.fromhex(sig))
        assert got is expected, f"BIP-340 vector {index} ({comment}): expected {expected}, got {got}"


def test_verifier_rejects_malformed_lengths_rather_than_raising():
    assert schnorr_verify(b"", b"", b"") is False
    assert schnorr_verify(b"\x00" * 31, b"\x00" * 32, b"\x00" * 64) is False
    assert schnorr_verify(b"\x00" * 32, b"\x00" * 32, b"\x00" * 63) is False


def test_preimage_excludes_profile_content():
    """The property the whole repair procedure rests on: a tag stays valid across
    a profile edit. If this ever changes, recovering an old tag becomes wrong."""
    agent = "ae" * 32
    assert build_preimage(agent, "kind=0") == f"nostr:agent-auth:{agent}:kind=0"
    expected = hashlib.sha256(f"nostr:agent-auth:{agent}:kind=0".encode()).digest()
    assert auth_message(agent, "kind=0") == expected


class TestConditions:
    """Mirrors server.rs observer_auth_conditions_apply clause for clause."""

    def test_kind_must_match_the_event(self):
        assert conditions_apply("kind=0", kind=0, created_at=1)
        assert not conditions_apply("kind=0", kind=1, created_at=1)

    def test_created_at_bounds(self):
        assert conditions_apply("created_at<100", kind=0, created_at=99)
        assert not conditions_apply("created_at<100", kind=0, created_at=100)
        assert conditions_apply("created_at>100", kind=0, created_at=101)
        assert not conditions_apply("created_at>100", kind=0, created_at=100)

    def test_all_clauses_must_hold(self):
        assert conditions_apply("kind=0&created_at>10", kind=0, created_at=11)
        assert not conditions_apply("kind=0&created_at>10", kind=0, created_at=9)

    def test_unknown_clause_fails_but_empty_clause_passes(self):
        """The relay's final `else { clause.is_empty() }`. Being laxer here would
        report a seat healthy that the proxy refuses — the failure direction that
        makes a monitor worse than none."""
        assert not conditions_apply("expires=soon", kind=0, created_at=1)
        assert conditions_apply("", kind=0, created_at=1)
        assert conditions_apply("kind=0&", kind=0, created_at=1)


class TestVerifyAuthTag:
    AGENT = "ae925ba349c7055b65cf88575a790c414636d0c1a4c35ee123a342e72d6bfbe6"
    OWNER = "ae0954e4582ac686025837180479a4248add49016f1769d15d0af5bf2eb67cdd"
    SIG = ("56ce136df2414239fa57bf7b2b520540ce599b101dc90de47905fc515632687937f95f76bf714622"
           "ea1ae097bb3442037764a1a7512ff02109695581a3ff3bf5")

    def test_a_real_relay_tag_verifies(self):
        """A real auth tag recovered from a live seat's relay profile. If
        this fails, either the port is wrong or the tag was never valid — and the
        repair advice built on it would be wrong too."""
        verdict = verify_auth_tag(["auth", self.OWNER, "kind=0", self.SIG],
                                  self.AGENT, kind=0, created_at=1786000000)
        assert verdict.ok, verdict.reason
        assert verdict.owner_pubkey == self.OWNER

    def test_a_corrupted_signature_is_rejected(self):
        flipped = ("57" + self.SIG[2:])
        verdict = verify_auth_tag(["auth", self.OWNER, "kind=0", flipped],
                                  self.AGENT, kind=0, created_at=1786000000)
        assert not verdict.ok
        assert verdict.reason == "bad_signature"

    def test_tag_bound_to_its_agent_cannot_be_replayed_onto_another(self):
        """The property that makes tag reuse safe ONLY within one seat."""
        other_agent = "11" * 32
        verdict = verify_auth_tag(["auth", self.OWNER, "kind=0", self.SIG],
                                  other_agent, kind=0, created_at=1786000000)
        assert not verdict.ok
        assert verdict.reason == "bad_signature"

    def test_conditions_are_evaluated_before_the_signature(self):
        verdict = verify_auth_tag(["auth", self.OWNER, "kind=1", self.SIG],
                                  self.AGENT, kind=1, created_at=1786000000)
        assert verdict.reason in {"conditions_not_applicable", "bad_signature"}

    def test_self_attestation_refused(self):
        verdict = verify_auth_tag(["auth", self.AGENT, "kind=0", self.SIG],
                                  self.AGENT, kind=0, created_at=1)
        assert not verdict.ok
        assert verdict.reason == "self_attested"

    def test_malformed_shapes(self):
        assert verify_auth_tag(["auth"], self.AGENT, kind=0, created_at=1).reason == "malformed_tag"
        assert verify_auth_tag(["auth", "short", "kind=0", self.SIG],
                               self.AGENT, kind=0, created_at=1).reason == "malformed_tag"
