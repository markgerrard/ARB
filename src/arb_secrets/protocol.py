from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from nacl.exceptions import CryptoError

from arb_crypto import box_open, box_seal

SEEN_TTL = 24 * 60 * 60


class Rejection(Exception):
    pass


class Expired(Rejection):
    pass


class Replay(Rejection):
    pass


class AuthFail(Rejection):
    pass


class WrongAnswer(Rejection):
    pass


class WrongHolder(Rejection):
    pass


class Unauthorized(Rejection):
    pass


def build_drop(sender_priv: bytes, sender_id: str, to: str, secret: bytes, ttl: int, ks):
    msg_id = _new_id()
    expires_at = _expires_at(ttl)
    body = {"msg_id": msg_id, "secret": _b64(secret), "expires_at": expires_at}
    ct = box_seal(_dump(body), sender_priv, ks.resolve(to))
    return _envelope("secret_drop", sender_id, to, {"msg_id": msg_id, "expires_at": expires_at}), msg_id, ct


def build_request(sender_priv: bytes, sender_id: str, holder: str, what: str, ttl: int, ks):
    req_id = _new_id()
    expires_at = _expires_at(ttl)
    body = {
        "req_id": req_id,
        "what": what,
        "requester_agent_id": sender_id,
        "expires_at": expires_at,
    }
    ct = box_seal(_dump(body), sender_priv, ks.resolve(holder))
    return (
        _envelope("secret_request", sender_id, holder, {"req_id": req_id, "expires_at": expires_at}),
        req_id,
        ct,
    )


def build_reply(holder_priv: bytes, holder_id: str, request_meta: dict, secret: bytes, ttl: int, ks):
    reply_msg_id = _new_id()
    req_id = request_meta["req_id"]
    requester = request_meta["from"]
    expires_at = _expires_at(ttl)
    body = {
        "reply_msg_id": reply_msg_id,
        "in_reply_to": req_id,
        "echo_what": request_meta["what"],
        "secret": _b64(secret),
        "expires_at": expires_at,
    }
    ct = box_seal(_dump(body), holder_priv, ks.resolve(requester))
    return (
        _envelope(
            "secret_reply",
            holder_id,
            requester,
            {"reply_msg_id": reply_msg_id, "in_reply_to": req_id, "expires_at": expires_at},
        ),
        reply_msg_id,
        ct,
    )


def accept_drop(env: dict, ct: bytes, self_id: str, ks, transport) -> bytes:
    data = _data(env)
    sender = data["from"]
    body = _open_body(ct, self_id, ks, sender)
    _require_to_self(data, self_id)
    _require_fresh(body.get("expires_at", data["expires_at"]))
    _require_same(body, data, "msg_id")
    msg_id = body["msg_id"]
    _mark_seen(transport, self_id, msg_id)
    return _unb64(body["secret"])


def accept_request(
    env: dict,
    ct: bytes,
    self_id: str,
    ks,
    transport,
    allowed_requesters: set[str],
) -> dict:
    data = _data(env)
    sender = data["from"]
    body = _open_body(ct, self_id, ks, sender)
    if sender not in allowed_requesters:
        raise Unauthorized(f"{sender} is not allowed to request secrets")
    _require_to_self(data, self_id)
    _require_fresh(body.get("expires_at", data["expires_at"]))
    _require_same(body, data, "req_id")
    req_id = body["req_id"]
    _mark_seen(transport, self_id, req_id)
    return {
        "from": sender,
        "to": self_id,
        "req_id": req_id,
        "what": body["what"],
        "expires_at": body["expires_at"],
    }


def accept_reply(env: dict, ct: bytes, self_id: str, ks, transport, outstanding: dict) -> bytes:
    data = _data(env)
    sender = data["from"]
    body = _open_body(ct, self_id, ks, sender)
    req_id = body.get("in_reply_to", data["in_reply_to"])
    expected = outstanding.get(req_id)
    if expected is None:
        raise WrongAnswer(f"no outstanding request for {req_id}")
    if "expires_at" in expected:
        _require_fresh(expected["expires_at"])
    if sender != expected["holder"]:
        raise WrongHolder(f"reply from {sender}, expected {expected['holder']}")
    if body["echo_what"] != expected["what"]:
        raise WrongAnswer("reply does not answer the requested secret label")
    _require_to_self(data, self_id)
    _require_fresh(body.get("expires_at", data["expires_at"]))
    _require_same(body, data, "in_reply_to")
    _require_same(body, data, "reply_msg_id")
    reply_msg_id = body["reply_msg_id"]
    _mark_seen(transport, self_id, reply_msg_id)
    if hasattr(outstanding, "pop"):
        outstanding.pop(req_id, None)
    return _unb64(body["secret"])


def _envelope(event: str, sender: str, to: str, data: dict) -> dict:
    """Build a bus envelope carrying the FULL header, not just kind+payload.

    This used to return `{"kind", "payload"}` alone, discarding the `sender` and `to` it
    was handed — every ARB Secrets drop/request/reply since has been malformed at the top
    level. `agent_redis_bridge.envelope._header_from_value` requires
    ("id", "from", "branch", "to", "kind", "sent_at", "payload"), so a bridge daemon
    receiving one of these rejects it as `envelope-invalid missing-id` and the sealed
    delivery silently never lands.

    It survived because the only consumers so far were interactive peers reading
    `payload.data` (which does carry from/to) rather than daemons parsing the header. It
    surfaced in production when a peer's orchestrator seat received a relayed drop and
    reported the envelope as unattributed: **an envelope with no `from` on an inbox is exactly the
    shape a spoofed message would take**, so a recipient cannot distinguish "malformed by
    us" from "injected by someone else" and must fall back to checking the crypto.

    `from`/`to` stay duplicated inside `data` because `_require_to_self` and the holder
    checks read them there; the header is additive.
    """
    data = {"from": sender, "to": to, **data, "blob_key": ""}
    id_field = "msg_id" if event == "secret_drop" else "req_id" if event == "secret_request" else "reply_msg_id"
    data["blob_key"] = f"agent_scratch:secrets:blob:{to}:{data[id_field]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": str(uuid4()),
        "from": sender,
        "branch": "arb-secrets",
        "to": to,
        "kind": "notify",
        "sent_at": now,
        "payload": {"event": event, "data": data},
    }


def _open_body(ct: bytes, self_id: str, ks, sender: str) -> dict:
    try:
        return json.loads(box_open(ct, _priv(ks), ks.resolve(sender)).decode())
    except (CryptoError, ValueError, json.JSONDecodeError) as exc:
        raise AuthFail("could not authenticate encrypted body") from exc


def _priv(ks) -> bytes:
    return getattr(ks, "privkey")


def _data(env: dict) -> dict:
    return env["payload"]["data"]


def _require_to_self(data: dict, self_id: str) -> None:
    if data["to"] != self_id:
        raise AuthFail("envelope addressed to different recipient")


def _require_fresh(expires_at: float) -> None:
    if expires_at <= time.time():
        raise Expired("message expired")


def _require_same(body: dict, data: dict, field: str) -> None:
    if body[field] != data[field]:
        raise AuthFail(f"authenticated {field} does not match envelope")


def _mark_seen(transport, self_id: str, msg_id: str) -> None:
    if not transport.mark_seen(self_id, msg_id, SEEN_TTL):
        raise Replay(f"{msg_id} was already processed")


def _new_id() -> str:
    return secrets.token_hex(32)


def _expires_at(ttl: int) -> float:
    if ttl > SEEN_TTL:
        raise ValueError("ttl cannot exceed replay seen window")
    return time.time() + ttl


def _dump(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True).encode()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))
