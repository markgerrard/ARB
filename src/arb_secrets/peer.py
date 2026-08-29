from __future__ import annotations

import json
import time
import base64
import os
from dataclasses import dataclass
from pathlib import Path

from nacl.public import PrivateKey

from arb_crypto import generate_keypair, load_privkey
from arb_secrets.keystore import KeyStore
from arb_secrets.protocol import (
    accept_drop,
    accept_reply,
    accept_request,
    build_drop,
    build_reply,
    build_request,
    Rejection,
)
from arb_secrets.transport import Transport


@dataclass
class Incoming:
    event: str
    sender: str
    secret: bytes | None = None
    what: str | None = None
    meta: dict | None = None


class Peer:
    def __init__(
        self,
        redis,
        agent_id: str,
        privkey_path: str | Path,
        pins_path: str | Path,
        allowed_requesters: set[str] = frozenset(),
        audit_sink=None,
    ):
        self.redis = redis
        self.agent_id = agent_id
        self.privkey_path = Path(privkey_path).expanduser()
        self.allowed_requesters = set(allowed_requesters)
        self.audit_sink = audit_sink
        self.transport = Transport(redis)
        self.privkey = self._load_or_create_privkey()
        self.pubkey = bytes(PrivateKey(self.privkey).public_key)
        self.keystore = KeyStore(redis, agent_id, pins_path)
        self.keystore.privkey = self.privkey
        self.keystore.publish(self.pubkey)
        self.outstanding: dict[str, dict] = {}

    def push_secret(self, to: str, secret: bytes, ttl: int = 3600) -> str:
        envelope, blob_id, ct = build_drop(self.privkey, self.agent_id, to, secret, ttl, self.keystore)
        self.transport.put_blob(to, blob_id, ct, ttl)
        self.transport.push_pointer(to, envelope)
        self.transport.set_status(self.agent_id, blob_id, "sent", ttl)
        self._audit(envelope, "sent")
        return blob_id

    def request_secret(self, holder: str, what: str, ttl: int = 3600) -> str:
        envelope, req_id, ct = build_request(self.privkey, self.agent_id, holder, what, ttl, self.keystore)
        self.transport.put_blob(holder, req_id, ct, ttl)
        self.transport.push_pointer(holder, envelope)
        entry = {"holder": holder, "what": what, "expires_at": envelope["payload"]["data"]["expires_at"]}
        self.outstanding[req_id] = entry
        self.transport.put_outstanding(self.agent_id, req_id, entry, ttl)
        self.transport.set_status(self.agent_id, req_id, "sent", ttl)
        self._audit(envelope, "sent")
        return req_id

    def claim_incoming(self) -> list[Incoming]:
        claimed: list[Incoming] = []
        for envelope in self._drain_inbox():
            data = envelope["payload"]["data"]
            event = envelope["payload"]["event"]
            blob_id = self._blob_id(event, data)
            ct = self.transport.claim_blob(self.agent_id, blob_id)
            if ct is None:
                continue
            try:
                if event == "secret_drop":
                    secret = accept_drop(envelope, ct, self.agent_id, self.keystore, self.transport)
                    self.transport.set_status(data["from"], blob_id, "claimed", self._status_ttl(data))
                    claimed.append(Incoming(event=event, sender=data["from"], secret=secret, meta=data))
                elif event == "secret_request":
                    meta = accept_request(
                        envelope,
                        ct,
                        self.agent_id,
                        self.keystore,
                        self.transport,
                        self.allowed_requesters,
                    )
                    self.transport.set_status(data["from"], blob_id, "claimed", self._status_ttl(data))
                    claimed.append(Incoming(event=event, sender=data["from"], what=meta["what"], meta=meta))
                elif event == "secret_reply":
                    outstanding = self._outstanding_with_store(data["in_reply_to"])
                    expected = outstanding.get(data["in_reply_to"])
                    secret = accept_reply(envelope, ct, self.agent_id, self.keystore, self.transport, outstanding)
                    self.outstanding.pop(data["in_reply_to"], None)
                    self.transport.del_outstanding(self.agent_id, data["in_reply_to"])
                    self.transport.set_status(data["from"], blob_id, "claimed", self._status_ttl(data))
                    claimed.append(
                        Incoming(
                            event=event,
                            sender=data["from"],
                            secret=secret,
                            what=expected["what"] if expected else None,
                            meta=data,
                        )
                    )
                self._audit(envelope, "claimed")
            except Rejection as exc:
                if event == "secret_reply":
                    self.outstanding.pop(data["in_reply_to"], None)
                    self.transport.del_outstanding(self.agent_id, data["in_reply_to"])
                self.transport.set_status(data["from"], blob_id, "rejected", self._status_ttl(data))
                rejected = dict(data)
                rejected["rejection"] = type(exc).__name__
                claimed.append(Incoming(event=event, sender=data["from"], meta=rejected))
                self._audit(envelope, "rejected")
        return claimed

    def respond_to_request(self, incoming_request: Incoming, secret: bytes, ttl: int = 3600) -> str:
        if incoming_request.meta is None:
            raise ValueError("incoming request has no metadata")
        envelope, blob_id, ct = build_reply(
            self.privkey,
            self.agent_id,
            incoming_request.meta,
            secret,
            ttl,
            self.keystore,
        )
        recipient = envelope["payload"]["data"]["to"]
        self.transport.put_blob(recipient, blob_id, ct, ttl)
        self.transport.push_pointer(recipient, envelope)
        self.transport.set_status(self.agent_id, blob_id, "sent", ttl)
        self._audit(envelope, "sent")
        return blob_id

    def _load_or_create_privkey(self) -> bytes:
        if self.privkey_path.exists():
            return load_privkey(self.privkey_path)
        priv, _ = generate_keypair()
        os.makedirs(self.privkey_path.parent, mode=0o700, exist_ok=True)
        fd = os.open(self.privkey_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.b64encode(priv).decode("ascii"))
        return priv

    def _drain_inbox(self) -> list[dict]:
        key = self.transport.inbox_key(self.agent_id)
        envelopes = []
        while True:
            item = self._pop_inbox_item(key)
            if item is None:
                break
            if isinstance(item, bytes):
                item = item.decode()
            envelopes.append(json.loads(item))
        return envelopes

    def _pop_inbox_item(self, key: str):
        if hasattr(self.redis, "rpop"):
            return self.redis.rpop(key)
        values = self.redis.get(key) or []
        if not values:
            return None
        return values.pop()

    def _outstanding_with_store(self, req_id: str) -> dict[str, dict]:
        if req_id not in self.outstanding:
            stored = self.transport.get_outstanding(self.agent_id, req_id)
            if stored is not None:
                self.outstanding[req_id] = stored
        return self.outstanding

    @staticmethod
    def _status_ttl(data: dict) -> int:
        return max(1, int(data["expires_at"] - time.time()))

    @staticmethod
    def _blob_id(event: str, data: dict) -> str:
        if event == "secret_drop":
            return data["msg_id"]
        if event == "secret_request":
            return data["req_id"]
        return data["reply_msg_id"]

    def _audit(self, envelope: dict, decision: str) -> None:
        if self.audit_sink is None:
            return
        data = envelope["payload"]["data"]
        self.audit_sink(
            {
                "from": data["from"],
                "to": data["to"],
                "event": envelope["payload"]["event"],
                "id": self._blob_id(envelope["payload"]["event"], data),
                "decision": decision,
                "fingerprints": self._audit_fingerprints(data),
                "ts": time.time(),
            }
        )

    def _audit_fingerprints(self, data: dict) -> dict[str, str]:
        fingerprints = {}
        for peer_id in (data["from"], data["to"]):
            pinned = self.keystore.pin_fingerprint(peer_id)
            if pinned is not None:
                fingerprints[peer_id] = pinned
        return fingerprints
