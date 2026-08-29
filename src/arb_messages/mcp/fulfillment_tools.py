from __future__ import annotations

import logging

import psycopg

from arb_messages import fulfillment


log = logging.getLogger("arb_messages.mcp.fulfillment_tools")


class FulfillmentTools:
    """Operator-facing tools for whoever fulfills ARB Messages requests (Codex app, human-
    attended). Distinct trust boundary from MessagesTools (the agent-facing door): these tools
    are not scoped to a single agent's own identity -- claim_next sees the whole queue, and
    deliver_result/deny/fail act on whatever row_id the caller was handed by claim_next. The
    caller must hold the messages.fulfill scope, never messages.request's scope alone.
    """

    def __init__(self, store_conn_factory, settings, *, require_scope=None, audit_sink=None):
        self.store_conn_factory = store_conn_factory
        self.s = settings
        self._require_scope = require_scope or self._default_require_scope
        self.audit_sink = audit_sink

    def _default_require_scope(self, scope: str) -> None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or scope not in (getattr(token, "scopes", None) or []):
            raise PermissionError(f"{scope} scope required")

    def _audit(self, event: str, **fields) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink({"event": event, **fields})
        except Exception:
            log.exception("arb messages fulfillment audit failed")

    def _conn(self):
        return self.store_conn_factory()

    async def messages_claim_next(self) -> dict:
        self._require_scope("messages.fulfill")
        with self._conn() as conn:
            claimed = fulfillment.claim_next(conn, self.s.lease_seconds)
        if claimed is None:
            return {"status": "empty"}
        # cold-Opus P1 (found during the generalization review): the audit trail previously
        # omitted `capability` -- the load-bearing "what was actually requested" content -- which
        # made the audit log much less useful for security review (it could show a claim
        # happened, but not what it was for). capability is freeform text the requesting agent
        # supplied, not a secret, so it's safe to audit; the delivered RESULT is deliberately
        # never audited (see messages_deliver_result below) since that's the sealed secret's
        # plaintext.
        self._audit("claimed", row_id=claimed["id"], agent_id=claimed["agent_id"],
                     request_id=claimed["request_id"], capability=claimed["capability"])
        # claimed_at must round-trip through the MCP wire protocol (JSON) and back into
        # messages_deliver_result/messages_deny/messages_fail's own claimed_at parameter --
        # isoformat is the one representation both JSON and psycopg's timestamptz comparison
        # accept directly (verified: a stored timestamptz column matches an isoformat string
        # parameter in a WHERE clause without any cast).
        claimed["claimed_at"] = claimed["claimed_at"].isoformat()
        return {"status": "claimed", **claimed}

    async def messages_deliver_result(self, row_id: int, claimed_at: str, result_text: str,
                                       provider_token_id: str | None = None) -> dict:
        # agy-print P1 (found during the generalization review): provider_token_id was defined on
        # fulfillment.deliver_result but never exposed here, silently defaulting to None on every
        # call. Currently harmless (nothing in the generalized codebase writes provider_token_id
        # before this point, so there's nothing to clear), but exposing it lets an operator record
        # a revocable credential identifier for audit/tracking purposes when a fulfilled request
        # happens to produce one (e.g. Codex minted a real Cloudflare token using its own access).
        self._require_scope("messages.fulfill")
        from arb_messages import keys as keys_module

        with self._conn() as conn:
            ok = fulfillment.deliver_result(conn, keys_module, row_id, claimed_at, result_text,
                                             provider_token_id=provider_token_id)
        # Deliberately does NOT audit result_text -- that's the sealed secret's plaintext, and
        # putting it in an audit log would defeat the point of sealing it for the recipient only.
        self._audit("delivered" if ok else "deliver_failed", row_id=row_id)
        return {"status": "delivered" if ok else "failed"}

    async def messages_deny(self, row_id: int, claimed_at: str, reason: str) -> dict:
        self._require_scope("messages.fulfill")
        with self._conn() as conn:
            ok = fulfillment.deny(conn, row_id, claimed_at, reason)
        self._audit("denied" if ok else "deny_failed", row_id=row_id, reason=reason)
        return {"status": "denied" if ok else "failed"}

    async def messages_fail(self, row_id: int, claimed_at: str, reason: str) -> dict:
        self._require_scope("messages.fulfill")
        with self._conn() as conn:
            ok = fulfillment.fail(conn, row_id, claimed_at, reason)
        self._audit("failed" if ok else "fail_failed", row_id=row_id, reason=reason)
        return {"status": "failed" if ok else "failed_to_fail"}


def postgres_conn_factory(dsn: str):
    return lambda: psycopg.connect(dsn, autocommit=True)
