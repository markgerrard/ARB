import pytest, psycopg, os, time
from nacl.public import PrivateKey
from arb_messages.run import setup_schema
from arb_messages.store import enqueue, claim, read_and_mark_delivered
from arb_messages.keys import register_key, unseal
from arb_messages.fulfillment import claim_next, deliver_result, deny, fail
import arb_messages.keys as keys_mod

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

@pytest.fixture
def agent_key(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    return sk

def test_claim_next_returns_none_when_queue_empty(conn):
    assert claim_next(conn, lease_seconds=300) is None

def test_claim_next_returns_freeform_capability_untouched(conn):
    # capability is a freeform description of what's needed (e.g. an Azure AD app-registration
    # change), not a fixed Cloudflare-shaped enum -- claim_next must pass it through verbatim.
    freeform = "Add redirect URI https://x/callback and delegated perms openid,profile,email to app registration Y"
    enqueue(conn, "agent-a", "r1", "azure_ad_config", "azure", freeform, "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    assert claimed["capability"] == freeform
    assert claimed["provider"] == "azure"
    assert claimed["agent_id"] == "agent-a"
    assert claimed["request_id"] == "r1"
    assert "id" in claimed and "claimed_at" in claimed

def test_claim_next_never_claims_the_same_row_twice_concurrently(conn):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    a = claim_next(conn, lease_seconds=300)
    conn2 = psycopg.connect(DSN, autocommit=True)
    try:
        b = claim_next(conn2, lease_seconds=300)
    finally:
        conn2.close()
    assert a is not None
    assert b is None  # already claimed by the first caller

def test_deliver_result_seals_for_the_rows_own_agent_never_a_caller_supplied_one(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = deliver_result(conn, keys_mod, claimed["id"], claimed["claimed_at"], b"the real secret")
    assert ok is True
    result = read_and_mark_delivered(conn, "agent-a", "r1")
    assert result["status"] == "delivered"
    plaintext = unseal(bytes(result["body"]), bytes(agent_key))
    assert plaintext == b"the real secret"

def test_deliver_result_accepts_str_plaintext(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = deliver_result(conn, keys_mod, claimed["id"], claimed["claimed_at"], "a string secret")
    assert ok is True
    result = read_and_mark_delivered(conn, "agent-a", "r1")
    assert unseal(bytes(result["body"]), bytes(agent_key)) == b"a string secret"

def test_deliver_result_with_no_provider_token_id_is_never_swept_for_revocation(conn, agent_key):
    # Generic (non-Cloudflare) fulfillment produces no revocable token -- provider_token_id must
    # be allowed to stay NULL, and the existing sweep (which requires provider_token_id IS NOT
    # NULL to even consider a row) must correctly leave it alone forever.
    enqueue(conn, "agent-a", "r1", "azure_ad_config", "azure", "add a redirect URI", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = deliver_result(conn, keys_mod, claimed["id"], claimed["claimed_at"], b"redirect URI added: https://x/callback")
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT provider_token_id, status FROM arb_messages WHERE id = %s", (claimed["id"],))
        assert cur.fetchone() == (None, "done")

def test_deliver_result_fenced_out_returns_false_and_does_not_seal_for_a_stale_claim(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    stale = claim_next(conn, lease_seconds=0)
    time.sleep(0.05)
    fresh = claim_next(conn, lease_seconds=0)  # reclaims the same row -- stale.claimed_at no longer matches
    assert fresh is not None
    ok = deliver_result(conn, keys_mod, stale["id"], stale["claimed_at"], b"stale attempt's secret")
    assert ok is False
    # the row must be untouched by the stale attempt -- still claimed under the FRESH claimed_at
    with conn.cursor() as cur:
        cur.execute("SELECT status, claimed_at FROM arb_messages WHERE id = %s", (stale["id"],))
        status, claimed_at = cur.fetchone()
        assert status == "claimed"
        assert claimed_at == fresh["claimed_at"]

def test_deliver_result_no_live_key_returns_false(conn):
    # agent has never registered a key -- there's nothing to seal for, must fail closed rather
    # than write plaintext or silently succeed with an unusable result.
    enqueue(conn, "agent-no-key", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = deliver_result(conn, keys_mod, claimed["id"], claimed["claimed_at"], b"secret")
    assert ok is False
    with conn.cursor() as cur:
        cur.execute("SELECT status, body FROM arb_messages WHERE id = %s", (claimed["id"],))
        assert cur.fetchone() == ("claimed", None)  # left claimed, not silently marked done

def test_deny_marks_denied_with_reason(conn):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = deny(conn, claimed["id"], claimed["claimed_at"], "no access to that tenant")
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, reason FROM arb_messages WHERE id = %s", (claimed["id"],))
        assert cur.fetchone() == ("denied", "no access to that tenant")

def test_fail_marks_failed_with_reason(conn):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    claimed = claim_next(conn, lease_seconds=300)
    ok = fail(conn, claimed["id"], claimed["claimed_at"], "azure API returned a transient error")
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, reason FROM arb_messages WHERE id = %s", (claimed["id"],))
        assert cur.fetchone() == ("failed", "azure API returned a transient error")

def test_deny_fenced_out_returns_false(conn):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    stale = claim_next(conn, lease_seconds=0)
    time.sleep(0.05)
    claim_next(conn, lease_seconds=0)  # reclaims, fencing the stale attempt out
    assert deny(conn, stale["id"], stale["claimed_at"], "too late") is False
