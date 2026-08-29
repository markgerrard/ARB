from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from psycopg.types.json import Jsonb


def hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _dict_from_cursor(cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    names = [column.name for column in cursor.description]
    return dict(zip(names, row, strict=True))


def put_code(
    conn,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    resource: str,
    code_challenge: str,
    scopes: list[str],
    expires_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_auth.auth_codes
            (code_hash, client_id, redirect_uri, resource, code_challenge, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (hash_token(code), client_id, redirect_uri, resource, code_challenge, Jsonb(scopes), expires_at),
    )


def get_code(conn, code: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            """
            SELECT * FROM mcp_auth.auth_codes
            WHERE code_hash = %s AND used_at IS NULL AND expires_at > now()
            """,
            (hash_token(code),),
        )
    )


def consume_code(conn, code: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            """
            UPDATE mcp_auth.auth_codes
            SET used_at = now()
            WHERE code_hash = %s AND used_at IS NULL AND expires_at > now()
            RETURNING *
            """,
            (hash_token(code),),
        )
    )


def put_access_token(
    conn,
    *,
    token: str,
    client_id: str,
    resource: str,
    scopes: list[str],
    expires_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_auth.access_tokens
            (token_hash, client_id, resource, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (hash_token(token), client_id, resource, Jsonb(scopes), expires_at),
    )


def get_access_token(conn, token: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            """
            SELECT * FROM mcp_auth.access_tokens
            WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now()
            """,
            (hash_token(token),),
        )
    )


def revoke_access_token(conn, token: str) -> None:
    conn.execute(
        """
        UPDATE mcp_auth.access_tokens
        SET revoked_at = now()
        WHERE token_hash = %s AND revoked_at IS NULL
        """,
        (hash_token(token),),
    )


def put_refresh_token(
    conn,
    *,
    token: str,
    client_id: str,
    access_token: str,
    resource: str,
    scopes: list[str],
    expires_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_auth.refresh_tokens
            (token_hash, client_id, access_token_hash, resource, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (hash_token(token), client_id, hash_token(access_token), resource, Jsonb(scopes), expires_at),
    )


def get_refresh_token(conn, token: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            """
            SELECT * FROM mcp_auth.refresh_tokens
            WHERE token_hash = %s
              AND revoked_at IS NULL
              AND rotated_to IS NULL
              AND expires_at > now()
            """,
            (hash_token(token),),
        )
    )


def get_refresh_token_row_by_hash(conn, token_hash: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            "SELECT * FROM mcp_auth.refresh_tokens WHERE token_hash = %s",
            (token_hash,),
        )
    )


