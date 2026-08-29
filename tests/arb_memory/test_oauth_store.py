from datetime import datetime, timedelta, timezone
import uuid

from arb_memory.mcp import oauth_store


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def test_access_token_is_stored_hashed_without_plaintext(scratch):
    token = f"plain-access-token-{uuid.uuid4().hex}"
    oauth_store.put_access_token(
        scratch,
        token=token,
        client_id="client-hash",
        resource="https://mem.example.com",
        scopes=["memory.read"],
        expires_at=_future(),
    )

    row = scratch.execute(
        "SELECT * FROM mcp_auth.access_tokens WHERE token_hash = %s",
        (oauth_store.hash_token(token),),
    ).fetchone()
    assert row is not None
    assert token not in {str(value) for value in row}


def test_consume_code_returns_row_once(scratch):
    code = f"code-{uuid.uuid4().hex}"
    oauth_store.put_code(
        scratch,
        code=code,
        client_id="client-code",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        resource="https://mem.example.com",
        code_challenge="challenge",
        scopes=["memory.read"],
        expires_at=_future(),
    )

    first = oauth_store.consume_code(scratch, code)
    second = oauth_store.consume_code(scratch, code)
    assert first is not None
    assert first["client_id"] == "client-code"
    assert second is None


def test_rotate_refresh_token_marks_old_and_rejects_reuse(scratch):
    old_refresh = f"refresh-old-{uuid.uuid4().hex}"
    new_refresh = f"refresh-new-{uuid.uuid4().hex}"
    old_access = f"access-old-{uuid.uuid4().hex}"
    new_access = f"access-new-{uuid.uuid4().hex}"
    oauth_store.put_refresh_token(
        scratch,
        token=old_refresh,
        client_id="client-refresh",
        access_token=old_access,
        resource="https://mem.example.com",
        scopes=["memory.read"],
        expires_at=_future(),
    )

    rotated = oauth_store.rotate_refresh_token(
        scratch,
        old_token=old_refresh,
        new_token=new_refresh,
        new_access_token=new_access,
    )

    assert rotated is not None
    assert rotated["token_hash"] == oauth_store.hash_token(new_refresh)
    assert oauth_store.get_refresh_token(scratch, old_refresh) is None
    old_rotated_to = scratch.execute(
        "SELECT rotated_to FROM mcp_auth.refresh_tokens WHERE token_hash = %s",
        (oauth_store.hash_token(old_refresh),),
    ).fetchone()[0]
    assert old_rotated_to == oauth_store.hash_token(new_refresh)
