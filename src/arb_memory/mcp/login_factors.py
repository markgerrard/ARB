from __future__ import annotations

import hmac

import pyotp


def verify_passphrase(supplied: str, secret: str) -> bool:
    if not supplied or not secret:
        return False
    return hmac.compare_digest(supplied, secret)


def verify_totp(code: str, secret: str) -> bool:
    if not code or not secret:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def verify_two_factor(passphrase: str, totp: str, *, settings) -> bool:
    return verify_passphrase(passphrase, settings.login_secret) and verify_totp(
        totp, settings.totp_secret
    )
