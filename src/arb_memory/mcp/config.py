from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    mcp_dsn: str
    login_secret: str
    totp_secret: str
    access_ttl: int = 3600
    refresh_ttl: int = 2592000
    code_ttl: int = 600
    login_ttl: int = 300
    dcr_global_cap: int = 200
    dcr_metadata_max_bytes: int = 4096
    search_max_query_chars: int = 2000
    search_rate_per_min: int = 30
    login_global_fail_cap: int = 20
    write_rate_per_min: int = 30
    graph_rate_per_min: int = 30
    write_max_content_bytes: int = 262144
    write_index_chars: int = 8000
    write_max_text_chars: int = 8192
    write_max_tags: int = 16
    write_max_tag_chars: int = 64


def _append_query_param(dsn: str, key: str, value: str) -> str:
    parts = urlsplit(dsn)
    if not parts.scheme:
        sep = "&" if "?" in dsn else "?"
        return f"{dsn}{sep}{urlencode({key: value})}"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _default_mcp_dsn() -> str:
    dsn = os.environ.get("ARB_MEMORY_MCP_DSN")
    if dsn:
        return dsn
    base = os.environ.get("ARB_MEMORY_DSN", "")
    return base.replace("arb_memory:", f"{mcp_role_name()}:", 1)


def mcp_role_name() -> str:
    return os.environ.get("ARB_MEMORY_MCP_ROLE", "arbmem_mcp")


def canonical_resource(value) -> str:
    """Canonicalize an RFC 8707 resource indicator for comparison.

    Real OAuth clients (claude.ai) send the resource as the RFC 3986-normalized URI, which for a
    bare authority carries a trailing slash (https://host/). public_base_url is stored slash-stripped
    (load_settings, above), so any strict-equality audience check must normalize the resource the same
    way or every real token exchange 400s on a spurious audience mismatch. Trailing-slash-only: this
    does NOT collapse distinct paths, so https://host/evil stays != https://host. (Phase 3 connector
    canary against real claude.ai: POST /token -> 400 invalid_grant after a clean DCR + 2FA.)
    """
    return str(value).rstrip("/") if value is not None else ""


def load_settings() -> Settings:
    public_base_url = os.environ.get("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base_url:
        raise ValueError("ARB_MEMORY_MCP_PUBLIC_BASE_URL is required")

    mcp_dsn = _default_mcp_dsn()
    sslmode = os.environ.get("ARB_MEMORY_MCP_SSLMODE")
    if sslmode:
        mcp_dsn = _append_query_param(mcp_dsn, "sslmode", sslmode)

    login_secret = os.environ.get("ARB_MEMORY_MCP_LOGIN_SECRET", "")
    if not login_secret:
        raise ValueError("ARB_MEMORY_MCP_LOGIN_SECRET is required")
    totp_secret = os.environ.get("ARB_MEMORY_MCP_TOTP_SECRET", "")
    if not totp_secret:
        raise ValueError("ARB_MEMORY_MCP_TOTP_SECRET is required")

    return Settings(
        public_base_url=public_base_url,
        mcp_dsn=mcp_dsn,
        login_secret=login_secret,
        totp_secret=totp_secret,
    )


def mcp_connect(settings: Settings | None = None) -> psycopg.Connection:
    settings = settings or load_settings()
    return psycopg.connect(settings.mcp_dsn, autocommit=True)
