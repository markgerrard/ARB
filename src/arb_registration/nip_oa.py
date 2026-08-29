"""NIP-OA owner-key handling: nsec decoding and BIP-340 signing via libsecp256k1.

WHY THERE IS NO CURVE ARITHMETIC HERE ANY MORE. This module used to carry a
hand-written secp256k1 implementation -- point addition, scalar multiplication,
x-only lifting, the BIP-340 tagged hash. It existed because the production image
was `FROM python:3.14-slim` and coincurve (which wraps libsecp256k1, the
reference C implementation) publishes no cp314 wheel, so it could not ship.

That constraint was incidental, not required: `requires-python` is >=3.11, no
test pinned the base image, and the sibling `deploy/seat-host/Dockerfile` was
already on 3.11. The image moved to 3.13-slim, coincurve became a runtime
dependency, and the arithmetic went with it.

WHAT THE GUARDS BELOW ARE STILL DOING. coincurve is not a drop-in for the
deleted code at the boundaries, and the differences all fail OPEN:

  * `PublicKeyXOnly(b)` with fewer than 32 bytes does not raise -- it pads and
    parses a DIFFERENT key. Measured: b"\\x01" * 31 parses as 0101..0100.
  * `PrivateKey(b)` with fewer than 32 bytes likewise does not raise.
  * `verify()` raises ValueError on a malformed signature length where this
    module's callers expect False.

So every explicit length check here is load-bearing and must not be "simplified"
into the library call. What coincurve DOES subsume exactly is the on-curve test:
`PublicKeyXOnly(x)` raising ValueError partitions x identically to the old
`_lift_x(x) is None` (checked over x in 1..399, 399/399 agreement).

Message length is not among the differences: coincurve 21 is 32-byte-message
only and raises on anything else, matching the pre-existing scope limit rather
than widening it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from coincurve import PrivateKey, PublicKeyXOnly


CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class OwnerKeyError(ValueError):
    pass


def load_owner_secret(path: Path, expected_owner_pubkey: str) -> bytes:
    if path.stat().st_mode & 0o077:
        raise OwnerKeyError("owner key file must not be accessible by group or other users")
    secret = decode_secret(path.read_text(encoding="ascii").strip())
    owner_pubkey = xonly_pubkey(secret)
    if owner_pubkey != expected_owner_pubkey.lower():
        raise OwnerKeyError("owner key does not match ARB_REGISTRAR_MARK_PUBKEY")
    return secret


def decode_secret(value: str) -> bytes:
    if len(value) == 64:
        try:
            secret = bytes.fromhex(value)
        except ValueError as exc:
            raise OwnerKeyError("owner key must be 64-hex or nsec") from exc
    elif value.lower().startswith("nsec1"):
        secret = _decode_nsec(value)
    else:
        raise OwnerKeyError("owner key must be 64-hex or nsec")
    scalar = int.from_bytes(secret, "big")
    if len(secret) != 32 or not 0 < scalar < CURVE_ORDER:
        raise OwnerKeyError("owner key is outside the secp256k1 scalar range")
    return secret


def xonly_pubkey(secret: bytes) -> str:
    # Length first: PublicKeyXOnly.from_secret does not reject a short buffer.
    if len(secret) != 32:
        raise OwnerKeyError("owner key is outside the secp256k1 scalar range")
    try:
        return PublicKeyXOnly.from_secret(secret).format().hex()
    except ValueError as exc:
        raise OwnerKeyError("owner key is outside the secp256k1 scalar range") from exc


def build_auth_tag(
    owner_secret: bytes, agent_pubkey: str, *, aux_rand: bytes | None = None,
) -> str:
    try:
        agent_bytes = bytes.fromhex(agent_pubkey)
    except ValueError as exc:
        raise OwnerKeyError("agent pubkey must be 64 hexadecimal characters") from exc
    if len(agent_bytes) != 32 or not _is_valid_xonly(agent_bytes):
        raise OwnerKeyError("agent pubkey is not a valid x-only secp256k1 key")
    owner_pubkey = xonly_pubkey(owner_secret)
    if owner_pubkey == agent_pubkey.lower():
        raise OwnerKeyError("owner and agent pubkeys must differ")
    conditions = "kind=0"
    message = hashlib.sha256(
        f"nostr:agent-auth:{agent_pubkey.lower()}:{conditions}".encode("ascii")
    ).digest()
    signature = schnorr_sign(
        message, owner_secret, aux_rand=os.urandom(32) if aux_rand is None else aux_rand
    )
    # Kept despite both halves now being the same library. It no longer detects a
    # shared-defect signer/verifier pair -- that was its original (weak) purpose
    # and libsecp256k1 removes the need -- but it still catches a corrupted key
    # buffer or a wrong-owner mismatch before the tag is published.
    if not schnorr_verify(message, bytes.fromhex(owner_pubkey), signature):
        raise OwnerKeyError("generated BIP-340 signature did not verify")
    return json.dumps(
        ["auth", owner_pubkey, conditions, signature.hex()],
        separators=(",", ":"),
    )


def schnorr_sign(message: bytes, secret: bytes, *, aux_rand: bytes) -> bytes:
    """BIP-340 signing, delegated to libsecp256k1.

    The length checks are not redundant with the library: coincurve accepts a
    short secret buffer and signs with a different key rather than raising.
    """
    if len(message) != 32 or len(secret) != 32 or len(aux_rand) != 32:
        raise OwnerKeyError("BIP-340 signing inputs must be 32 bytes")
    try:
        return PrivateKey(secret).sign_schnorr(message, aux_rand)
    except ValueError as exc:
        raise OwnerKeyError("owner key is outside the secp256k1 scalar range") from exc


def schnorr_verify(message: bytes, pubkey: bytes, signature: bytes) -> bool:
    """BIP-340 verification, delegated to libsecp256k1.

    False on any malformed input rather than raising -- callers classify, and a
    length error is as much a verification failure as a wrong signature. The
    try/except is what converts coincurve's ValueError (off-curve x, and any
    length it does police) into that contract.
    """
    if len(message) != 32 or len(pubkey) != 32 or len(signature) != 64:
        return False
    try:
        return PublicKeyXOnly(pubkey).verify(signature, message)
    except ValueError:
        return False


def _is_valid_xonly(raw: bytes) -> bool:
    """Exact replacement for the old `_lift_x(x) is not None`, for 32-byte input.

    Caller must check the length first; see the module docstring on padding.
    """
    try:
        PublicKeyXOnly(raw)
    except ValueError:
        return False
    return True


def _decode_nsec(value: str) -> bytes:
    if value.lower() != value and value.upper() != value:
        raise OwnerKeyError("nsec must not mix uppercase and lowercase")
    normalized = value.lower()
    separator = normalized.rfind("1")
    if separator < 1 or separator + 7 > len(normalized):
        raise OwnerKeyError("nsec is malformed")
    hrp = normalized[:separator]
    if hrp != "nsec":
        raise OwnerKeyError("owner key bech32 prefix must be nsec")
    try:
        data = [BECH32_CHARSET.index(char) for char in normalized[separator + 1:]]
    except ValueError as exc:
        raise OwnerKeyError("nsec contains an invalid bech32 character") from exc
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise OwnerKeyError("nsec checksum is invalid")
    decoded = _convert_bits(data[:-6], 5, 8, pad=False)
    if decoded is None or len(decoded) != 32:
        raise OwnerKeyError("nsec payload must decode to 32 bytes")
    return bytes(decoded)


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _convert_bits(
    data: list[int], from_bits: int, to_bits: int, *, pad: bool,
) -> list[int] | None:
    accumulator = 0
    bits = 0
    result = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None
    return result