def rotate_refresh_token(
    conn,
    *,
    old_token: str,
    new_token: str,
    new_access_token: str,
) -> dict[str, Any] | None:
    old_hash = hash_token(old_token)
    new_hash = hash_token(new_token)
    old = _dict_from_cursor(
        conn.execute(
            """
            UPDATE mcp_auth.refresh_tokens
            SET rotated_to = %s
            WHERE token_hash = %s
              AND revoked_at IS NULL
              AND rotated_to IS NULL
              AND expires_at > now()
            RETURNING client_id, resource, scopes, expires_at
            """,
            (new_hash, old_hash),
        )
    )
    if old is None:
        return None
    return _dict_from_cursor(
        conn.execute(
            """
            INSERT INTO mcp_auth.refresh_tokens
                (token_hash, client_id, access_token_hash, resource, scopes, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                new_hash,
                old["client_id"],
                hash_token(new_access_token),
                old["resource"],
                Jsonb(old["scopes"]),
                old["expires_at"],
            ),
        )
    )


def revoke_refresh_family_by_hash(conn, token_hash: str) -> None:
    token_hashes = []
    access_hashes = []
    current = token_hash
    while current:
        row = get_refresh_token_row_by_hash(conn, current)
        if row is None or row["token_hash"] in token_hashes:
            break
        token_hashes.append(row["token_hash"])
        if row["access_token_hash"]:
            access_hashes.append(row["access_token_hash"])
        current = row["rotated_to"]
    if token_hashes:
        conn.execute(
            """
            UPDATE mcp_auth.refresh_tokens
            SET revoked_at = now()
            WHERE token_hash = ANY(%s) AND revoked_at IS NULL
            """,
            (token_hashes,),
        )
    if access_hashes:
        conn.execute(
            """
            UPDATE mcp_auth.access_tokens
            SET revoked_at = now()
            WHERE token_hash = ANY(%s) AND revoked_at IS NULL
            """,
            (access_hashes,),
        )


def put_client(
    conn,
    *,
    client_id: str,
    redirect_uris: list[str],
    metadata: dict[str, Any],
    client_secret: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_auth.oauth_clients
            (client_id, client_secret_hash, redirect_uris, metadata)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (client_id) DO UPDATE
        SET client_secret_hash = EXCLUDED.client_secret_hash,
            redirect_uris = EXCLUDED.redirect_uris,
            metadata = EXCLUDED.metadata
        """,
        (
            client_id,
            hash_token(client_secret) if client_secret else None,
            Jsonb(redirect_uris),
            Jsonb(metadata),
        ),
    )


def get_client(conn, client_id: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute("SELECT * FROM mcp_auth.oauth_clients WHERE client_id = %s", (client_id,))
    )


def touch_client(conn, client_id: str) -> None:
    conn.execute(
        "UPDATE mcp_auth.oauth_clients SET last_used_at = now() WHERE client_id = %s",
        (client_id,),
    )


def count_clients(conn) -> int:
    return conn.execute("SELECT count(*) FROM mcp_auth.oauth_clients").fetchone()[0]


def gc_unused_clients(conn, *, older_than: datetime) -> int:
    cursor = conn.execute(
        """
        DELETE FROM mcp_auth.oauth_clients
        WHERE coalesce(last_used_at, created_at) < %s
        """,
        (older_than,),
    )
    return cursor.rowcount


def put_login_session(
    conn,
    *,
    session_id: str,
    csrf_token: str,
    authorize_state: dict[str, Any],
    expires_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO mcp_auth.login_sessions
            (session_id, csrf_token, authorize_state, expires_at)
        VALUES (%s, %s, %s, %s)
        """,
        (session_id, csrf_token, Jsonb(authorize_state), expires_at),
    )


def get_login_session(conn, session_id: str) -> dict[str, Any] | None:
    return _dict_from_cursor(
        conn.execute(
            """
            SELECT * FROM mcp_auth.login_sessions
            WHERE session_id = %s AND expires_at > now()
            """,
            (session_id,),
        )
    )


def bump_fail(conn, session_id: str) -> int | None:
    row = conn.execute(
        """
        UPDATE mcp_auth.login_sessions
        SET fail_count = fail_count + 1
        WHERE session_id = %s
        RETURNING fail_count
        """,
        (session_id,),
    ).fetchone()
    return None if row is None else row[0]


def get_global_login_fail_count(conn, client_id: str, *, window_seconds: int) -> int:
    window_started_after = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    row = conn.execute(
        """
        SELECT fail_count
        FROM mcp_auth.login_attempts
        WHERE client_id = %s AND window_started_at >= %s
        """,
        (client_id, window_started_after),
    ).fetchone()
    return 0 if row is None else row[0]


def bump_global_login_fail(conn, client_id: str, *, window_seconds: int) -> int:
    window_started_after = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    row = conn.execute(
        """
        INSERT INTO mcp_auth.login_attempts (client_id, fail_count, window_started_at)
        VALUES (%s, 1, now())
        ON CONFLICT (client_id) DO UPDATE
        SET fail_count = CASE
                WHEN mcp_auth.login_attempts.window_started_at < %s THEN 1
                ELSE mcp_auth.login_attempts.fail_count + 1
            END,
            window_started_at = CASE
                WHEN mcp_auth.login_attempts.window_started_at < %s THEN now()
                ELSE mcp_auth.login_attempts.window_started_at
            END
        RETURNING fail_count
        """,
        (client_id, window_started_after, window_started_after),
    ).fetchone()
    return row[0]


def reset_global_login_fail(conn, client_id: str) -> None:
    conn.execute("DELETE FROM mcp_auth.login_attempts WHERE client_id = %s", (client_id,))
