import anyio
from mcp.server.fastmcp import FastMCP
from arb_messages.mcp.door_wire import register_messages_tools

ENV = {"ARB_MESSAGES_POSTGRES_DSN": "postgresql://x", "ARB_MESSAGES_ALLOWED_AGENTS": "agent-a",
       "ARB_MESSAGES_ALLOWED_PROVIDERS": "cloudflare"}

def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}

def test_noop_without_required_env():
    server = FastMCP("test")
    assert register_messages_tools(server, {}) is False
    assert not _tool_names(server)

def test_registers_all_three_tools_on_valid_config():
    server = FastMCP("test")
    assert register_messages_tools(server, ENV) is True
    assert _tool_names(server) == {"messages_request", "messages_register_key", "messages_poll"}

def test_kill_switch_off_prevents_registration():
    server = FastMCP("test")
    assert register_messages_tools(server, {**ENV, "ARB_MESSAGES_ENABLED": "0"}) is False
    assert not _tool_names(server)

def test_fail_soft_on_construction_error():
    server = FastMCP("test")
    def boom(): raise RuntimeError("backend down")
    assert register_messages_tools(server, ENV, client_factory=boom) is False
    assert not _tool_names(server)

def test_scope_in_both_valid_and_default_scopes():
    # Matches tests/arb_email/test_door_wiring.py:58-77's exact real pattern -- scopes live on
    # the constructed server's settings, not a module attribute.
    from arb_memory.mcp.config import Settings
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = Settings(public_base_url="https://mem.example.com", mcp_dsn="postgresql://example",
                         login_secret="passphrase", totp_secret="totp")
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: None)
    server = build_server(settings=settings, provider=provider, conn_factory=lambda: None,
                           embed=lambda _t: [])
    scopes = server.settings.auth.client_registration_options
    assert "messages.request" in scopes.valid_scopes
    assert "messages.request" in scopes.default_scopes  # in default_scopes too, matching the
    # chatgpt-connector-scope-grant lesson: ChatGPT only requests its DCR-registered/default set
    # messages.fulfill: also in default_scopes (operator decision 2026-07-02, explicit, after
    # the ChatGPT-can-never-pick-up-a-non-default-scope tradeoff was surfaced -- see
    # server.py's ClientRegistrationOptions comment for the full reasoning and the accepted
    # consequence that every future claude.ai session also receives this scope).
    assert "messages.fulfill" in scopes.valid_scopes
    assert "messages.fulfill" in scopes.default_scopes
