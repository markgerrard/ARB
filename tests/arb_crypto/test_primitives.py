import pytest

from arb_crypto import fingerprint, generate_keypair, seal, unseal, validate_public_key


def test_sealbox_roundtrip():
    priv, pub = generate_keypair()
    ct = seal(b"secret-bytes", pub)
    assert unseal(ct, priv) == b"secret-bytes"


def test_fingerprint_stable_and_hex():
    _, pub = generate_keypair()
    fp = fingerprint(pub)
    assert fp == fingerprint(pub)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_validate_rejects_malformed():
    with pytest.raises(ValueError):
        validate_public_key(b"too-short")
