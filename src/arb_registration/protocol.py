from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any
from uuid import uuid4

from .crypto import sign_fields, verify_fields


REQUEST_EVENT = "seat_registration_request"
PROFILE_READY_EVENT = "seat_registration_profile_ready"
PROVISION_EVENT = "seat_registration_provision"
GRANT_EVENT = "seat_registration_grant"
DENY_EVENT = "seat_registration_deny"
BUS_REQUEST_EVENT = "bus_registration_request"
BUS_GRANT_EVENT = "bus_registration_grant"
BUS_DENY_EVENT = "bus_registration_deny"
SIGNED_EVENTS = {REQUEST_EVENT, PROFILE_READY_EVENT, BUS_REQUEST_EVENT}
_REPLY = re.compile(r"^\s*(approve|deny)\s+([0-9a-fA-F-]{8,64})\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ApprovalReply:
    action: str
    request_id: str


def parse_approval_reply(content: str) -> ApprovalReply | None:
    match = _REPLY.fullmatch(content)
    if not match:
        return None
    return ApprovalReply(match.group(1).lower(), match.group(2).lower())


def signed_data(event: str, fields: dict[str, Any], secret_hex: str, compressed_pubkey: str) -> dict[str, Any]:
    body = {"event": event, **fields, "signing_pubkey": compressed_pubkey}
    return {**body, "signature": sign_fields(body, secret_hex)}


def verify_signed_data(data: dict[str, Any]) -> bool:
    signature = data.get("signature")
    compressed = data.get("signing_pubkey")
    xonly = data.get("pubkey")
    event = data.get("event")
    if not all(isinstance(value, str) for value in (signature, compressed, xonly, event)):
        return False
    if event not in SIGNED_EVENTS:
        return False
    body = {key: value for key, value in data.items() if key != "signature"}
    return verify_fields(body, signature, compressed, xonly)


def notify_envelope(sender: str, recipient: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": str(uuid4()), "from": sender, "branch": "seat-registration", "to": recipient,
        "kind": "notify", "sent_at": now, "payload": {"event": event, "data": data},
    }
