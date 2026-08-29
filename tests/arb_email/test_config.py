import pytest

from arb_email.config import load_settings


BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok"}


def test_defaults_explicit_address_allowlist():
    s = load_settings(BASE)
    assert s.sender == "arb@example.com" and s.stream == "arb"
    assert s.default_to == "arb@example.com"
    assert list(s.to_allowlist) == ["arb@example.com"]


def test_missing_token_fails():
    with pytest.raises(ValueError):
        load_settings({})


def test_default_to_must_be_in_allowlist():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_EMAIL_TO_ALLOWLIST": "@other.com"})


def test_empty_allowlist_after_drop_fails():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_EMAIL_TO_ALLOWLIST": " , "})


def test_body_cap_default_bytes():
    assert load_settings(BASE).body_max == 102400
