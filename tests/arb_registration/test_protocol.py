from __future__ import annotations

from pathlib import Path

from arb_registration.crypto import load_or_create_key, public_identity
from arb_registration.protocol import REQUEST_EVENT, parse_approval_reply, signed_data, verify_signed_data


def test_reply_parser_accepts_only_exact_action_and_request_id():
    assert parse_approval_reply(" approve abcdef12 ").action == "approve"
    assert parse_approval_reply("DENY 12345678-abcd").request_id == "12345678-abcd"
    for invalid in ("approve", "please approve abcdef12", "approve abc", "approve abcdef12 extra", "grant abcdef12"):
        assert parse_approval_reply(invalid) is None


def test_signature_is_bound_to_xonly_nostr_identity(tmp_path):
    secret = load_or_create_key(tmp_path / "seat.key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {"pubkey": pubkey, "name": "seat"}, secret, compressed)
    assert verify_signed_data(data)
    assert (tmp_path / "seat.key").stat().st_mode & 0o777 == 0o600

    data["name"] = "attacker"
    assert not verify_signed_data(data)

def test_signature_rejects_different_xonly_identity(tmp_path):
    secret = load_or_create_key(tmp_path / "seat.key")
    pubkey, compressed = public_identity(secret)
    data = signed_data(REQUEST_EVENT, {"pubkey": pubkey, "name": "seat"}, secret, compressed)
    data["pubkey"] = "00" * 32
    assert not verify_signed_data(data)
