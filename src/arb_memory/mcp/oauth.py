from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RegistrationError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import canonical_resource, load_settings, mcp_connect
from arb_memory.mcp.redirect_policy import connector_host_for_redirect_uris, is_allowed_redirect


def _default_conn_factory():
    return mcp_connect()


class ArbMemoryOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, *, settings=None, conn_factory=None):
        self.settings = settings or load_settings()
        self.conn_factory = conn_factory or _default_conn_factory

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = self.conn_factory()
        row = oauth_store.get_client(conn, client_id)
        if row is None:
            return None
        oauth_store.touch_client(conn, client_id)
        return OAuthClientInformationFull(
            client_id=row["client_id"],
            client_secret=None,
            token_endpoint_auth_method=row["metadata"].get("token_endpoint_auth_method", "client_secret_post"),
            redirect_uris=row["redirect_uris"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            # Honour the scope the client REGISTERED with (stored in metadata), not a hardcode.
            # The SDK's /authorize handler validates the request against this; a hardcoded
            # "memory.read" rejected every memory.write authorize with invalid_scope, bouncing the
            # OAuth flow back to the connector before /login. Registration already validated the
            # scope is a subset of valid_scopes, so this is safe. Fallback covers pre-scope clients.
            scope=row["metadata"].get("scope") or "memory.read",
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not redirect_uris or any(not is_allowed_redirect(uri) for uri in redirect_uris):
            import logging

            logging.getLogger("arb_memory.mcp.oauth").debug(
                "DCR rejected redirect_uris=%r (not in allowlist)", redirect_uris
            )
            raise RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed")

        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        metadata = client_info.model_dump(mode="json", exclude_none=True)
        metadata_bytes = len(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if metadata_bytes > self.settings.dcr_metadata_max_bytes:
            raise RegistrationError("invalid_client_metadata", "client metadata is too large")

        conn = self.conn_factory()
        oauth_store.gc_unused_clients(
            conn,
            older_than=datetime.now(timezone.utc) - timedelta(days=1),
        )
        if oauth_store.count_clients(conn) >= self.settings.dcr_global_cap:
            raise RegistrationError("invalid_client_metadata", "dynamic client registration cap reached")

        client_id = client_info.client_id or secrets.token_urlsafe(24)
        client_info.client_id = client_id
        oauth_store.put_client(
            conn,
            client_id=client_id,
            client_secret=None,
            redirect_uris=redirect_uris,
            metadata=metadata,
        )

    async def authorize(self, client, params) -> str:
        if client is None:
            raise AuthorizeError("unauthorized_client", "unknown client")
        redirect_uri = params.redirect_uri
        if redirect_uri is None:
            redirect_uri = client.redirect_uris[0] if client.redirect_uris else None
        redirect_uri = str(redirect_uri) if redirect_uri is not None else ""
        registered = {str(uri) for uri in client.redirect_uris or []}
        if redirect_uri not in registered or not is_allowed_redirect(redirect_uri):
            raise AuthorizeError("unauthorized_client", "redirect_uri is not allowed")

        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        scopes = list(params.scopes or ["memory.read"])
        authorize_state = {
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "resource": canonical_resource(params.resource or self.settings.public_base_url),
            "code_challenge": params.code_challenge,
            "scopes": scopes,
            "state": params.state,
        }
        conn = self.conn_factory()
        oauth_store.put_login_session(
            conn,
            session_id=session_id,
            csrf_token=csrf_token,
            authorize_state=authorize_state,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.login_ttl),
        )
        return f"{self.settings.public_base_url}/login?{urlencode({'session': session_id})}"

    def issue_authorization_code(self, authorize_state: dict) -> str:
        code = secrets.token_urlsafe(32)
        conn = self.conn_factory()
        oauth_store.put_code(
            conn,
            code=code,
            client_id=authorize_state["client_id"],
            redirect_uri=authorize_state["redirect_uri"],
            resource=authorize_state.get("resource") or self.settings.public_base_url,
            code_challenge=authorize_state["code_challenge"],
            scopes=list(authorize_state.get("scopes") or ["memory.read"]),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.code_ttl),
        )
        return code

    async def load_authorization_code(self, client, authorization_code: str):
        conn = self.conn_factory()
        row = oauth_store.get_code(conn, authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=row["expires_at"].timestamp(),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            resource=row["resource"],
        )

    async def exchange_authorization_code(self, client, authorization_code):
        conn = self.conn_factory()
        row = oauth_store.get_code(conn, authorization_code.code)
        if row is None:
            raise TokenError("invalid_grant", "authorization code does not exist")
        if (
            row["client_id"] != client.client_id
            or row["client_id"] != authorization_code.client_id
            or row["redirect_uri"] != str(authorization_code.redirect_uri)
            or canonical_resource(row["resource"]) != canonical_resource(authorization_code.resource)
            or canonical_resource(row["resource"]) != self.settings.public_base_url
            or row["code_challenge"] != authorization_code.code_challenge
        ):
            raise TokenError("invalid_grant", "authorization code binding mismatch")
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        # Atomic multi-write: consume the code + mint access + mint refresh in ONE transaction so a
        # mid-sequence failure cannot leave partial auth state (a consumed code with no refresh, or
        # an access token with no refresh). Connections are autocommit by default; this wraps the one
        # genuinely-multi-write path explicitly. (Phase 3 code-review residual; cold-Opus.)
        with conn.transaction():
            consumed = oauth_store.consume_code(conn, authorization_code.code)
            if consumed is None:
                raise TokenError("invalid_grant", "authorization code does not exist")
            scopes = list(consumed["scopes"])
            oauth_store.put_access_token(
                conn,
                token=access_token,
                client_id=client.client_id,
                resource=consumed["resource"],
                scopes=scopes,
                expires_at=now + timedelta(seconds=self.settings.access_ttl),
            )
            oauth_store.put_refresh_token(
                conn,
                token=refresh_token,
                client_id=client.client_id,
                access_token=access_token,
                resource=consumed["resource"],
                scopes=scopes,
                expires_at=now + timedelta(seconds=self.settings.refresh_ttl),
            )
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.settings.access_ttl,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client, refresh_token: str):
        conn = self.conn_factory()
        row = oauth_store.get_refresh_token(conn, refresh_token)
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"].timestamp()),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]):
        if refresh_token is None or refresh_token.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token is invalid")
        requested_scopes = list(scopes or refresh_token.scopes)
        if not set(requested_scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "requested scope is not allowed")

        conn = self.conn_factory()
        old_hash = oauth_store.hash_token(refresh_token.token)
        old_row = oauth_store.get_refresh_token_row_by_hash(conn, old_hash)
        if old_row is None or old_row["client_id"] != client.client_id or old_row["revoked_at"] is not None:
            raise TokenError("invalid_grant", "refresh token is invalid")
        if old_row["rotated_to"] is not None:
            oauth_store.revoke_refresh_family_by_hash(conn, old_hash)
            raise TokenError("invalid_grant", "refresh token reuse detected")

        access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        # Atomic rotation: rotate the refresh token (mark old + insert new) + mint the new access
        # token in ONE transaction, so a failure can't leave the old token rotated-away with no
        # usable new pair (a torn rotation = silent DoS on the connector). (Phase 3 residual.)
        with conn.transaction():
            rotated = oauth_store.rotate_refresh_token(
                conn,
                old_token=refresh_token.token,
                new_token=new_refresh_token,
                new_access_token=access_token,
            )
            if rotated is None:
                raise TokenError("invalid_grant", "refresh token is invalid")
            oauth_store.put_access_token(
                conn,
                token=access_token,
                client_id=client.client_id,
                resource=rotated["resource"],
                scopes=requested_scopes,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.access_ttl),
            )
        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=self.settings.access_ttl,
            scope=" ".join(requested_scopes),
        )

    async def revoke_token(self, token) -> None:
        conn = self.conn_factory()
        if isinstance(token, AccessToken):
            oauth_store.revoke_access_token(conn, token.token)
            return
        if isinstance(token, RefreshToken):
            oauth_store.revoke_refresh_family_by_hash(conn, oauth_store.hash_token(token.token))

    async def load_access_token(self, token: str):
        conn = self.conn_factory()
        row = oauth_store.get_access_token(conn, token)
        if row is None or canonical_resource(row["resource"]) != self.settings.public_base_url:
            return None
        # Stash a stable connector-category claim (found for ARB Messages' generalization: a
        # low-maintenance allowlist needs something more stable than client_id, which churns on
        # every re-authorization). Looked up here rather than at token-mint time so it reflects
        # the client's CURRENT registered redirect_uris even for a long-lived refresh token whose
        # access token is reissued later. Best-effort: a missing/ungettable client just means no
        # claim, never a hard failure of an otherwise-valid token.
        connector_host = None
        try:
            client_row = oauth_store.get_client(conn, row["client_id"])
            if client_row is not None:
                connector_host = connector_host_for_redirect_uris(client_row["redirect_uris"])
        except Exception:
            pass
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            claims={"connector_host": connector_host} if connector_host else None,
        )

    verify_token = load_access_token
