from __future__ import annotations

import json
import logging


_audit_log = logging.getLogger("arb_email.audit")


def default_audit_sink(event: dict) -> None:
    _audit_log.info("arb_email_audit %s", json.dumps(event, sort_keys=True, default=str))

