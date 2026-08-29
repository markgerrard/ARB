from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_or_create_key(path: Path) -> str:
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError("seat key must not be accessible by group or other users")
        secret = path.read_text(encoding="ascii").strip()
        _private_key(secret)
        return secret
    path.parent.mkdir(parents=True, exist_ok=True)
    secret_int = int.from_bytes(os.urandom(32), "big") % (CURVE_ORDER - 1) + 1
    secret = f"{secret_int:064x}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (secret + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return secret


def public_identity(secret_hex: str) -> tuple[str, str]:
    public = _private_key(secret_hex).public_key()
    numbers = public.public_numbers()
    compressed = public.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    ).hex()
    return f"{numbers.x:064x}", compressed


def sign_fields(fields: dict[str, Any], secret_hex: str) -> str:
    der = _private_key(secret_hex).sign(canonical_bytes(fields), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    if s > CURVE_ORDER // 2:
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        der = encode_dss_signature(r, CURVE_ORDER - s)
    return base64.b64encode(der).decode("ascii")


def verify_fields(fields: dict[str, Any], signature: str, compressed_pubkey: str, xonly: str) -> bool:
    try:
        raw = bytes.fromhex(compressed_pubkey)
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), raw)
        if f"{public.public_numbers().x:064x}" != xonly.lower():
            return False
        public.verify(base64.b64decode(signature, validate=True), canonical_bytes(fields), ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, TypeError, InvalidSignature, binascii.Error):
        return False


def _private_key(secret_hex: str) -> ec.EllipticCurvePrivateKey:
    try:
        value = int(secret_hex, 16)
    except ValueError as exc:
        raise ValueError("seat key must be 64 hexadecimal characters") from exc
    if len(secret_hex) != 64 or not 0 < value < CURVE_ORDER:
        raise ValueError("seat key is outside the secp256k1 scalar range")
    return ec.derive_private_key(value, ec.SECP256K1())
