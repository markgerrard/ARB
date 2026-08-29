from __future__ import annotations

import logging


log = logging.getLogger("arb_messages.door")


def register_messages_tools(server, env, *, client_factory=None) -> bool:
    required = (
        "ARB_MESSAGES_POSTGRES_DSN",
        "ARB_MESSAGES_ALLOWED_AGENTS",
        "ARB_MESSAGES_ALLOWED_PROVIDERS",
    )
    if any(not env.get(key) for key in required) or env.get("ARB_MESSAGES_ENABLED", "1") != "1":
        return False
    try:
        from arb_messages.audit import default_audit_sink
        from arb_messages.config import load_settings
        from arb_messages.mcp.door_tools import MessagesTools, postgres_conn_factory

        settings = load_settings(env)
        if not settings.messages_enabled:
            return False
        conn_factory = client_factory() if client_factory else postgres_conn_factory(settings.postgres_dsn)
        tools = MessagesTools(conn_factory, settings, audit_sink=default_audit_sink)
    except Exception:
        log.exception("ARB Messages door tools not registered (config/back-end error); memory door unaffected")
        return False

    async def messages_request(request_id: str, capability: str, provider: str) -> dict:
        """Request a privileged action for the authenticated agent, described freeform.

        Enqueues the request and returns immediately: pending means not yet claimed, claimed means
        a fulfiller is working on it. Retrieve the final result with messages_poll.
        A delivered result carries body_b64: base64-encoded NaCl SealedBox ciphertext sealed
        to the agent's registered public key; base64-decode it, then unseal it.
        Polling again after delivery returns already_delivered with the same body_b64 -- delivery
        is idempotent for the requesting agent. A retried already-fulfilled request returns the
        sealed result directly, so a lost response is recoverable.
        """
        return await tools.messages_request(request_id, capability, provider)

    async def messages_register_key(pubkey: str) -> dict:
        """Register the authenticated agent's public key for sealed results."""
        return await tools.messages_register_key(pubkey)

    async def messages_poll(request_id: str) -> dict:
        """Poll a previously requested result for the authenticated agent.

        A delivered result carries body_b64: base64-encoded NaCl SealedBox ciphertext sealed
        to the agent's registered public key; base64-decode it, then unseal it.
        Polling again after delivery returns already_delivered with the same body_b64 -- delivery
        is idempotent for the requesting agent; a lost response is recoverable by polling again.
        """
        return await tools.messages_poll(request_id)

    server.add_tool(messages_request, name="messages_request")
    server.add_tool(messages_register_key, name="messages_register_key")
    server.add_tool(messages_poll, name="messages_poll")
    return True
