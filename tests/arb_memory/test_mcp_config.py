import pytest

from arb_memory.mcp.config import Settings, mcp_role_name


def test_settings_require_public_base_url(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", raising=False)
    import arb_memory.mcp.config as cfg

    with pytest.raises(ValueError, match="ARB_MEMORY_MCP_PUBLIC_BASE_URL"):
        cfg.load_settings()


def test_settings_carry_public_base_url(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com/")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory:pw@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", "passphrase")
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", "totp")
    import arb_memory.mcp.config as cfg

    settings = cfg.load_settings()
    assert settings.public_base_url == "https://mem.example.com"


def test_settings_mcp_dsn_uses_mcp_role_and_sslmode(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory:pw@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", "passphrase")
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", "totp")
    monkeypatch.delenv("ARB_MEMORY_MCP_DSN", raising=False)
    monkeypatch.setenv("ARB_MEMORY_MCP_SSLMODE", "require")
    monkeypatch.setenv("ARB_MEMORY_MCP_ROLE", "arbmemory-dev-mcp")
    import arb_memory.mcp.config as cfg

    settings = cfg.load_settings()
    assert f"{mcp_role_name()}:pw" in settings.mcp_dsn
    assert "sslmode=require" in settings.mcp_dsn


@pytest.mark.parametrize(
    "name",
    ["ARB_MEMORY_MCP_LOGIN_SECRET", "ARB_MEMORY_MCP_TOTP_SECRET"],
)
def test_settings_reject_empty_login_secrets(monkeypatch, name):
    monkeypatch.setenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com")
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory:pw@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", "passphrase")
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", "totp")
    monkeypatch.setenv(name, "")
    import arb_memory.mcp.config as cfg

    with pytest.raises(ValueError, match=name):
        cfg.load_settings()


def test_mcp_connect_opens_autocommit_connection(monkeypatch):
    calls = []
    sentinel = object()

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    import arb_memory.mcp.config as cfg

    monkeypatch.setattr(cfg.psycopg, "connect", fake_connect)
    settings = Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )

    assert cfg.mcp_connect(settings) is sentinel
    assert calls == [((settings.mcp_dsn,), {"autocommit": True})]
