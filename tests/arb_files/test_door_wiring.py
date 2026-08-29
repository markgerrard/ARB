import anyio
from mcp.server.fastmcp import FastMCP

from arb_files.mcp.door_wire import register_file_tools


ENV = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


class DummyStore:
    pass


def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}


def test_register_file_tools_noops_when_env_absent():
    server = FastMCP("test")
    assert register_file_tools(server, {}) is False
    assert not any(name.startswith("file_") for name in _tool_names(server))


def test_register_file_tools_adds_expected_tools_with_config():
    server = FastMCP("test")
    assert register_file_tools(server, ENV, store_factory=lambda: DummyStore()) is True
    assert {
        "file_list",
        "file_head",
        "file_get_url",
        "file_get_inline",
        "file_put_inline",
        "file_put_url",
        "file_delete",
    } <= _tool_names(server)


def test_register_file_tools_fail_soft_on_store_error():
    server = FastMCP("test")

    def boom():
        raise RuntimeError("backend down")

    assert register_file_tools(server, ENV, store_factory=boom) is False
    assert not any(name.startswith("file_") for name in _tool_names(server))


def _tool_descriptions(server):
    return {tool.name: (tool.description or "") for tool in anyio.run(server.list_tools)}


def test_put_tool_descriptions_carry_naming_and_memory_pointer_hints():
    # The MCP tool descriptions are how connectors (ChatGPT/claude.ai) learn the layout convention
    # and the ARB Memory<->ARB Files discoverability bridge. Guard them from silently regressing.
    server = FastMCP("test")
    assert register_file_tools(server, ENV, store_factory=lambda: DummyStore()) is True
    desc = _tool_descriptions(server)
    for tool in ("file_put_inline", "file_put_url"):
        d = desc[tool]
        assert "artefacts/" in d and "handoffs/" in d and "builds/" in d, f"{tool} missing naming hint"
        assert "memory_remember" in d, f"{tool} missing memory-pointer hint"


def test_read_tool_descriptions_point_to_memory_search():
    # The read side must close the discovery loop: when list/head can't find a file by name, the
    # assistant should be told to fall back to memory_search (not a duplicated file-search tool).
    server = FastMCP("test")
    assert register_file_tools(server, ENV, store_factory=lambda: DummyStore()) is True
    desc = _tool_descriptions(server)
    for tool in ("file_list", "file_head"):
        assert "memory_search" in desc[tool], f"{tool} missing memory_search discovery hint"
