import pytest, psycopg, os, time
from arb_messages.run import setup_schema
from arb_messages.store import (enqueue, claim, fenced_write, read_and_mark_delivered,
    write_sealed_result, write_provider_token_id, mark_failed)

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

def test_enqueue_dedupes_same_agent_request_id(conn):
    id1 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    id2 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    assert id1 == id2

def test_enqueue_allows_same_request_id_different_agent(conn):
    id1 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    id2 = enqueue(conn, "b", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    assert id1 != id2

def test_claim_two_concurrent_never_claim_same_row(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    conn2 = psycopg.connect(DSN, autocommit=True)
    try:
        # both connections attempt to claim; SKIP LOCKED means at most one gets it per call
        row1 = claim(conn, lease_seconds=300)
        row2 = claim(conn2, lease_seconds=300)
        assert row1 is not None
        assert row2 is None  # already claimed by conn
    finally:
        conn2.close()

def test_lease_expiry_makes_claimed_row_reclaimable(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=0)  # zero-second lease: immediately "expired"
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)
    assert row2 is not None and row2["id"] == row["id"]

def test_fenced_write_stale_claim_affects_zero_rows(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=0)
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)  # reclaims, new claimed_at
    assert row2["id"] == row["id"]
    ok = fenced_write(conn, row["id"], row["claimed_at"], provider_token_id="stale-token")
    assert ok is False  # row["claimed_at"] is now stale, worker "row" lost the race

# Plan-review r2 (agy-print P1): the direct regression guard for the sweep-vs-slow-worker race.
# A slow worker whose claimed_at is STILL VALID (no reclaim happened -- only the sweep acted on
# the row) must still be fenced out once the sweep has revoked its token, or the agent would
# receive a sealed result pointing at an already-dead token.
def test_fenced_write_blocked_after_sweep_revokes_the_token(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=300)  # long lease -- no reclaim will happen
    with conn.cursor() as cur:
        # simulates the sweep: revokes the token, but does NOT touch status/claimed_at
        cur.execute("UPDATE arb_messages SET provider_token_id = 'tok-x', token_revoked_at = now() WHERE id = %s", (row["id"],))
    # the ORIGINAL slow worker, still holding the SAME (still-technically-valid) claimed_at,
    # now tries to complete the row it thinks it still owns
    ok = fenced_write(conn, row["id"], row["claimed_at"], status="done", body=b"stale-sealed-bytes")
    assert ok is False  # must be fenced out even though claimed_at still matches -- the sweep
                         # already revoked the token this write would have delivered

# Plan-review r3 (codex P1, agy-print P1, cold-Opus P1 -- 3-way convergence): the direct
# regression guard for the reclaim-after-sweep wedge the r2 fix introduced. A LEGITIMATE
# reclaiming worker (fresh claimed_at, not the stale slow worker from the test above) must be
# able to write successfully, proving claim()'s token_revoked_at=NULL reset actually works.
def test_reclaim_after_sweep_revoke_can_still_write(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=0)  # zero-lease so it's immediately reclaimable
    with conn.cursor() as cur:
        # simulates: worker A wrote provider_token_id then stalled; sweep category 1 revoked it
        cur.execute("UPDATE arb_messages SET provider_token_id = 'tok-a-stale', token_revoked_at = now() WHERE id = %s", (row["id"],))
    time.sleep(0.05)
    row_b = claim(conn, lease_seconds=0)  # worker B legitimately reclaims -- a normal lease-expiry reclaim
    assert row_b["id"] == row["id"]
    # worker B's fresh claimed_at must NOT be fenced out by the stale token_revoked_at left
    # behind by the sweep -- claim() must have reset it on reclaim
    ok = fenced_write(conn, row_b["id"], row_b["claimed_at"], provider_token_id="tok-b-fresh")
    assert ok is True  # the legitimate new claimant can write; the row is not permanently wedged
    with conn.cursor() as cur:
        cur.execute("SELECT token_revoked_at FROM arb_messages WHERE id = %s", (row_b["id"],))
        assert cur.fetchone()[0] is None  # reset by the reclaim, as claim()'s UPDATE now specifies

def test_read_and_mark_delivered_pending_row_is_true_noop(conn):
    # Plan-review r1 (agy-print P1, codex P1, cold-Opus P1 -- 3-way convergence): the original
    # version of this test only checked the returned status dict, never the DB's actual
    # delivered_at column. A buggy implementation that sets delivered_at=now() on every read
    # regardless of status (the exact round-4/5 bug the spec fixed) would still pass a
    # status-only assertion. Strengthened to check delivered_at directly, AND to prove the poll
    # sequence end-to-end: an early poll while pending must not consume the one-time slot once
    # the row genuinely completes.
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    result = read_and_mark_delivered(conn, "a", "r1")
    assert result["status"] == "pending"
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] is None
    result2 = read_and_mark_delivered(conn, "a", "r1")
    assert result2["status"] == "pending"  # repeatable, not consumed
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] is None
    # Now complete the row and confirm the earlier pending polls did NOT consume the slot --
    # the subsequent poll must return the real sealed body, not already_delivered.
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    result3 = read_and_mark_delivered(conn, "a", "r1")
    assert result3["status"] == "delivered" and result3["body"] == b"sealed-bytes"

def test_read_and_mark_delivered_redelivers_to_same_agent(conn):
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    r1 = read_and_mark_delivered(conn, "a", "r1")
    assert r1["status"] == "delivered" and r1["body"] == b"sealed-bytes"
    r2 = read_and_mark_delivered(conn, "a", "r1")
    assert r2["status"] == "already_delivered"
    assert r2["body"] == b"sealed-bytes"

def test_redelivery_does_not_update_delivered_at(conn):
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    r1 = read_and_mark_delivered(conn, "a", "r1")
    assert r1["status"] == "delivered"
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        first_delivered_at = cur.fetchone()[0]
    r2 = read_and_mark_delivered(conn, "a", "r1")
    assert r2["status"] == "already_delivered"
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == first_delivered_at

def test_redelivery_wrong_agent_still_not_found(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    r1 = read_and_mark_delivered(conn, "a", "r1")
    assert r1["status"] == "delivered"
    result = read_and_mark_delivered(conn, "b", "r1")
    assert result["status"] == "not_found"

def test_read_and_mark_delivered_wrong_actor_not_found(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    result = read_and_mark_delivered(conn, "b", "r1")  # wrong agent_id
    assert result["status"] == "not_found"

def test_read_and_mark_delivered_failed_and_denied_are_noops(conn):
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row = claim(conn, lease_seconds=300)
    mark_failed(conn, row["id"], row["claimed_at"], "test failure")
    result = read_and_mark_delivered(conn, "a", "r1")
    assert result["status"] == "failed"

def test_write_sealed_result_has_no_plaintext_parameter():
    import inspect
    sig = inspect.signature(write_sealed_result)
    assert "token" not in sig.parameters and "plaintext" not in sig.parameters
    assert "sealed_bytes" in sig.parameters

def test_attempts_column_increments_on_each_claim(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "approved", None)
    row1 = claim(conn, lease_seconds=0)
    assert row1["attempts"] == 1
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)
    assert row2["attempts"] == 2
