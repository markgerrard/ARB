import anyio
from mcp.server.fastmcp import FastMCP
from arb_messages.mcp.fulfillment_wire import register_fulfillment_tools

ENV = {"ARB_MESSAGES_POSTGRES_DSN": "postgresql://x", "ARB_MESSAGES_ALLOWED_AGENTS": "agent-a",
       "ARB_MESSAGES_ALLOWED_PROVIDERS": "cloudflare"}

def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}

def test_noop_without_required_env():
    server = FastMCP("test")
    assert register_fulfillment_tools(server, {}) is False
    assert not _tool_names(server)

def test_registers_all_four_tools_on_valid_config():
    server = FastMCP("test")
    assert register_fulfillment_tools(server, ENV) is True
    assert _tool_names(server) == {
        "messages_claim_next", "messages_deliver_result", "messages_deny", "messages_fail",
    }

def test_kill_switch_off_prevents_registration():
    server = FastMCP("test")
    assert register_fulfillment_tools(server, {**ENV, "ARB_MESSAGES_ENABLED": "0"}) is False
    assert not _tool_names(server)

def test_fail_soft_on_construction_error():
    server = FastMCP("test")
    def boom(): raise RuntimeError("backend down")
    assert register_fulfillment_tools(server, ENV, client_factory=boom) is False
    assert not _tool_names(server)

# messages.fulfill is now default-granted to every connecting client (claude.ai, ChatGPT), not
# scoped to Codex App specifically -- there is no technical restriction narrowing these tools to
# the Codex App operator. The tool description is the only guardrail telling an ordinary agent
# session not to call them; direct regression guard that it's actually present on all four.
def test_all_four_tools_carry_the_codex_app_only_notice():
    server = FastMCP("test")
    register_fulfillment_tools(server, ENV)
    tools = anyio.run(server.list_tools)
    assert len(tools) == 4
    for tool in tools:
        assert "CODEX APP ONLY" in tool.description
        assert "do NOT call this tool" in tool.description
