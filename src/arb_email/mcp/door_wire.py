from __future__ import annotations

import logging


log = logging.getLogger("arb_email.door")


def register_email_tool(server, env, *, client_factory=None) -> bool:
    if not env.get("ARB_EMAIL_POSTMARK_TOKEN") or env.get("ARB_EMAIL_SEND_ENABLED") != "1":
        return False
    try:
        from arb_email.audit import default_audit_sink
        from arb_email.client import EmailClient
        from arb_email.config import load_settings
        from arb_email.mcp.door_tools import EmailTools

        settings = load_settings(env)
        if not settings.send_enabled:
            return False
        client = client_factory() if client_factory else EmailClient(settings, audit_sink=default_audit_sink)
        tools = EmailTools(client, settings, audit_sink=default_audit_sink)
    except Exception:
        log.exception("ARB Email door tool not registered (config/back-end error); memory door unaffected")
        return False

    async def email_send(subject: str, html_body=None, text_body=None, to=None) -> dict:
        """Send email from fixed arb@example.com on the fixed arb stream.

        Recipients must be allowlisted. The subject and body are model-supplied; From,
        MessageStream, recipient policy, caps, and audit are server-enforced.
        """
        return await tools.email_send(subject, html_body=html_body, text_body=text_body, to=to)

    server.add_tool(email_send, name="email_send")
    return True
