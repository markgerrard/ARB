from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from arb_email.mcp.door_tools import EmailTools


def build_local_server(settings, *, client, seat_id: str, audit_sink=None, now=None) -> FastMCP:
    if not settings.send_enabled:
        raise ValueError("ARB Email send disabled")
    tools = EmailTools(
        client,
        settings,
        require_scope=lambda _scope: None,
        actor=lambda: seat_id,
        now=now,
        audit_sink=audit_sink,
    )
    server = FastMCP("arb-email-local")
    server.add_tool(tools.email_send, name="email_send")
    return server

