from __future__ import annotations

from typing import Any


_ROW_COLUMNS = (
    "id",
    "agent_id",
    "request_id",
    "request_type",
    "provider",
    "capability",
    "status",
    "policy_decision",
    "policy_reason",
    "reason",
    "provider_token_id",
    "body",
    "attempts",
    "claimed_at",
    "completed_at",
    "delivered_at",
    "token_revoked_at",
    "created_at",
)


def _row_dict(cur, row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {desc.name: value for desc, value in zip(cur.description, row)}


def enqueue(conn, agent_id, request_id, request_type, provider, capability,
            policy_decision, policy_reason) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arb_messages
                (agent_id, request_id, request_type, provider, capability,
                 policy_decision, policy_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, request_id) DO NOTHING
            RETURNING id
            """,
            (agent_id, request_id, request_type, provider, capability,
             policy_decision, policy_reason),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "SELECT id FROM arb_messages WHERE agent_id = %s AND request_id = %s",
            (agent_id, request_id),
        )
        return cur.fetchone()[0]


def claim(conn, lease_seconds) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH candidate AS (
                SELECT id
                FROM arb_messages
                WHERE status = 'pending'
                   OR (status = 'claimed'
                       AND claimed_at < now() - (%s * interval '1 second'))
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE arb_messages AS m
            SET status = 'claimed',
                claimed_at = now(),
                attempts = attempts + 1,
                token_revoked_at = NULL
            FROM candidate
            WHERE m.id = candidate.id
            RETURNING {", ".join("m." + col for col in _ROW_COLUMNS)}
            """,
            (lease_seconds,),
        )
        return _row_dict(cur, cur.fetchone())


def fenced_write(conn, row_id, claimed_at, **fields) -> bool:
    if not fields:
        return True
    assignments = []
    values = []
    for name, value in fields.items():
        if isinstance(value, _SqlNow):
            assignments.append(f"{name} = now()")
        else:
            assignments.append(f"{name} = %s")
            values.append(value)
    values.extend([row_id, claimed_at])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE arb_messages
            SET {", ".join(assignments)}
            WHERE id = %s
              AND status = 'claimed'
              AND claimed_at = %s
              AND token_revoked_at IS NULL
            """,
            tuple(values),
        )
        return cur.rowcount == 1


def is_still_owner(conn, row_id, claimed_at) -> bool:
    """Read-only ownership check: does this worker still hold the claim it believes it does?

    Added after a P0 found during the final code-review round (not caught by any of the 9
    spec/plan panel rounds, nor by the plan's own test suite): `fenced_write` correctly blocks a
    stale worker's WRITEs, but a stale worker that reads live state (e.g. a name-based
    Cloudflare token lookup) and then acts DESTRUCTIVELY on what it reads (revoking a token) can
    still damage a different, newer, legitimate worker's already-completed mint before it ever
    attempts a write of its own -- fencing only protected writes, not read-then-destroy actions.
    Callers that are about to perform any external side effect (not just a DB write) on behalf of
    a claimed row must check this FIRST and bail out immediately if it returns False, before
    touching anything external.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM arb_messages
            WHERE id = %s AND status = 'claimed' AND claimed_at = %s AND token_revoked_at IS NULL
            """,
            (row_id, claimed_at),
        )
        return cur.fetchone() is not None


def read_and_mark_delivered(conn, agent_id, request_id) -> dict:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, body, delivered_at
                FROM arb_messages
                WHERE agent_id = %s AND request_id = %s
                FOR UPDATE
                """,
                (agent_id, request_id),
            )
            row = cur.fetchone()
            if row is None:
                return {"status": "not_found"}
            row_id, status, body, delivered_at = row
            if status in ("pending", "claimed", "failed", "denied"):
                return {"status": status}
            if status == "done" and delivered_at is None:
                cur.execute("UPDATE arb_messages SET delivered_at = now() WHERE id = %s", (row_id,))
                return {"status": "delivered", "body": bytes(body)}
            if status == "done" and delivered_at is not None:
                # Safe re-delivery to the same agent: the SELECT is agent_id-scoped and the
                # body is ciphertext sealed to that agent's registered key. This closes the
                # mark-before-confirmed-transmission loss window from the 2026-07-02 incident class.
                return {"status": "already_delivered", "body": bytes(body)}
            return {"status": status}


def write_sealed_result(conn, row_id, claimed_at, sealed_bytes: bytes, provider_token_id: str) -> bool:
    return fenced_write(
        conn,
        row_id,
        claimed_at,
        status="done",
        body=sealed_bytes,
        provider_token_id=provider_token_id,
        completed_at=_SqlNow(),
    )


def write_provider_token_id(conn, row_id, claimed_at, provider_token_id: str) -> bool:
    return fenced_write(conn, row_id, claimed_at, provider_token_id=provider_token_id)


def mark_failed(conn, row_id, claimed_at, reason: str) -> bool:
    return fenced_write(conn, row_id, claimed_at, status="failed", policy_reason=reason, reason=reason)


def mark_denied(conn, row_id, claimed_at, reason: str) -> bool:
    return fenced_write(conn, row_id, claimed_at, status="denied", policy_reason=reason, reason=reason)


class _SqlNow:
    pass
