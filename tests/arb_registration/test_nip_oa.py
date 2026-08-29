from __future__ import annotations

import hashlib
import json

import pytest

import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from arb_registration.crypto import public_identity
from arb_registration.nip_oa import (
    OwnerKeyError,
    build_auth_tag,
    load_owner_secret,
    schnorr_verify,
    xonly_pubkey,
)


OWNER_SECRET = (3).to_bytes(32, "big")
OWNER_NSEC = "nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqps52s3re"


def test_nsec_fixture_is_accepted_and_matches_owner_pubkey(tmp_path):
    path = tmp_path / "owner.key"
    path.write_text(OWNER_NSEC + "\n")
    path.chmod(0o600)
    expected = xonly_pubkey(OWNER_SECRET)

    assert load_owner_secret(path, expected) == OWNER_SECRET


def test_owner_key_file_rejects_public_mode_and_wrong_mark(tmp_path):
    path = tmp_path / "owner.key"
    path.write_text(OWNER_SECRET.hex() + "\n")
    path.chmod(0o644)
    with pytest.raises(OwnerKeyError, match="group or other"):
        load_owner_secret(path, xonly_pubkey(OWNER_SECRET))
    path.chmod(0o600)
    with pytest.raises(OwnerKeyError, match="does not match"):
        load_owner_secret(path, "aa" * 32)


def test_kind_zero_auth_tag_uses_exact_nip_oa_preimage_and_bip340_signature():
    agent_pubkey, _ = public_identity(f"{4:064x}")
    tag_json = build_auth_tag(OWNER_SECRET, agent_pubkey, aux_rand=b"\x00" * 32)
    tag = json.loads(tag_json)

    assert tag[:3] == ["auth", xonly_pubkey(OWNER_SECRET), "kind=0"]
    message = hashlib.sha256(
        f"nostr:agent-auth:{agent_pubkey}:kind=0".encode("ascii")
    ).digest()
    assert schnorr_verify(message, bytes.fromhex(tag[1]), bytes.fromhex(tag[3]))
    assert len(tag[3]) == 128


def test_owner_cannot_self_attest():
    with pytest.raises(OwnerKeyError, match="must differ"):
        build_auth_tag(
            OWNER_SECRET, xonly_pubkey(OWNER_SECRET), aux_rand=b"\x00" * 32
        )
