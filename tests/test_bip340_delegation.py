"""The BIP-340 delegation to libsecp256k1: does the thin wrapper plumb correctly?

WHAT THIS FILE USED TO BE, AND WHY THE CLAIM IS NOW WEAKER. This was
`test_bip340_external_oracle.py`. The repo carried two hand-written BIP-340
implementations, and this file checked both against coincurve (libsecp256k1) as
an INDEPENDENT reference -- a genuinely strong claim, because two implementations
that agree byte-for-byte are unlikely to share a defect.

That independence is gone, on purpose. The hand-rolled arithmetic was deleted
and both modules now call libsecp256k1 directly, so comparing
`arb_registration.nip_oa.schnorr_sign` against `PrivateKey.sign_schnorr` compares
a wrapper to an inline copy of its own body. Keeping the old docstring would have
made this file the exact overclaim it was originally written to prevent.

WHAT IT HONESTLY TESTS NOW. Two things, both real:

  1. THE WRAPPERS PLUMB CORRECTLY. Argument order, hex handling, and the
     ValueError-to-False conversion are hand-written and therefore breakable. A
     `verify(msg, sig)` transposition is caught here (and by the sweep in
     tests/mutation_check_bip340_delegation.py, which demonstrates it).
  2. THE GUARDS THAT COINCURVE DOES NOT PROVIDE. This is the new risk surface
     and the reason this file did not simply get deleted. coincurve fails OPEN
     on short buffers: `PublicKeyXOnly(b"\\x01" * 31)` does not raise, it pads
     and parses a DIFFERENT key. Every explicit length check in the two modules
     is load-bearing for that reason, and nothing else in the suite pins them.

WHAT NO LONGER NEEDS TESTING HERE. The curve arithmetic itself. libsecp256k1 is
the reference implementation of this specification and carries its own test
suite; re-deriving confidence in it from 16 sample signatures would be theatre.
The specification vectors are retained anyway -- they are nearly free and they
calibrate the corpus against the library (see the direct-call test below), which
is what tells you whether a failure is in the wrapper or the world.

WHY A HARD IMPORT AND NOT `importorskip`. coincurve is now a RUNTIME dependency
(pyproject.toml, core `dependencies`). If it is absent the package is broken, not
merely untestable, so this file says so loudly rather than skipping green.
"""

from __future__ import annotations

import hashlib

import pytest
import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from coincurve import PrivateKey, PublicKeyXOnly

from arb_registration import nip_oa as arb_nip_oa
from buzz_ops import nip_oa as buzz_nip_oa

# The corpus is imported, never re-transcribed. That file's own comment records
# an earlier draft typed from memory in which three vectors were wrong.
from tests.buzz_ops.test_nip_oa import VECTORS

CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# Every verifier that claims to implement BIP-340, checked uniformly.
VERIFIERS = [
    pytest.param(arb_nip_oa.schnorr_verify, id="arb_registration"),
    pytest.param(buzz_nip_oa.schnorr_verify, id="buzz_ops"),
]

# Signing case count. Now that the arithmetic is libsecp256k1's, these cases
# exist to vary the KEY across the wrapper -- y-parity handling is key-dependent
# and lives inside the library, but the wrapper's argument order does not depend
# on the key at all, so a large sweep would buy nothing. Kept at 16 because the
# cases are now microseconds each rather than ~90ms of pure-Python EC arithmetic.
SIGNING_CASES = 16


def _case(index: int) -> tuple[bytes, bytes, bytes]:
    """Deterministic (secret, message, aux_rand) for a case index.

    Derived rather than literal so the spread of keys is wide, and reproducible
    so a failure names a case you can re-run.
    """
    return (
        hashlib.sha256(f"oracle-secret-{index}".encode()).digest(),
        hashlib.sha256(f"oracle-message-{index}".encode()).digest(),
        hashlib.sha256(f"oracle-aux-{index}".encode()).digest(),
    )


def _valid_cases() -> list[tuple[int, bytes, bytes, bytes]]:
    out = []
    for index in range(SIGNING_CASES):
        secret, message, aux = _case(index)
        if 0 < int.from_bytes(secret, "big") < CURVE_ORDER:
            out.append((index, secret, message, aux))
    return out


VALID_CASES = _valid_cases()


def test_every_signing_case_is_usable():
    """Guards the guard: a derivation that silently yielded no cases would make
    every parametrized signing test below vanish, and vanishing tests report as
    a pass. Asserts the count rather than merely non-emptiness."""
    assert len(VALID_CASES) == SIGNING_CASES


# --------------------------------------------------------------------------
# The specification vectors, through the wrappers and past them
# --------------------------------------------------------------------------


