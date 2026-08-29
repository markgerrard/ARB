"""Admin / diagnostics endpoints. Config introspection is a classic whole-object leak channel."""
import logging

logger = logging.getLogger("admin")

_SECRET_KEYS = {"api_token", "db_password", "db_dsn", "signing_secret"}


def dump_config(cfg):
    logger.debug("effective config: %r", cfg.as_dict())
    return cfg.as_dict()


def dump_config_masked(cfg):
    safe = {k: ("***" if k in _SECRET_KEYS else v) for k, v in cfg.as_dict().items()}
    logger.debug("effective config: %r", safe)
    return safe


def dump_public_settings(cfg):
    public = {"region": cfg.region, "app_name": cfg.app_name}
    logger.info("public settings: %r", public)
    return public


def config_summary(cfg):
    logger.info("config has %d keys", len(cfg.as_dict()))
    return len(cfg.as_dict())
