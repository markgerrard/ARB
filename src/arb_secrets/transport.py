from __future__ import annotations

import json


class Transport:
    def __init__(self, redis):
        self.redis = redis

    def put_blob(self, recipient: str, blob_id: str, ciphertext: bytes, ttl: int) -> None:
        self.redis.set(self.blob_key(recipient, blob_id), ciphertext, ex=ttl)

    def claim_blob(self, recipient: str, blob_id: str) -> bytes | None:
        return self.redis.getdel(self.blob_key(recipient, blob_id))

    def push_pointer(self, recipient: str, envelope: dict) -> None:
        self.redis.lpush(self.inbox_key(recipient), json.dumps(envelope, sort_keys=True))

    def mark_seen(self, self_id: str, msg_id: str, ttl: int) -> bool:
        return bool(self.redis.set(self.seen_key(self_id, msg_id), "1", nx=True, ex=ttl))

    def set_status(self, sender: str, msg_id: str, value: str, ttl: int) -> None:
        self.redis.set(self.status_key(sender, msg_id), value, ex=ttl)

    def get_status(self, sender: str, msg_id: str) -> str | None:
        value = self.redis.get(self.status_key(sender, msg_id))
        if isinstance(value, bytes):
            return value.decode()
        return value

    def put_outstanding(self, self_id: str, req_id: str, value: dict, ttl: int) -> None:
        self.redis.set(self.outstanding_key(self_id, req_id), json.dumps(value), ex=ttl)

    def get_outstanding(self, self_id: str, req_id: str) -> dict | None:
        value = self.redis.get(self.outstanding_key(self_id, req_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        return json.loads(value)

    def del_outstanding(self, self_id: str, req_id: str) -> None:
        self.redis.delete(self.outstanding_key(self_id, req_id))

    @staticmethod
    def blob_key(recipient: str, blob_id: str) -> str:
        return f"agent_scratch:secrets:blob:{recipient}:{blob_id}"

    @staticmethod
    def inbox_key(recipient: str) -> str:
        return f"agent_scratch:agent:{recipient}:inbox"

    @staticmethod
    def seen_key(self_id: str, msg_id: str) -> str:
        return f"agent_scratch:secrets:seen:{self_id}:{msg_id}"

    @staticmethod
    def status_key(sender: str, msg_id: str) -> str:
        return f"agent_scratch:secrets:status:{sender}:{msg_id}"

    @staticmethod
    def outstanding_key(self_id: str, req_id: str) -> str:
        return f"agent_scratch:secrets:outstanding:{self_id}:{req_id}"
