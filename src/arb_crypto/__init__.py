from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as nacl_random


def validate_public_key(pubkey_bytes: bytes) -> PublicKey:
    try:
        return PublicKey(pubkey_bytes)
    except Exception as exc:
        raise ValueError("malformed public key") from exc


def seal(plaintext: bytes, recipient_pubkey: bytes) -> bytes:
    return SealedBox(validate_public_key(recipient_pubkey)).encrypt(plaintext)


def unseal(sealed: bytes, recipient_privkey: bytes) -> bytes:
    return SealedBox(PrivateKey(recipient_privkey)).decrypt(sealed)


def box_seal(plaintext: bytes, sender_privkey: bytes, recipient_pubkey: bytes) -> bytes:
    box = Box(PrivateKey(sender_privkey), validate_public_key(recipient_pubkey))
    nonce = nacl_random(Box.NONCE_SIZE)
    return nonce + box.encrypt(plaintext, nonce).ciphertext


def box_open(ciphertext: bytes, recipient_privkey: bytes, sender_pubkey: bytes) -> bytes:
    box = Box(PrivateKey(recipient_privkey), validate_public_key(sender_pubkey))
    nonce = ciphertext[: Box.NONCE_SIZE]
    body = ciphertext[Box.NONCE_SIZE :]
    return box.decrypt(body, nonce)


def fingerprint(pubkey_bytes: bytes) -> str:
    return hashlib.sha256(pubkey_bytes).hexdigest()


def generate_keypair() -> tuple[bytes, bytes]:
    sk = PrivateKey.generate()
    return bytes(sk), bytes(sk.public_key)


def load_privkey(path: str | Path) -> bytes:
    return base64.b64decode(Path(path).read_text().strip())
