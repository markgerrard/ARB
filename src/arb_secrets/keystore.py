from __future__ import annotations

import os
import secrets
from pathlib import Path

from arb_crypto import fingerprint


class KeyStoreError(Exception):
    pass


class KeyMismatch(KeyStoreError):
    pass


class NoKey(KeyStoreError):
    pass


class KeyStore:
    def __init__(self, redis, agent_id: str, pins_path: str | Path):
        self.redis = redis
        self.agent_id = agent_id
        self.pins_path = Path(pins_path).expanduser()

    def publish(self, pubkey: bytes) -> None:
        self.redis.set(self._pubkey_key(self.agent_id), pubkey)

    def resolve(self, peer_id: str) -> bytes:
        pubkey = self.redis.get(self._pubkey_key(peer_id))
        if pubkey is None:
            raise NoKey(f"no published key for {peer_id}")
        if isinstance(pubkey, str):
            pubkey = pubkey.encode("latin1")
        peer_fingerprint = fingerprint(pubkey)
        pinned = self.pin_fingerprint(peer_id)
        if pinned is None:
            self._write_pin(peer_id, peer_fingerprint)
            return pubkey
        if pinned != peer_fingerprint:
            raise KeyMismatch(f"published key for {peer_id} does not match local pin")
        return pubkey

    def pin_fingerprint(self, peer_id: str) -> str | None:
        return self._read_pins().get(peer_id)

    @staticmethod
    def _pubkey_key(agent_id: str) -> str:
        return f"agent_scratch:secrets:pubkey:{agent_id}"

    def _read_pins(self) -> dict[str, str]:
        if not self.pins_path.exists():
            return {}
        pins: dict[str, str] = {}
        for line in self.pins_path.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            peer_id, peer_fingerprint = parts
            pins[peer_id] = peer_fingerprint
        return pins

    def _write_pin(self, peer_id: str, peer_fingerprint: str) -> None:
        self.pins_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pins = self._read_pins()
        pins[peer_id] = peer_fingerprint
        body = "".join(f"{name} {fp}\n" for name, fp in sorted(pins.items()))
        tmp_path = self.pins_path.with_name(f".{self.pins_path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as tmp:
                tmp.write(body)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.pins_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
