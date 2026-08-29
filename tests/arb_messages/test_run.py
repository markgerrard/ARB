import pytest, psycopg, os
from arb_messages.run import setup_schema

DSN = os.environ.get("ARB_MESSAGES_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="ARB_MESSAGES_TEST_DSN not set — Postgres-backed; run via scripts/arb-messages-gate, which forbids skips",
)

@pytest.fixture
def conn():
    # Plan-review r2 (cold-Opus P1): the original fixture had no TRUNCATE, unlike every sibling
    # test file's fixture (Task 2-5 all truncate before yielding) -- and with autocommit=True,
    # this file's own conn.rollback() calls after a deliberate UniqueViolation are no-ops (there's
    # no open transaction to roll back), so the row inserted by
    # test_creates_arb_messages_with_compound_unique persists into later tests in this same file
    # (including test_arb_messages_has_completed_at_and_body_is_bytea, whose INSERT for the same
    # (agent_id, request_id) pair would then hit a real UniqueViolation of its own). Matched to
    # the sibling fixtures' pattern.
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys, arb_messages_settings RESTART IDENTITY")
    setup_schema(c)  # re-seed arb_messages_settings's 'paused' row via setup_schema's own
                      # idempotent ON CONFLICT DO NOTHING logic, not a duplicate raw INSERT
    yield c
    c.close()

def test_setup_schema_idempotent(conn):
    setup_schema(conn)
    setup_schema(conn)  # must not raise on second run

def test_creates_arb_messages_with_compound_unique(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability) VALUES ('a','r1','mint','cloudflare','zone_dns_edit')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability) VALUES ('a','r1','mint','cloudflare','zone_dns_edit')")
    conn.rollback()

def test_arb_messages_allows_same_request_id_different_agent(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability) VALUES ('a','r1','mint','cloudflare','zone_dns_edit')")
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability) VALUES ('b','r1','mint','cloudflare','zone_dns_edit')")
    conn.commit()

def test_arb_agent_keys_one_live_key_per_agent(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk1','fp1')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk2','fp2')")
    conn.rollback()
    with conn.cursor() as cur:
        # revoke then re-register succeeds
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'a'")
        cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk2','fp2')")
    conn.commit()

def test_settings_paused_seed_survives_rerun(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_messages_settings SET value = '1' WHERE key = 'paused'")
    conn.commit()
    setup_schema(conn)  # must NOT reset paused back to '0'
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM arb_messages_settings WHERE key = 'paused'")
        assert cur.fetchone()[0] == "1"

def test_arb_messages_has_completed_at_and_body_is_bytea(conn):
    # Plan-review r1 fix's own explicit schema guard, proactively added ahead of round-2 review.
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO arb_messages (agent_id, request_id, request_type, provider,
            capability, body, completed_at) VALUES ('a','r1','mint','cloudflare',
            'zone_dns_edit', %s, now()) RETURNING completed_at, body""", (b"raw-bytes-not-json",))
        completed_at, body = cur.fetchone()
        assert completed_at is not None
        assert bytes(body) == b"raw-bytes-not-json"  # bytea round-trips raw bytes; jsonb would reject this insert
    conn.commit()
