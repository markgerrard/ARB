from __future__ import annotations

import base64
import logging

import psycopg

from arb_messages import keys, store


log = logging.getLogger("arb_messages.mcp.door_tools")


class MessagesTools:
    def __init__(self, store_conn_factory, settings, *, require_scope=None, actor=None,
                 connector_host=None, audit_sink=None):
        self.store_conn_factory = store_conn_factory
        self.s = settings
        self._require_scope = require_scope or self._default_require_scope
        self._actor_source = actor or self._default_actor_source
        self._connector_host_source = connector_host or self._default_connector_host_source
        self.audit_sink = audit_sink

    def _default_require_scope(self, scope: str) -> None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or scope not in (getattr(token, "scopes", None) or []):
            raise PermissionError(f"{scope} scope required")

    def _default_actor_source(self) -> str | None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        return getattr(token, "client_id", None) if token is not None else None

    def _actor(self) -> str:
        actor = self._actor_source()
        if actor is None:
            raise PermissionError("authenticated client_id required")
        return actor.lower()

    def _default_connector_host_source(self) -> str | None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None:
            return None
        return (getattr(token, "claims", None) or {}).get("connector_host")

    def _connector_host(self) -> str:
        # Found for ARB Messages' generalization: client_id (used by _actor() for delivery/key
        # scoping, unchanged) is a fresh value minted per OAuth registration -- an allowlist keyed
        # on it would churn on every re-authorization. connector_host is a stable, cryptographically
        # backed category (see redirect_policy.connector_host_for_redirect_uris) derived from the
        # client's registered redirect_uri, used ONLY for the coarse "is this kind of connector
        # allowed to use ARB Messages at all" gate -- never for delivery/key identity, since
        # collapsing that to a shared category would let concurrent sessions of the same connector
        # (which register distinct keys) interfere with each other's registered key/delivery.
        host = self._connector_host_source()
        if host is None:
            raise PermissionError("recognized connector required")
        return host

    def _deny(self, reason: str, **fields) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink({"event": "denied", "reason": reason, **fields})
        except Exception:
            log.exception("arb messages denial audit failed")

    def _conn(self):
        return self.store_conn_factory()

    def _encode_delivery(self, result: dict) -> dict:
        # The sealed body is raw ciphertext, not valid UTF-8 -- returning it straight through
        # crashes MCP's JSON-RPC serialization AFTER read_and_mark_delivered() has already
        # committed delivered_at, permanently losing the payload (every later poll hits the
        # already_delivered branch, body gone). Base64-encode at the door, the JSON-safe boundary.
        # The *_b64 name follows arb_files content_b64 / arb_memory content_bytes_b64.
        if "body" not in result:
            return result
        result = dict(result)
        body = result.pop("body")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError(f"sealed body must be bytes-like, got {type(body).__name__}")
        result["body_b64"] = base64.b64encode(bytes(body)).decode("ascii")
        return result

    async def messages_request(self, request_id: str, capability: str, provider: str) -> dict:
        # capability is a freeform description of what's needed -- "mint a Cloudflare DNS-edit
        # token for zone X", "add a redirect URI to Azure AD app Y", anything a human-attended
        # fulfiller (Codex app) can act on. There is no structural check ON THE FREEFORM TEXT
        # itself: judgment about whether to fulfill a given request lives with whoever answers
        # it, not with a fixed capability allowlist.
        #
        # provider IS structurally checked, though -- found by all three reviewers of the
        # generalization pass (agy-print, cold-Opus, pi-GLM independently): without it, an
        # allowlisted agent could enqueue a request for ANY category of privileged action, with
        # Codex's judgment as the sole backstop. provider is a coarse-grained category (e.g.
        # "cloudflare", "azure", "digitalocean") an operator can allowlist without coupling to
        # any one provider's specific permission model -- it bounds which CATEGORIES of request
        # this deployment will broker at all, denied before ever reaching the queue.
        try:
            self._require_scope("messages.request")
        except PermissionError:
            self._deny("missing scope", request_id=request_id)
            raise
        agent_id = self._actor()
        try:
            connector_host = self._connector_host()
        except PermissionError:
            self._deny("connector not allowlisted", actor=agent_id, request_id=request_id)
            raise ValueError("connector not allowlisted")
        if connector_host not in self.s.allowed_agents:
            self._deny("connector not allowlisted", actor=agent_id, request_id=request_id, connector_host=connector_host)
            raise ValueError("connector not allowlisted")
        if provider not in self.s.allowed_providers:
            self._deny("provider not allowlisted", actor=agent_id, request_id=request_id, provider=provider)
            raise ValueError("provider not allowlisted")

        with self._conn() as conn:
            if keys.live_key(conn, agent_id) is None:
                self._deny("no live key", actor=agent_id, request_id=request_id)
                raise ValueError("no live key")
            store.enqueue(conn, agent_id, request_id, "generic", provider, capability, "approved", None)
            # Fulfillment is human-attended (minutes, not seconds), so enqueue and return.
            # The immediate check lets retries of an already-fulfilled request_id recover
            # the sealed result instead of seeing a misleading pending response.
            result = store.read_and_mark_delivered(conn, agent_id, request_id)
            if result.get("status") in ("delivered", "already_delivered", "failed", "denied"):
                return self._encode_delivery(result)
        # Pass through the last observed status so callers can distinguish pending from claimed.
        return {"status": result.get("status", "pending"), "request_id": request_id}

    async def messages_register_key(self, pubkey: str) -> dict:
        try:
            self._require_scope("messages.request")
        except PermissionError:
            self._deny("missing scope")
            raise
        agent_id = self._actor()
        raw_pubkey = base64.b64decode(pubkey)
        with self._conn() as conn:
            keys.register_key(conn, agent_id, raw_pubkey)
        return {"status": "registered"}

    async def messages_poll(self, request_id: str) -> dict:
        try:
            self._require_scope("messages.request")
        except PermissionError:
            self._deny("missing scope", request_id=request_id)
            raise
        agent_id = self._actor()
        with self._conn() as conn:
            return self._encode_delivery(store.read_and_mark_delivered(conn, agent_id, request_id))


def postgres_conn_factory(dsn: str):
    return lambda: psycopg.connect(dsn, autocommit=True)
