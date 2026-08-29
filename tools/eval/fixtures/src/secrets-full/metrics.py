"""Metrics + webhooks. Identifiers are hashed; webhook secrets are verified, never logged."""
import hashlib
import hmac
import logging

logger = logging.getLogger("metrics")


def record_active(user_id):
    fingerprint = hashlib.sha256(str(user_id).encode()).hexdigest()[:12]
    logger.info("active user fp=%s", fingerprint)
    return fingerprint


def record_flag(name, enabled):
    logger.info("feature flag %s enabled=%s", name, bool(enabled))
    return enabled


def verify_webhook(body, signature, signing_secret):
    expected = hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(expected, signature)
    logger.info("webhook signature valid=%s", valid)
    return valid