@pytest.mark.parametrize("verify", VERIFIERS)
@pytest.mark.parametrize("index,pubkey,message,signature,expected,comment", VECTORS)
def test_wrapped_verifiers_match_the_specification(
    verify, index, pubkey, message, signature, expected, comment
):
    """The wrapper's end-to-end verdict on the specification's own corpus.

    This is where an argument transposition inside the wrapper surfaces: with
    `verify(msg, sig)` instead of `verify(sig, msg)` the POSITIVE vectors stop
    verifying. Demonstrated, not assumed -- see the mutation sweep.
    """
    got = verify(bytes.fromhex(message), bytes.fromhex(pubkey), bytes.fromhex(signature))
    assert got is expected, f"vector {index} ({comment or 'no comment'})"


@pytest.mark.parametrize("index,pubkey,message,signature,expected,comment", VECTORS)
def test_the_library_itself_matches_the_specification(
    index, pubkey, message, signature, expected, comment
):
    """Calls coincurve directly, bypassing both wrappers.

    Retained after the delegation because it is what SPLITS a failure. If the
    wrapper test above goes red and this one stays green, the wrapper is at
    fault; if both go red, the library or the corpus is. Without it, a red suite
    would not say which.
    """
    try:
        got = PublicKeyXOnly(bytes.fromhex(pubkey)).verify(
            bytes.fromhex(signature), bytes.fromhex(message)
        )
    except ValueError:
        # An off-curve x-coordinate raises rather than returning False. For a
        # vector expected to FAIL that is still the correct verdict.
        got = False
    assert got is expected, f"vector {index} ({comment or 'no comment'})"


# --------------------------------------------------------------------------
# Signing: the wrapper's argument plumbing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("index,secret,message,aux", VALID_CASES)
def test_signer_wrapper_passes_its_arguments_through_unchanged(
    index, secret, message, aux
):
    """Pins `schnorr_sign(message, secret, aux_rand=aux)` onto
    `PrivateKey(secret).sign_schnorr(message, aux)`.

    HONEST SCOPE. Both sides are libsecp256k1, so this is NOT the independent
    byte-for-byte agreement the file once asserted -- it cannot detect a defect
    in the curve arithmetic, because there is only one copy of it. What it does
    detect is the wrapper transposing (message, aux_rand), which produces
    perfectly valid signatures over the wrong thing and is otherwise silent.
    """
    mine = arb_nip_oa.schnorr_sign(message, secret, aux_rand=aux)
    theirs = PrivateKey(secret).sign_schnorr(message, aux)
    assert mine == theirs, f"case {index}: {mine.hex()} != {theirs.hex()}"


@pytest.mark.parametrize("index,secret,message,aux", VALID_CASES)
def test_xonly_pubkey_wrapper_returns_the_x_coordinate_as_hex(
    index, secret, message, aux
):
    """Key derivation is upstream of both signing and the owner-identity check
    in load_owner_secret, so a defect here misattributes signatures rather than
    invalidating them -- a quieter failure than a bad signature. The wrapper
    chooses the encoding (`.format().hex()`), which is its own to get wrong."""
    assert arb_nip_oa.xonly_pubkey(secret) == (
        PublicKeyXOnly.from_secret(secret).format().hex()
    ), f"case {index}"


# --------------------------------------------------------------------------
# The guards coincurve does NOT provide -- the new risk surface
# --------------------------------------------------------------------------
#
# Measured on coincurve 21.0.0, and the reason the length checks in both modules
# cannot be "simplified" into the library call:
#
#   PublicKeyXOnly(b"\x01" * 31)  -> parses as 0101..0100, NO exception
#   PrivateKey(b"\x01" * 31)      -> parses, NO exception
#
# Both fail OPEN. A truncated buffer becomes a different valid key rather than an
# error, so without the explicit checks a caller passing a short pubkey would get
# a confident verdict about a key it never supplied.


@pytest.mark.parametrize("verify", VERIFIERS)
def test_a_truncated_pubkey_is_rejected_not_silently_repadded(verify):
    """The padding hazard, in both directions. Measured on coincurve 21.0.0:

        pubkey[:31]      -> parses as <key with last byte 00>   verify=False
        pubkey + b"\\x00" -> parses as <THE REAL KEY>            verify=True

    DO NOT COLLAPSE THESE TWO ASSERTIONS. Only the over-long one can fail. The
    short case is refused either way -- a padded buffer is a different valid key
    and the signature fails against it -- so it stays False with the guard
    deleted (docs/defect-classes/refusal-is-ambient-assert-the-code.md). The
    over-long case is the verdict flip: coincurve truncates back to the real key
    and returns True, meaning `schnorr_verify` would accept a pubkey argument
    that is not the pubkey. The mutation sweep pins the guard through that half
    alone; the short assertion is documentation of the hazard, not a check of it.
    """
    _, secret, message, aux = VALID_CASES[0]
    signature = arb_nip_oa.schnorr_sign(message, secret, aux_rand=aux)
    pubkey = bytes.fromhex(arb_nip_oa.xonly_pubkey(secret))

    assert verify(message, pubkey, signature) is True, "sanity: the full key verifies"
    assert verify(message, pubkey[:31], signature) is False
    assert verify(message, pubkey + b"\x00", signature) is False


