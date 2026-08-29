"""NIP-OA agent auth tags: verification, mirrored from the relay's own rules.

Why this exists as a verifier rather than a string check: an `auth` tag that is
PRESENT but invalid fails exactly as silently as an absent one, and the failure
mode we have actually been bitten by twice (a rename republishing kind:0 without
the tag) would be caught by a substring grep — but the next one might not be.

Ported from, and deliberately kept in step with, buzz's own implementation:
  the SDK's build_preimage / verify_auth_tag
  the web-channel proxy's profile_agent_owner
  the web-channel proxy's observer_auth_conditions_apply

If the relay's rules change, this file is wrong until it is changed too. The
primitive is not taken on trust either: it is tested against BIP-340's own
specification vectors, within the scope stated at schnorr_verify — 32-byte
messages only, which is all NIP-OA ever presents.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from coincurve import PublicKeyXOnly

# --- secp256k1 / BIP-340 ------------------------------------------------------
#
# Previously implemented here by hand, because the production image was
# `FROM python:3.14-slim` and coincurve (libsecp256k1) has no cp314 wheel. That
# pin turned out to be incidental rather than required, the image moved to
# 3.13-slim, and the arithmetic was deleted in favour of the reference C
# implementation. See src/arb_registration/nip_oa.py's module docstring for the
# boundary differences that keep the explicit length guards below necessary —
# coincurve pads a short buffer rather than rejecting it.


def schnorr_verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    """BIP-340 verification, restricted to 32-byte messages.

    False on any malformed input rather than raising — callers classify, and a
    length error is as much a verification failure as a wrong signature. The
    try/except converts coincurve's ValueError (raised for an off-curve x, and
    for lengths it polices itself) into that contract.

    SCOPE. BIP-340 was extended in 2022-12 to sign messages of arbitrary length,
    and the specification's vectors 15-18 exercise exactly that (messages of 0,
    1, 17 and 100 bytes, all expected to VERIFY). This function rejects them on
    the length check above, so those four are deliberately absent from the test
    corpus rather than silently failing in it.

    That is sound for the only caller: auth_message() hashes to a sha256 digest,
    so NIP-OA never presents anything but 32 bytes. It is stated here because
    "checked against the specification's vectors" would otherwise imply a
    generality this does not have — anyone reusing this outside NIP-OA, on a
    message that is not a digest, needs the variable-length path and does not
    have it. Delegating did not widen this: coincurve 21 is itself 32-byte-only
    and raises on anything else, so the scope limit is now enforced twice.
    """
    if len(msg) != 32 or len(pubkey) != 32 or len(sig) != 64:
        return False
    try:
        return PublicKeyXOnly(pubkey).verify(sig, msg)
    except ValueError:
        return False


# --- NIP-OA -------------------------------------------------------------------

def build_preimage(agent_pubkey_hex: str, conditions: str) -> str:
    """Mirrors buzz's own SDK build_preimage.

    Note what is ABSENT: the profile's content. The signature commits to the
    agent pubkey and the conditions only, which is why a tag minted for an
    earlier profile stays valid across a later edit — and why recovering one
    from event history is a legitimate repair rather than a forgery.
    """
    return f"nostr:agent-auth:{agent_pubkey_hex}:{conditions}"


def auth_message(agent_pubkey_hex: str, conditions: str) -> bytes:
    return hashlib.sha256(build_preimage(agent_pubkey_hex, conditions).encode()).digest()


def conditions_apply(conditions: str, *, kind: int, created_at: int) -> bool:
    """server.rs observer_auth_conditions_apply, clause for clause.

    The final `else` is not a typo: an UNRECOGNISED clause fails the whole
    condition string, while an empty clause passes. Mirroring that exactly
    matters — being more permissive here would report a seat as healthy that
    the proxy refuses.
    """
    for clause in conditions.split("&"):
        if clause.startswith("kind="):
            value = clause[len("kind="):]
            if not value.isdigit() or int(value) != kind:
                return False
        elif clause.startswith("created_at<"):
            value = clause[len("created_at<"):]
            if not value.isdigit() or not created_at < int(value):
                return False
        elif clause.startswith("created_at>"):
            value = clause[len("created_at>"):]
            if not value.isdigit() or not created_at > int(value):
                return False
        elif clause != "":
            return False
    return True


@dataclass(frozen=True)
class AuthTagVerdict:
    ok: bool
    reason: str
    owner_pubkey: str | None = None
    conditions: str | None = None


def verify_auth_tag(tag: list, agent_pubkey_hex: str, *, kind: int, created_at: int) -> AuthTagVerdict:
    """Verify ONE candidate auth tag. Cardinality is the caller's problem —
    see check_seat, because 'exactly one' is a property of the event, not of a tag."""
    if not isinstance(tag, list) or len(tag) < 4:
        return AuthTagVerdict(False, "malformed_tag")
    _, owner_hex, conditions, sig_hex = tag[0], tag[1], tag[2], tag[3]
    if not all(isinstance(v, str) for v in (owner_hex, conditions, sig_hex)):
        return AuthTagVerdict(False, "malformed_tag")
    if len(owner_hex) != 64 or len(sig_hex) != 128:
        return AuthTagVerdict(False, "malformed_tag", owner_hex, conditions)
    if owner_hex == agent_pubkey_hex:
        # nip_oa.rs refuses self-attestation at mint time; refuse it at audit
        # time too, or a seat that signed its own tag reads as healthy here.
        return AuthTagVerdict(False, "self_attested", owner_hex, conditions)
    if not conditions_apply(conditions, kind=kind, created_at=created_at):
        return AuthTagVerdict(False, "conditions_not_applicable", owner_hex, conditions)
    try:
        owner = bytes.fromhex(owner_hex)
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return AuthTagVerdict(False, "malformed_tag", owner_hex, conditions)
    if not schnorr_verify(auth_message(agent_pubkey_hex, conditions), owner, sig):
        return AuthTagVerdict(False, "bad_signature", owner_hex, conditions)
    return AuthTagVerdict(True, "ok", owner_hex, conditions)


def parse_tags(raw: str | list | None) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
