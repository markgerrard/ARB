from __future__ import annotations

import os
import socket


def run_local_mcp() -> None:
    from arb_email.audit import default_audit_sink
    from arb_email.client import EmailClient
    from arb_email.config import load_settings
    from arb_email.mcp.local_server import build_local_server

    settings = load_settings(os.environ)
    if not settings.send_enabled:
        raise ValueError("ARB Email send disabled")
    seat_id = os.environ.get("ARB_EMAIL_SEAT_ID") or f"seat-{socket.gethostname()}"
    client = EmailClient(settings, audit_sink=default_audit_sink)
    server = build_local_server(
        settings,
        client=client,
        seat_id=seat_id,
        audit_sink=default_audit_sink,
    )
    server.run(transport="stdio")

