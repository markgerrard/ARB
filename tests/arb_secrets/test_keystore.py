import pytest

from arb_crypto import fingerprint, generate_keypair
from arb_secrets.keystore import KeyMismatch, KeyStore, NoKey


class FakeRedis(dict):
    def set(self, k, v):
        self[k] = v

    def get(self, k):
        return super().get(k)


def test_resolve_tofu_pins_on_first_sight(tmp_path):
    r = FakeRedis()
    _, pub = generate_keypair()
    r.set("agent_scratch:secrets:pubkey:peer-b", pub)
    ks = KeyStore(r, "peer-a", tmp_path / "pins.b64")
    assert ks.resolve("peer-b") == pub
    assert ks.pin_fingerprint("peer-b") == fingerprint(pub)


def test_resolve_mismatch_after_pin_raises(tmp_path):
    r = FakeRedis()
    _, pub = generate_keypair()
    _, evil = generate_keypair()
    r.set("agent_scratch:secrets:pubkey:peer-b", pub)
    ks = KeyStore(r, "peer-a", tmp_path / "pins.b64")
    ks.resolve("peer-b")
    r.set("agent_scratch:secrets:pubkey:peer-b", evil)
    with pytest.raises(KeyMismatch):
        ks.resolve("peer-b")


def test_resolve_no_published_key_raises_nokey(tmp_path):
    ks = KeyStore(FakeRedis(), "peer-a", tmp_path / "pins.b64")
    with pytest.raises(NoKey):
        ks.resolve("peer-never-published")


def test_malformed_pin_line_does_not_break_resolve(tmp_path):
    r = FakeRedis()
    _, pub = generate_keypair()
    r.set("agent_scratch:secrets:pubkey:peer-b", pub)
    pins_path = tmp_path / "pins.b64"
    pins_path.write_text("truncated-line-without-fingerprint\n")
    ks = KeyStore(r, "peer-a", pins_path)

    assert ks.resolve("peer-b") == pub
    assert ks.pin_fingerprint("peer-b") == fingerprint(pub)
