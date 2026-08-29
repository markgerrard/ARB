"""End-to-end integration test for ARB Messages' generic relay.

Wires together ARB Messages' own code as two genuinely independent components -- the MCP door
(MessagesTools, as a remote coding agent would call it) and the operator-facing fulfillment
tools (FulfillmentTools, as Codex app would call it) -- coordinating ONLY through a real
Postgres queue, exactly matching the real deployment topology (both run inside the same MCP
server process, but as separate trust boundaries with separate OAuth scopes). Nothing here is
Cloudflare-specific: the whole point of the generic relay is that the actual privileged action
(Cloudflare, DigitalOcean, Azure AD, anything) happens entirely outside this codebase, performed
by whoever holds messages.fulfill scope -- this test proves the surrounding queue/fencing/
sealing machinery correctly relays a freeform request end to end regardless of what it's for.

Run directly: PYTHONPATH=src ARB_MESSAGES_TEST_DSN=... .venv/bin/python -m pytest
tests/arb_messages/test_e2e_full_pipeline.py -v -s
"""
from __future__ import annotations

import asyncio
import base64
import os
import threading
import time

import psycopg
import pytest
from nacl.public import PrivateKey

from arb_messages.config import Settings
from arb_messages.keys import unseal
from arb_messages.mcp.door_tools import MessagesTools, postgres_conn_factory
from arb_messages.mcp.fulfillment_tools import FulfillmentTools
from arb_messages.run import setup_schema


DSN = os.environ.get("ARB_MESSAGES_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="ARB_MESSAGES_TEST_DSN not set — Postgres-backed; run via scripts/arb-messages-gate, which forbids skips",
)


@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys RESTART IDENTITY")
    yield c
    c.close()


