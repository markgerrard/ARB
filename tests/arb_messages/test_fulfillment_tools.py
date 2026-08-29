import asyncio
import os

import psycopg
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from nacl.public import PrivateKey

from arb_messages.config import Settings
from arb_messages.keys import register_key
from arb_messages.mcp.door_tools import postgres_conn_factory
from arb_messages.mcp.fulfillment_tools import FulfillmentTools
from arb_messages.run import setup_schema
from arb_messages.store import enqueue

DSN = os.environ.get("ARB_MESSAGES_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="ARB_MESSAGES_TEST_DSN not set — Postgres-backed; run via scripts/arb-messages-gate, which forbids skips",
)


def settings(**overrides):
    base = dict(postgres_dsn="unused-in-these-tests", allowed_agents=frozenset({"agent-a"}),
                allowed_providers=frozenset({"cloudflare"}))
    base.update(overrides)
    return Settings(**base)


class FakeConnFactory:
    def __call__(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass


def _set_access_token(scopes):
    token = AccessToken(token="tok", client_id="codex-app", scopes=scopes)
    ctx = auth_context_var.set(AuthenticatedUser(token))
    return ctx


# Cold-Opus P1 (found during ARB Messages generalization review): every existing test stubs
# require_scope as a no-op, so nothing proves the REAL scope check
# (FulfillmentTools._default_require_scope, wired to the actual MCP auth context) genuinely
# rejects a caller lacking messages.fulfill. This is the direct regression guard, driving the
# real mechanism -- not an override -- against a real AuthenticatedUser/AccessToken in the
# actual contextvar the MCP SDK populates from a real request.
def test_default_require_scope_rejects_caller_without_messages_fulfill():
    tools = FulfillmentTools(FakeConnFactory(), settings())
    ctx = _set_access_token(["messages.request"])  # holds a DIFFERENT scope, not messages.fulfill
    try:
        with pytest.raises(PermissionError, match="messages.fulfill"):
            asyncio.run(tools.messages_claim_next())
    finally:
        auth_context_var.reset(ctx)


def test_default_require_scope_rejects_unauthenticated_caller():
    tools = FulfillmentTools(FakeConnFactory(), settings())
    # auth_context_var defaults to None -- no explicit set() call, simulating an unauthenticated
    # request reaching this tool directly.
    with pytest.raises(PermissionError, match="messages.fulfill"):
        asyncio.run(tools.messages_claim_next())


def test_default_require_scope_allows_caller_with_messages_fulfill():
    tools = FulfillmentTools(FakeConnFactory(), settings())
    ctx = _set_access_token(["messages.fulfill"])
    try:
        # Passes the real scope check; fails later for an unrelated reason (FakeConnFactory
        # doesn't behave like a real connection) -- proving the scope gate itself let it through
        # rather than raising PermissionError.
        with pytest.raises(Exception) as excinfo:
            asyncio.run(tools.messages_claim_next())
        assert not isinstance(excinfo.value, PermissionError)
    finally:
        auth_context_var.reset(ctx)


@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys RESTART IDENTITY")
    yield c
    c.close()


# cold-Opus P1 (found during the generalization review): the audit trail previously omitted
# capability -- the load-bearing "what was requested" content -- making the log much less
# useful for security review. Direct regression guard.
def test_claim_next_audits_the_freeform_capability(conn):
    enqueue(conn, "agent-a", "r1", "generic", "any", "add a redirect URI to Azure app Y", "approved", None)
    audits = []
    tools = FulfillmentTools(postgres_conn_factory(DSN), settings(),
                              require_scope=lambda s: None, audit_sink=audits.append)
    asyncio.run(tools.messages_claim_next())
    claimed_events = [a for a in audits if a["event"] == "claimed"]
    assert len(claimed_events) == 1
    assert claimed_events[0]["capability"] == "add a redirect URI to Azure app Y"


def test_deliver_result_does_not_audit_the_plaintext_result(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    audits = []
    tools = FulfillmentTools(postgres_conn_factory(DSN), settings(),
                              require_scope=lambda s: None, audit_sink=audits.append)
    claimed = asyncio.run(tools.messages_claim_next())
    asyncio.run(tools.messages_deliver_result(claimed["id"], claimed["claimed_at"], "the real secret value"))
    for event in audits:
        assert "the real secret value" not in str(event)


# agy-print P1 (found during the generalization review): provider_token_id was defined on the
# business-logic function but never exposed by the MCP tool wrapper. Direct regression guard
# that it's now threaded through correctly.
def test_deliver_result_threads_provider_token_id_through_to_the_row(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    tools = FulfillmentTools(postgres_conn_factory(DSN), settings(), require_scope=lambda s: None)
    claimed = asyncio.run(tools.messages_claim_next())
    result = asyncio.run(tools.messages_deliver_result(
        claimed["id"], claimed["claimed_at"], "secret", provider_token_id="cf-tok-123",
    ))
    assert result["status"] == "delivered"
    with conn.cursor() as cur:
        cur.execute("SELECT provider_token_id FROM arb_messages WHERE id = %s", (claimed["id"],))
        assert cur.fetchone()[0] == "cf-tok-123"
