from __future__ import annotations

import base64

from arb_crypto import fingerprint, seal, unseal, validate_public_key as _validate_public_key


def register_key(conn, agent_id: str, pubkey_bytes: bytes) -> None:
    # Rotate-on-register: agent_id is derived from the shared OAuth connector
    # identity (door_tools.py:_actor), not scoped per session/project, so
    # multiple independent Claude Code sessions can legitimately share one
    # agent_id. A plain INSERT here hit arb_agent_keys_one_live_key_idx
    # (unique on agent_id WHERE revoked_at IS NULL) for any session after the
    # first, and the caller had no way to recover — it could never register a
    # key of its own, only fail. Revoking the prior live row (if any) then
    # inserting the new one, in one transaction, makes registration always
    # succeed: the most recently registered key is always the live one.
    # Tradeoff (accepted): an earlier session's key stops working with no
    # notification to that session — see docs/BACKLOG.md for the deeper,
    # not-yet-built fix (scope agent_id per session/project instead).
    _validate_public_key(pubkey_bytes)
    pubkey = base64.b64encode(pubkey_bytes).decode("ascii")
    key_fingerprint = fingerprint(pubkey_bytes)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE arb_agent_keys
                SET revoked_at = now()
                WHERE agent_id = %s AND revoked_at IS NULL
                """,
                (agent_id,),
            )
            cur.execute(
                """
                INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint)
                VALUES (%s, %s, %s)
                """,
                (agent_id, pubkey, key_fingerprint),
            )


def live_key(conn, agent_id: str) -> bytes | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pubkey
            FROM arb_agent_keys
            WHERE agent_id = %s AND revoked_at IS NULL
            """,
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return base64.b64decode(row[0])