def test_full_pipeline_request_to_delivery_via_independent_door_and_fulfiller(conn):
    # 1. An agent generates a real NaCl keypair and registers the public half through the
    #    REAL door tool (mcp/door_tools.py), exactly as a real agent's first-run bootstrap
    #    would, with a simulated (not mocked-away) authenticated identity -- `actor`/
    #    `require_scope` stand in for a real MCP OAuth context, the one piece that requires a
    #    live MCP client/server round-trip to exercise for real.
    agent_sk = PrivateKey.generate()
    settings = Settings(
        postgres_dsn=DSN,
        allowed_agents=frozenset({"claude.ai"}),  # connector-host category, not a client_id
        allowed_providers=frozenset({"cloudflare", "azure"}),
        # pi-GLM P2 (found during the generalization review): a 2s lease is timing-sensitive
        # for the threaded fulfiller polling at 0.05s intervals -- bumped to remove the flake risk.
        lease_seconds=10,
        messages_enabled=True,
    )
    audits = []
    # A fresh connection per call, exactly like production's postgres_conn_factory -- door_tools
    # uses `with self._conn() as conn:`, and psycopg's Connection.__exit__ commits+closes, so a
    # shared/reused connection object would be closed after the first call.
    door = MessagesTools(
        postgres_conn_factory(DSN), settings,
        require_scope=lambda scope: None,
        actor=lambda: "agent-e2e",
        connector_host=lambda: "claude.ai",
        audit_sink=audits.append,
    )
    fulfiller = FulfillmentTools(
        postgres_conn_factory(DSN), settings,
        require_scope=lambda scope: None,  # stands in for a real messages.fulfill-scoped caller
        audit_sink=audits.append,
    )
    asyncio.run(door.messages_register_key(base64.b64encode(bytes(agent_sk.public_key)).decode()))
    with conn.cursor() as cur:
        cur.execute("SELECT agent_id, revoked_at FROM arb_agent_keys WHERE agent_id = 'agent-e2e'")
        assert cur.fetchone() == ("agent-e2e", None)

    # 2. Start a genuinely independent thread standing in for Codex app polling for work --
    #    coordinating with the door ONLY through Postgres, exactly like the real deployment
    #    (Codex app calls MCP tools; it never shares in-process state with the door).
    delivered_results = {"req-1": b"cf-secret-for-zone_dns_edit", "req-2": b"cf-secret-for-a-second-request"}
    stop = threading.Event()
    def run_fulfiller():
        while not stop.is_set():
            claimed = asyncio.run(fulfiller.messages_claim_next())
            if claimed["status"] != "claimed":
                time.sleep(0.05)
                continue
            # Codex's own judgment/action happens here in reality (Cloudflare, DO, Azure AD,
            # anything) -- entirely outside this codebase. This test simulates a successful
            # fulfillment using a canned result keyed by request_id.
            result_text = delivered_results.get(claimed["request_id"], f"fulfilled: {claimed['capability']}")
            asyncio.run(fulfiller.messages_deliver_result(claimed["id"], claimed["claimed_at"], result_text))
    fulfiller_thread = threading.Thread(target=run_fulfiller, daemon=True)
    fulfiller_thread.start()

    def poll_until_delivered(request_id):
        for _ in range(30):
            polled = asyncio.run(door.messages_poll(request_id))
            if polled["status"] in ("delivered", "already_delivered"):
                return polled
            assert polled["status"] in ("pending", "claimed")
            time.sleep(0.5)
        pytest.fail(f"{request_id} was not delivered within the bounded poll window")

    try:
        # 3. The agent calls the REAL door tool exactly as an MCP client would -- this enqueues
        #    through the real store.enqueue and returns immediately. The independent fulfiller
        #    thread claims, fulfills (simulated), and delivers through Postgres; the agent obtains
        #    the final result through messages_poll.
        request_result = asyncio.run(door.messages_request(
            "req-1", "mint a Cloudflare DNS-edit token for zone z1", "cloudflare",
        ))
        assert request_result["status"] in ("pending", "claimed")
        assert request_result["request_id"] == "req-1"
        result = poll_until_delivered("req-1")

        # 4. Prove the delivered payload is genuinely sealed for THIS agent's real private key --
        #    not a stub, not plaintext leaking through the store.
        assert "body" not in result
        sealed = base64.b64decode(result["body_b64"])
        plaintext = unseal(sealed, bytes(agent_sk))
        assert plaintext == delivered_results["req-1"]

        # 5. Idempotent re-delivery: polling the SAME request_id again re-delivers the
        #    sealed body to the same agent. The status still distinguishes first receipt
        #    (`delivered`) from subsequent recoverable polls (`already_delivered`).
        second_poll = asyncio.run(door.messages_poll("req-1"))
        assert second_poll["status"] == "already_delivered"
        assert "body" not in second_poll
        second_plaintext = unseal(base64.b64decode(second_poll["body_b64"]), bytes(agent_sk))
        assert second_plaintext == plaintext

        # 6. Client-retry idempotency: re-requesting the SAME (agent_id, request_id) must land
        #    on the same row, not enqueue a duplicate.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM arb_messages WHERE agent_id = 'agent-e2e' AND request_id = 'req-1'"
            )
            assert cur.fetchone()[0] == 1

        # 7. A second, independent, genuinely different (non-Cloudflare-shaped) request enqueues
        #    through the same live door+fulfiller pipeline and obtains its result through polling,
        #    proving the relay is generic, not tied to any one provider's request shape.
        request_result2 = asyncio.run(door.messages_request(
            "req-2", "Add redirect URI https://x/callback to Azure AD app registration Y", "azure",
        ))
        assert request_result2["status"] in ("pending", "claimed")
        assert request_result2["request_id"] == "req-2"
        result2 = poll_until_delivered("req-2")
        assert result2["status"] in ("delivered", "already_delivered")
        assert "body" not in result2
        plaintext2 = unseal(base64.b64decode(result2["body_b64"]), bytes(agent_sk))
        assert plaintext2 == delivered_results["req-2"]
        assert plaintext2 != plaintext  # distinct fulfillment, distinct result

        # 8. Denial path through the real pipeline: a connector not in the allowlist is denied by
        #    the door itself (pre-check), never reaching the queue or the fulfiller at all.
        stranger = MessagesTools(
            postgres_conn_factory(DSN), settings,
            require_scope=lambda scope: None,
            actor=lambda: "agent-not-allowed",
            connector_host=lambda: "some-other-connector",
            audit_sink=audits.append,
        )
        with pytest.raises(ValueError):
            asyncio.run(stranger.messages_request("req-3", "anything", "cloudflare"))
        assert any(a["event"] == "denied" and a.get("reason") == "connector not allowlisted" for a in audits)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM arb_messages WHERE request_id = 'req-3'")
            assert cur.fetchone()[0] == 0  # denied pre-check must never enqueue

        # 9. The structural provider allowlist (added after the generalization review) denies a
        #    request for a category this deployment isn't configured to broker, through the same
        #    real pipeline -- never reaching the queue, even for an otherwise-allowlisted agent.
        with pytest.raises(ValueError):
            asyncio.run(door.messages_request("req-4", "anything", "not-a-broked-provider"))
        assert any(a["event"] == "denied" and a.get("reason") == "provider not allowlisted" for a in audits)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM arb_messages WHERE request_id = 'req-4'")
            assert cur.fetchone()[0] == 0
    finally:
        stop.set()
        fulfiller_thread.join(timeout=5)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "-s"]))
