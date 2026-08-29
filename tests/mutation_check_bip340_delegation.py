"""Proves tests/test_bip340_delegation.py can actually fail.

    <venv>/bin/python tests/mutation_check_bip340_delegation.py

Successor to mutation_check_bip340_oracle.py, which mutated the hand-written
curve arithmetic. That arithmetic is gone -- both modules now call libsecp256k1 --
so the mutable surface changed completely. Mutating a C library's maths is not
possible from here and would not be useful if it were; what IS hand-written, and
therefore breakable, is the wrapper: argument order, the length guards coincurve
does not enforce, and the ValueError-to-False conversion.

Every mutation below is a defect that a careless refactor of the delegation could
plausibly introduce, and each names the test that must catch it.
"""
from _mutation_lib import sweep

TARGET = "arb_registration/nip_oa.py"
TEST_FILE = "tests/test_bip340_delegation.py"

MUTATIONS = [
    # The signature-verification argument order. coincurve takes (sig, msg);
    # the wrapper's own signature is (message, pubkey, signature). Transposing
    # is the single most likely slip in this file and is completely silent --
    # it produces a verifier that simply always says no.
    (
        "verify() argument order transposed (sig, msg) -> (msg, sig)",
        "return PublicKeyXOnly(pubkey).verify(signature, message)",
        "return PublicKeyXOnly(pubkey).verify(message, signature)",
        "test_wrapped_verifiers_match_the_specification",
    ),
    # The signing argument order. Both are 32 bytes, so nothing about the types
    # objects; the result is a valid BIP-340 signature over the wrong message
    # with the wrong nonce. Only a byte comparison notices.
    (
        "sign_schnorr() argument order transposed (msg, aux) -> (aux, msg)",
        "return PrivateKey(secret).sign_schnorr(message, aux_rand)",
        "return PrivateKey(secret).sign_schnorr(aux_rand, message)",
        "test_signer_wrapper_passes_its_arguments_through_unchanged",
    ),
    # The secret-length guard. PrivateKey() accepts a short buffer and signs
    # with a padded key, so dropping this check turns a caller error into a
    # confident signature under a key nobody chose.
    (
        "signing drops its input length guard",
        "if len(message) != 32 or len(secret) != 32 or len(aux_rand) != 32:",
        "if False:",
        "test_signing_rejects_short_buffers_rather_than_signing_with_a_padded_key",
    ),
    # The on-curve check behind build_auth_tag's "not a valid x-only key".
    # Weakening it lets a malformed agent pubkey through to be signed over.
    (
        "_is_valid_xonly accepts everything",
        "        PublicKeyXOnly(raw)\n    except ValueError:\n        return False\n    return True",
        "        pass\n    except ValueError:\n        return False\n    return True",
        "test_build_auth_tag_still_rejects_an_off_curve_agent_pubkey",
    ),
    # The ValueError-to-False conversion. An OFF-CURVE x is what actually
    # reaches it: coincurve raises from PublicKeyXOnly's constructor, and
    # callers of schnorr_verify branch on a bool, so the exception would crash
    # them at a trust boundary instead of rejecting.
    #
    # The named test here is deliberately NOT the malformed-lengths one. Every
    # case in that test is rejected by the wrapper's own length guard before
    # coincurve is called, so it stays green with the try/except deleted -- this
    # sweep is what established that, against a docstring that claimed otherwise.
    (
        "schnorr_verify lets coincurve's ValueError escape",
        "    try:\n        return PublicKeyXOnly(pubkey).verify(signature, message)\n    except ValueError:\n        return False",
        "    return PublicKeyXOnly(pubkey).verify(signature, message)",
        "test_an_off_curve_pubkey_is_rejected_not_raised",
    ),
    # The pubkey length guard. See the note below on which half of the paired
    # assertion in the named test actually does the killing -- it is not the
    # obvious one.
    (
        "schnorr_verify drops its pubkey length guard",
        "    if len(message) != 32 or len(pubkey) != 32 or len(signature) != 64:\n        return False",
        "    if len(message) != 32 or len(signature) != 64:\n        return False",
        "test_a_truncated_pubkey_is_rejected_not_silently_repadded",
    ),
]


# WHY THE PUBKEY-GUARD MUTATION IS CAUGHT, AND WHAT WOULD HAVE MISSED IT
#
# Worth recording because the first draft of this file asserted the opposite --
# that the guard could not be pinned -- and the sweep disproved it. Measured on
# coincurve 21.0.0 against a real key:
#
#   pubkey[:31]      -> parses as <key with last byte replaced by 00>  verify=False
#   pubkey + b"\x00" -> parses as <THE REAL KEY>                       verify=True
#
# The SHORT case is invisible, exactly as
# docs/defect-classes/refusal-is-ambient-assert-the-code.md predicts: a padded
# buffer is a different valid key, the signature fails against it, and the
# verdict stays False whether the guard is present or not. A test asserting only
# `verify(msg, pubkey[:31], sig) is False` would stay green with the guard gone.
#
# The OVER-LONG case flips a verdict, which is why it is catchable: coincurve
# truncates the 33-byte buffer back to the real key and returns True. Without the
# guard, `schnorr_verify` would accept a pubkey argument that is not the pubkey.
#
# So the two assertions in the named test are not redundant and must not be
# "tidied" into one. The short one documents the padding hazard; the long one is
# the only half that can fail.


if __name__ == "__main__":
    raise SystemExit(sweep(TARGET, TEST_FILE, MUTATIONS))
