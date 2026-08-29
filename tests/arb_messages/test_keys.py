import pytest, psycopg, os, base64
from nacl.public import PrivateKey
from arb_messages.run import setup_schema
from arb_messages.keys import register_key, live_key, seal, unseal

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
        cur.execute("TRUNCATE arb_agent_keys RESTART IDENTITY")
    yield c
    c.close()

def test_register_and_lookup(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    assert live_key(conn, "agent-a") == bytes(sk.public_key)

def test_no_live_key_returns_none(conn):
    assert live_key(conn, "agent-nobody") is None

def test_malformed_pubkey_rejected_before_insert(conn):
    with pytest.raises(ValueError):
        register_key(conn, "agent-a", b"too-short")
    assert live_key(conn, "agent-a") is None

def test_second_registration_rotates_the_live_key(conn):
    # agent_id is the shared OAuth connector identity (door_tools.py:_actor),
    # not scoped per session, so a second independent session registering for
    # the same agent_id is expected, not an error condition to reject.
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    sk2 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk2.public_key))
    assert live_key(conn, "agent-a") == bytes(sk2.public_key)

def test_rotation_preserves_prior_key_as_revoked_not_deleted(conn):
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    sk2 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk2.public_key))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pubkey, revoked_at IS NOT NULL FROM arb_agent_keys "
            "WHERE agent_id = 'agent-a' ORDER BY created_at"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0] == (base64.b64encode(bytes(sk1.public_key)).decode("ascii"), True)
    assert rows[1] == (base64.b64encode(bytes(sk2.public_key)).decode("ascii"), False)

def test_rotation_is_atomic_no_gap_with_zero_or_two_live_keys(conn):
    # Guards the conn.transaction() wrap: a crash between the UPDATE and the
    # INSERT must never be observable as a partial state (fault injection via
    # a forced rollback mid-registration is impractical here; this asserts
    # the invariant register_key must maintain on every successful call).
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    sk2 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk2.public_key))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM arb_agent_keys WHERE agent_id = 'agent-a' AND revoked_at IS NULL")
        assert cur.fetchone()[0] == 1

def test_revoked_key_excluded_from_live_lookup(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'agent-a'")
    assert live_key(conn, "agent-a") is None

def test_reregistration_after_revoke_succeeds(conn):
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'agent-a'")
    sk2 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk2.public_key))
    assert live_key(conn, "agent-a") == bytes(sk2.public_key)

def test_seal_unseal_round_trip():
    sk = PrivateKey.generate()
    ciphertext = seal(b"a real token", bytes(sk.public_key))
    assert unseal(ciphertext, bytes(sk)) == b"a real token"
    assert b"a real token" not in ciphertext  # sanity: not accidentally cleartext
