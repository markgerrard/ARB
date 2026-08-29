import inspect

import pyotp

from arb_memory.mcp.config import Settings
from arb_memory.mcp.login_factors import (
    verify_passphrase,
    verify_totp,
    verify_two_factor,
)


def _settings(secret, totp_secret):
    return Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret=secret,
        totp_secret=totp_secret,
    )


def test_two_factor_requires_correct_passphrase_and_totp():
    totp_secret = pyotp.random_base32()
    code = pyotp.TOTP(totp_secret).now()
    settings = _settings("passphrase", totp_secret)

    assert verify_two_factor("passphrase", code, settings=settings) is True
    assert verify_two_factor("passphrase", "000000", settings=settings) is False
    assert verify_two_factor("passphrase", "", settings=settings) is False
    assert verify_two_factor("wrong", code, settings=settings) is False


def test_verify_totp_accepts_current_code():
    secret = pyotp.random_base32()
    assert verify_totp(pyotp.TOTP(secret).now(), secret) is True


def test_verify_passphrase_uses_constant_time_compare():
    source = inspect.getsource(verify_passphrase)
    assert "hmac.compare_digest" in source
    assert "==" not in source
    assert verify_passphrase("same", "same") is True
    assert verify_passphrase("same", "different") is False
