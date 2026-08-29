import base64
import stat

from arb_crypto import fingerprint
from arb_secrets.cli import init_keypair, publish_key


class FakeRedis(dict):
    def set(self, k, v):
        self[k] = v

    def get(self, k):
        return dict.get(self, k)


def test_init_keypair_creates_mode_600_file_and_is_idempotent(tmp_path):
    path = tmp_path / "privkey.b64"
    first = init_keypair(path)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first_body = path.read_text()

    second = init_keypair(path)
    assert second == first
    assert path.read_text() == first_body
    assert len(base64.b64decode(first_body)) == 32


def test_publish_key_writes_pubkey_and_returns_fingerprint(tmp_path):
    r = FakeRedis()
    path = tmp_path / "privkey.b64"
    pubkey, fp = init_keypair(path)

    assert publish_key(r, "peer-a", path) == fp
    assert r["agent_scratch:secrets:pubkey:peer-a"] == pubkey
    assert fp == fingerprint(pubkey)
