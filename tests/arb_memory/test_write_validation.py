import re

import pytest

from arb_memory.mcp import tools
from arb_memory.mcp.config import Settings

S = Settings(
    public_base_url="https://x",
    mcp_dsn="postgresql://x",
    login_secret="l",
    totp_secret="t",
)


def test_derive_artefact_id_is_deterministic_and_prefixed():
    a = tools.derive_artefact_id("hello world", "text/plain")
    b = tools.derive_artefact_id("hello world", "text/plain")
    assert a == b and a.startswith("art-") and re.fullmatch(r"art-[0-9a-f]{16}", a)


def test_validate_content_rejects_empty_oversize_badmime():
    with pytest.raises(ValueError):
        tools.validate_content("", "text/plain", S)
    with pytest.raises(ValueError):
        tools.validate_content("x" * (S.write_max_content_bytes + 1), "text/plain", S)
    with pytest.raises(ValueError):
        tools.validate_content("ok", "application/x-evil", S)
    tools.validate_content("ok", "text/markdown", S)


def test_validate_content_requires_valid_json_for_json_mime():
    with pytest.raises(ValueError, match="content is not valid JSON"):
        tools.validate_content("not-json", "application/json", S)
    tools.validate_content('{"ok": true}', "application/json", S)


def test_validate_artefact_id_charset():
    tools.validate_artefact_id("art-abc_123")
    with pytest.raises(ValueError):
        tools.validate_artefact_id("bad id with spaces")
    with pytest.raises(ValueError):
        tools.validate_artefact_id("x" * 65)


def test_require_write_scope_denies_anonymous_and_missing(monkeypatch):
    mt = tools.MemoryTools(S, conn_factory=lambda: None, embed=lambda t: [])
    monkeypatch.setattr(tools, "get_access_token", lambda: None)
    with pytest.raises(PermissionError):
        mt._require_write_scope()

    class _Tok:
        scopes = ["memory.read"]

    monkeypatch.setattr(tools, "get_access_token", lambda: _Tok())
    with pytest.raises(PermissionError):
        mt._require_write_scope()

    class _Tok2:
        scopes = ["memory.read", "memory.write"]

    monkeypatch.setattr(tools, "get_access_token", lambda: _Tok2())
    mt._require_write_scope()