@pytest.mark.parametrize("verify", VERIFIERS)
def test_malformed_lengths_return_false_rather_than_raising(verify):
    """Callers classify on the boolean; a length error is as much a verification
    failure as a wrong signature.

    NOTE ON WHAT THIS DOES *NOT* EXERCISE. Every case below is rejected by the
    wrapper's own length guard, which returns before coincurve is called -- so
    this test says nothing about the try/except around it. An earlier draft of
    this docstring claimed it did; the mutation sweep disproved that by deleting
    the try/except and watching this test stay green. The ValueError conversion
    is pinned by test_an_off_curve_pubkey_is_rejected_not_raised instead, which
    reaches a genuinely raising path.
    """
    _, secret, message, aux = VALID_CASES[0]
    signature = arb_nip_oa.schnorr_sign(message, secret, aux_rand=aux)
    pubkey = bytes.fromhex(arb_nip_oa.xonly_pubkey(secret))

    assert verify(b"", b"", b"") is False
    assert verify(message[:31], pubkey, signature) is False
    assert verify(message, pubkey, signature[:63]) is False
    assert verify(message, pubkey, signature + b"\x00") is False


@pytest.mark.parametrize("verify", VERIFIERS)
def test_an_off_curve_pubkey_is_rejected_not_raised(verify):
    """x = 5 has no y on secp256k1 (5^3 + 7 is not a quadratic residue mod p).
    coincurve raises ValueError on construction; the contract here is False.

    Chosen deliberately over x = 1 or x = 2, which ARE on the curve and would
    have made this test pass for the wrong reason.
    """
    _, secret, message, aux = VALID_CASES[0]
    signature = arb_nip_oa.schnorr_sign(message, secret, aux_rand=aux)
    assert verify(message, (5).to_bytes(32, "big"), signature) is False


def test_signing_rejects_short_buffers_rather_than_signing_with_a_padded_key():
    """`PrivateKey(b"\\x01" * 31)` does not raise -- it yields a usable key. So a
    short secret would otherwise produce a real signature under a key the caller
    never chose, and `xonly_pubkey` would report that key as theirs."""
    _, secret, message, aux = VALID_CASES[0]

    with pytest.raises(arb_nip_oa.OwnerKeyError):
        arb_nip_oa.schnorr_sign(message, secret[:31], aux_rand=aux)
    with pytest.raises(arb_nip_oa.OwnerKeyError):
        arb_nip_oa.schnorr_sign(message[:31], secret, aux_rand=aux)
    with pytest.raises(arb_nip_oa.OwnerKeyError):
        arb_nip_oa.schnorr_sign(message, secret, aux_rand=aux[:31])
    with pytest.raises(arb_nip_oa.OwnerKeyError):
        arb_nip_oa.xonly_pubkey(secret[:31])


def test_out_of_range_secrets_raise_owner_key_error_not_value_error():
    """coincurve raises a bare ValueError for a zero or >= N scalar. OwnerKeyError
    is a ValueError subclass, so a caller catching the narrow type must still see
    it -- the conversion in the wrapper is what makes that true."""
    message, aux = _case(0)[1], _case(0)[2]

    for bad in (b"\x00" * 32, CURVE_ORDER.to_bytes(32, "big")):
        with pytest.raises(arb_nip_oa.OwnerKeyError):
            arb_nip_oa.schnorr_sign(message, bad, aux_rand=aux)
        with pytest.raises(arb_nip_oa.OwnerKeyError):
            arb_nip_oa.xonly_pubkey(bad)


def test_build_auth_tag_still_rejects_an_off_curve_agent_pubkey():
    """`build_auth_tag`'s validity check moved from `_lift_x(x) is None` to
    catching PublicKeyXOnly's ValueError. Those partition x identically (checked
    over x in 1..399, 399/399 agreement), but the guard is security-relevant and
    the substitution is not self-evident from reading either line."""
    _, secret, _, _ = VALID_CASES[0]

    with pytest.raises(arb_nip_oa.OwnerKeyError, match="not a valid x-only"):
        arb_nip_oa.build_auth_tag(secret, (5).to_bytes(32, "big").hex())
    with pytest.raises(arb_nip_oa.OwnerKeyError, match="not a valid x-only"):
        arb_nip_oa.build_auth_tag(secret, "ff" * 32)  # x >= field prime
