"""Service configuration. Holds both secrets (token, db password, DSN) and non-secret settings."""
import logging
import os

logger = logging.getLogger("config")


class Config:
    def __init__(self):
        self.api_token = os.environ.get("API_TOKEN", "")
        self.db_password = os.environ.get("DB_PASSWORD", "")
        # DSN embeds the password: postgres://user:PASSWORD@host:5432/billing
        self.db_dsn = os.environ.get("DB_DSN", "")
        self.service_url = os.environ.get("SERVICE_URL", "")
        self.signing_secret = os.environ.get("SIGNING_SECRET", "")
        self.region = os.environ.get("REGION", "us-east-1")  # non-secret
        self.app_name = "billing"                            # non-secret

    def load(self, source):
        logger.info("config loaded from source=%s", source)
        return self

    def as_dict(self):
        return {
            "api_token": self.api_token,
            "db_password": self.db_password,
            "db_dsn": self.db_dsn,
            "service_url": self.service_url,
            "signing_secret": self.signing_secret,
            "region": self.region,
            "app_name": self.app_name,
        }
