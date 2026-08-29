import pytest
from nacl.exceptions import CryptoError

from arb_crypto import box_open, box_seal, generate_keypair


def test_box_roundtrip_authenticated():
    a_priv, a_pub = generate_keypair()
    b_priv, b_pub = generate_keypair()
    ct = box_seal(b"cred", a_priv, b_pub)
    assert box_open(ct, b_priv, a_pub) == b"cred"


def test_box_wrong_sender_fails():
    a_priv, a_pub = generate_keypair()
    b_priv, b_pub = generate_keypair()
    _, c_pub = generate_keypair()
    ct = box_seal(b"cred", a_priv, b_pub)
    with pytest.raises(CryptoError):
        box_open(ct, b_priv, c_pub)


def test_box_tamper_fails():
    a_priv, a_pub = generate_keypair()
    b_priv, b_pub = generate_keypair()
    ct = bytearray(box_seal(b"cred", a_priv, b_pub))
    ct[-1] ^= 1
    with pytest.raises(CryptoError):
        box_open(bytes(ct), b_priv, a_pub)
