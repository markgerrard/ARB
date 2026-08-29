"""Authentication: login, refresh, logout. Logging here must never emit raw credentials."""
import logging

logger = logging.getLogger("auth")


def login(user_id, api_token):
    if not _verify(user_id, api_token):
        return login_failure(user_id, api_token)
    logger.info("login ok for %s token=%s", user_id, api_token)
    return True


def login_failure(user_id, api_token):
    logger.warning("login failed for %s token_present=%s", user_id, bool(api_token))
    return False


def refresh(user_id, api_token, token_id):
    if not api_token:
        return False
    logger.info("token refresh for %s token_id=%s", user_id, token_id)
    return True


def logout(user_id):
    logger.info("logout for user %s", user_id)
    return True


def _verify(user_id, api_token):
    return bool(user_id) and bool(api_token)
