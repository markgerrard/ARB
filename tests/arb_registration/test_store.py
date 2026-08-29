from __future__ import annotations

from datetime import timedelta
import json

import pytest

from arb_registration.store import RegistrationStore, TokenError


def request(pubkey: str = "11" * 32) -> dict[str, str]:
    return {
        "pubkey": pubkey, "signing_pubkey": "02" + pubkey,
        "channels_json": json.dumps(["channel-a"]), "reply_agent_id": "seat-registration-test",
        "client_nonce": "nonce",
    }


def test_token_is_hashed_at_rest_and_can_only_be_reserved_once(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    token, record = store.mint("throwaway", "host-b", timedelta(hours=1))
    assert token.startswith(f"arbseat_{record.id}_")
    assert token not in (tmp_path / "tokens.sqlite3").read_bytes().decode("latin1")

    request_id = store.reserve(token=token, name="throwaway", host="host-b", request=request())
    assert store.request(request_id)["status"] == "pending"
    with pytest.raises(TokenError, match="invalid registration credential"):
        store.reserve(token=token, name="throwaway", host="host-b", request=request("22" * 32))


def test_wrong_name_or_host_does_not_consume_token(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    token, _ = store.mint("throwaway", "host-b", timedelta(hours=1))
    with pytest.raises(TokenError):
        store.reserve(token=token, name="other", host="host-b", request=request())
    with pytest.raises(TokenError):
        store.reserve(token=token, name="throwaway", host="other", request=request())
    assert store.reserve(token=token, name="throwaway", host="host-b", request=request())


def test_host_scoped_token_cannot_reserve_another_host(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    token, _ = store.mint("throwaway", "host-b", timedelta(hours=1))
    with pytest.raises(TokenError, match="invalid registration credential"):
        store.reserve(token=token, name="throwaway", host="site-a", request=request())


def test_token_mint_requires_a_host_scope(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    with pytest.raises(TokenError, match="host must be nonblank"):
        store.mint("throwaway", None, timedelta(hours=1))
    with pytest.raises(TokenError, match="host must be nonblank"):
        store.mint("throwaway", "", timedelta(hours=1))


def test_revoke_hides_no_secret_and_is_terminal(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    token, record = store.mint("throwaway", "host-b", timedelta(hours=1))
    listed = store.list_tokens()[0]
    assert not hasattr(listed, "token_hash")
    store.revoke(record.id)
    assert store.list_tokens()[0].status == "revoked"
    with pytest.raises(TokenError):
        store.reserve(token=token, name="throwaway", host="host-b", request=request())


def test_expired_token_fails_closed(tmp_path):
    store = RegistrationStore(tmp_path / "tokens.sqlite3")
    token, _ = store.mint("throwaway", "host-b", timedelta(microseconds=1))
    with pytest.raises(TokenError):
        store.reserve(token=token, name="throwaway", host="host-b", request=request())
