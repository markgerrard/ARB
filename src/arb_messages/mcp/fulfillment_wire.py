from __future__ import annotations

import logging


log = logging.getLogger("arb_messages.fulfillment_wire")


def register_fulfillment_tools(server, env, *, client_factory=None) -> bool:
    required = (
        "ARB_MESSAGES_POSTGRES_DSN",
        "ARB_MESSAGES_ALLOWED_AGENTS",
    )
    if any(not env.get(key) for key in required) or env.get("ARB_MESSAGES_ENABLED", "1") != "1":
        return False
    try:
        from arb_messages.audit import default_audit_sink
        from arb_messages.config import load_settings
        from arb_messages.mcp.fulfillment_tools import FulfillmentTools, postgres_conn_factory

        settings = load_settings(env)
        if not settings.messages_enabled:
            return False
        conn_factory = client_factory() if client_factory else postgres_conn_factory(settings.postgres_dsn)
        tools = FulfillmentTools(conn_factory, settings, audit_sink=default_audit_sink)
    except Exception:
        log.exception("ARB Messages fulfillment tools not registered (config/back-end error); memory door unaffected")
        return False

    # These four tools are reachable by ANY connected agent holding messages.fulfill, which is
    # now a default-granted scope (every claude.ai/ChatGPT connection gets it, not just Codex
    # App -- see server.py's ClientRegistrationOptions comment). The descriptions below are the
    # only guardrail telling an ordinary agent session NOT to call them: there is no technical
    # scope restriction narrowing this to Codex App specifically. Passed via add_tool's
    # `description` param (not a docstring -- an f-string can't BE a docstring, since it compiles
    # to a JoinedStr AST node rather than the Constant node Python's docstring recognition needs).
    operator_notice = (
        "RESTRICTED TO CODEX APP ONLY. This tool exists to let Codex App fulfill ARB Messages "
        "requests on behalf of other agents (e.g. a remote Claude Code session). If you are "
        "ChatGPT, Claude.ai, or any other agent that is not specifically acting as the Codex App "
        "operator for this system, do NOT call this tool -- it can claim, answer, or deny "
        "another agent's pending request. "
    )

    async def messages_claim_next() -> dict:
        return await tools.messages_claim_next()

    async def messages_deliver_result(row_id: int, claimed_at: str, result_text: str,
                                       provider_token_id: str | None = None) -> dict:
        return await tools.messages_deliver_result(row_id, claimed_at, result_text, provider_token_id)

    async def messages_deny(row_id: int, claimed_at: str, reason: str) -> dict:
        return await tools.messages_deny(row_id, claimed_at, reason)

    async def messages_fail(row_id: int, claimed_at: str, reason: str) -> dict:
        return await tools.messages_fail(row_id, claimed_at, reason)

    server.add_tool(messages_claim_next, name="messages_claim_next",
                     description=operator_notice + "Claim the next pending ARB Messages request for fulfillment.")
    server.add_tool(messages_deliver_result, name="messages_deliver_result",
                     description=operator_notice + "Deliver a fulfilled result, sealed for the requesting agent.")
    server.add_tool(messages_deny, name="messages_deny",
                     description=operator_notice + "Deny a claimed request.")
    server.add_tool(messages_fail, name="messages_fail",
                     description=operator_notice + "Mark a claimed request as failed.")
    return True
