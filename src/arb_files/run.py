from __future__ import annotations

import os
import socket


def run_local_mcp() -> None:
    from arb_files.audit import default_audit_sink
    from arb_files.config import load_settings
    from arb_files.mcp.local_server import build_local_server
    from arb_files.store import FilesStore

    settings = load_settings(os.environ)
    seat_id = os.environ.get("ARB_FILES_SEAT_ID") or f"seat-{socket.gethostname()}"
    store = FilesStore(settings, audit_sink=default_audit_sink)
    server = build_local_server(settings, store=store, seat_id=seat_id)
    server.run(transport="stdio")
