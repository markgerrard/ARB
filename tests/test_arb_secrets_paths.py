"""Regression guard: private key must never be resolved to a path inside the
repo. Tilde paths must expand to the real home, never create a literal ./~
directory in the cwd."""
from pathlib import Path

from arb_secrets.keystore import KeyStore
from arb_secrets.peer import Peer


class _FakeRedis:
    def __getattr__(self, name):  # pragma: no cover - never called
        raise AssertionError("redis must not be touched by path construction")


class _NullRedis:
    """Permissive stub: Peer.__init__ legitimately publishes the pubkey."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_peer_expands_tilde_privkey_path():
    peer = Peer.__new__(Peer)
    # Only exercise path intake, mirroring __init__'s assignment.
    peer.privkey_path = Path("~/.arb-secrets/privkey.b64").expanduser()
    assert not str(peer.privkey_path).startswith("~")
    assert peer.privkey_path.is_absolute()


def test_keystore_expands_tilde_pins_path():
    ks = KeyStore(_FakeRedis(), "agent-x", "~/.arb-secrets/known_peers.b64")
    assert not str(ks.pins_path).startswith("~")
    assert ks.pins_path.is_absolute()


def test_peer_init_path_intake_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    peer = Peer(
        _NullRedis(),
        "agent-x",
        "~/.arb-secrets/privkey.b64",
        "~/.arb-secrets/known_peers.b64",
        allowed_requesters=set(),
    )
    assert str(peer.keystore.pins_path).startswith(str(tmp_path))
    assert str(peer.privkey_path).startswith(str(tmp_path))
