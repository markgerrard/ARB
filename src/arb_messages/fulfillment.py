from __future__ import annotations

from arb_messages import store


def claim_next(conn, lease_seconds) -> dict | None:
    row = store.claim(conn, lease_seconds)
    if row is None:
        return None
    return {
        "id": row["id"],
        "claimed_at": row["claimed_at"],
        "agent_id": row["agent_id"],
        "request_id": row["request_id"],
        "request_type": row["request_type"],
        "provider": row["provider"],
        "capability": row["capability"],  # freeform description of what's needed, not a fixed enum
        "attempts": row["attempts"],
    }


def deliver_result(conn, keys_module, row_id, claimed_at, plaintext: bytes, *, provider_token_id: str | None = None) -> bool:
    # agent_id is derived from the row itself, never taken from the caller -- an operator
    # supplying the wrong agent_id (typo, stale copy-paste) must not be able to seal a result
    # for the wrong agent's key. The same query doubles as the ownership check: if this row is
    # no longer claimed under the exact claimed_at the caller holds (reclaimed by a lease
    # timeout in the meantime), stop before doing any sealing work -- write_sealed_result's own
    # fenced_write would refuse the write anyway, but there's no reason to seal a plaintext for
    # a fence check we can already see will fail.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id FROM arb_messages WHERE id = %s AND status = 'claimed' AND claimed_at = %s",
            (row_id, claimed_at),
        )
        found = cur.fetchone()
        if found is None:
            return False
        agent_id = found[0]

    live_key = keys_module.live_key(conn, agent_id)
    if live_key is None:
        return False

    plaintext_bytes = plaintext.encode() if isinstance(plaintext, str) else plaintext
    sealed = keys_module.seal(plaintext_bytes, live_key)
    return store.write_sealed_result(conn, row_id, claimed_at, sealed, provider_token_id)


def deny(conn, row_id, claimed_at, reason: str) -> bool:
    return store.mark_denied(conn, row_id, claimed_at, reason)


def fail(conn, row_id, claimed_at, reason: str) -> bool:
    return store.mark_failed(conn, row_id, claimed_at, reason)
