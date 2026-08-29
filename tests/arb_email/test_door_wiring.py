import anyio
from mcp.server.fastmcp import FastMCP

from arb_email.mcp.door_wire import register_email_tool


ENV = {
    "ARB_EMAIL_POSTMARK_TOKEN": "tok",
    "ARB_EMAIL_SEND_ENABLED": "1",
}


class FakeClient:
    def send(self, subject, html_body, text_body, *, to, actor):
        return {"sent": True, "message_id": "mid-door", "to": to, "stream": "arb"}


def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}


def _tool_descriptions(server):
    return {tool.name: (tool.description or "") for tool in anyio.run(server.list_tools)}


def test_register_email_tool_noops_without_token():
    server = FastMCP("test")
    assert register_email_tool(server, {}) is False
    assert "email_send" not in _tool_names(server)


def test_register_email_tool_noops_when_disabled():
    server = FastMCP("test")
    assert register_email_tool(server, {**ENV, "ARB_EMAIL_SEND_ENABLED": "0"}) is False
    assert "email_send" not in _tool_names(server)


def test_register_email_tool_fail_soft_on_construction_error():
    server = FastMCP("test")

    def boom():
        raise RuntimeError("backend down")

    assert register_email_tool(server, ENV, client_factory=boom) is False
    assert "email_send" not in _tool_names(server)


def test_register_email_tool_adds_email_send_with_description():
    server = FastMCP("test")
    assert register_email_tool(server, ENV, client_factory=lambda: FakeClient()) is True
    assert "email_send" in _tool_names(server)
    desc = _tool_descriptions(server)["email_send"]
    assert "arb@example.com" in desc
    assert "allowlisted" in desc
    assert "arb" in desc
    assert "model-supplied" in desc


def test_server_scope_wiring_valid_not_default():
    from arb_memory.mcp.config import Settings
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: None)
    server = build_server(settings=settings, provider=provider, conn_factory=lambda: None, embed=lambda _t: [])
    scopes = server.settings.auth.client_registration_options
    assert "email.send" in scopes.valid_scopes
    # email.send is now ALSO a default scope so ChatGPT (which only requests its DCR-registered/default
    # set, unlike claude.ai) can be granted it on a fresh reconnect. Safe: scope != containment
    # (allowlist + caps + kill switch are). See memory chatgpt-connector-scope-grant.
    assert "email.send" in scopes.default_scopes
