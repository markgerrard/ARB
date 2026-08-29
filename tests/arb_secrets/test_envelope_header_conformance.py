"""Every ARB Secrets envelope must satisfy the bus's own header contract.

`_envelope` used to return `{"kind", "payload"}` alone, discarding the sender and
recipient it was handed, so every drop/request/reply since it was written has been
malformed at the top level. `agent_redis_bridge.envelope` requires
("id", "from", "branch", "to", "kind", "sent_at", "payload") and a daemon receiving one
of these rejects it as `envelope-invalid missing-id` — the sealed delivery never lands.

It hid because the only consumers so far were interactive peers reading `payload.data`
(which does carry from/to) rather than daemons parsing the header. It surfaced when
claude-orch-mini-dev received a relayed drop and reported it as unattributed: an envelope
with no `from` on an inbox is exactly the shape a spoofed message would take, so the
recipient could not distinguish "malformed by us" from "injected by someone else".

These tests assert against the REAL consumer (`parse_header`) rather than a local list of
field names, so they cannot drift apart from the contract they are protecting.
"""

from __future__ import annotations

import json

import pytest

from agent_redis_bridge.envelope import parse_header
from arb_crypto import generate_keypair
from arb_secrets.protocol import build_drop, build_reply, build_request

SENDER = "codex-arbmem-prod"
RECIPIENT = "claude-orch-mini-dev-cli"


class _KeyStore:
    """Minimal resolve()-only stand-in; these tests are about the header, not the crypto."""

    def __init__(self, keys: dict[str, bytes], priv: bytes) -> None:
        self._keys = keys
        self.privkey = priv

    def resolve(self, peer_id: str) -> bytes:
        return self._keys[peer_id]


@pytest.fixture()
def keys():
    s_priv, s_pub = generate_keypair()
    r_priv, r_pub = generate_keypair()
    return {
        "store": _KeyStore({SENDER: s_pub, RECIPIENT: r_pub}, s_priv),
        "sender_priv": s_priv,
        "recipient_priv": r_priv,
    }


def _envelopes(keys):
    ks = keys["store"]
    drop, _msg_id, _ct = build_drop(keys["sender_priv"], SENDER, RECIPIENT, b"s3cret", 600, ks)
    request, *_ = build_request(keys["sender_priv"], SENDER, RECIPIENT, "label", 600, ks)
    reply_meta = {
        "req_id": "r1", "holder": RECIPIENT, "what": "label",
        "from": SENDER, "to": RECIPIENT, "expires_at": 9e12,
    }
    reply, *_ = build_reply(keys["recipient_priv"], RECIPIENT, reply_meta, b"s3cret", 600, ks)
    return {"secret_drop": drop, "secret_request": request, "secret_reply": reply}


def test_every_builder_produces_a_header_the_bus_parser_accepts(keys):
    for name, env in _envelopes(keys).items():
        # parse_header raises EnvelopeError on any missing/invalid field — the same call
        # the bridge makes on an inbound envelope.
        header = parse_header(json.dumps(env))
        assert header.sender, f"{name}: empty from"
        assert header.recipient if hasattr(header, "recipient") else env["to"], name


def test_header_attributes_the_message_so_a_recipient_need_not_trust_payload_data(keys):
    """The defect's real cost: an unattributed envelope is indistinguishable from a spoof."""
    for name, env in _envelopes(keys).items():
        assert env["from"] in (SENDER, RECIPIENT), f"{name}: header lost the sender"
        assert env["to"] in (SENDER, RECIPIENT), f"{name}: header lost the recipient"
        assert env["from"] == env["payload"]["data"]["from"], f"{name}: header/payload disagree"
        assert env["to"] == env["payload"]["data"]["to"], f"{name}: header/payload disagree"


def test_ids_are_unique_per_envelope(keys):
    first = _envelopes(keys)["secret_drop"]["id"]
    second = _envelopes(keys)["secret_drop"]["id"]
    assert first != second, "envelope id must not be reused across messages"
