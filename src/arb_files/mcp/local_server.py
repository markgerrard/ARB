from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from arb_files.mcp.local_tools import LocalFileTools


def build_local_server(settings, *, store, seat_id: str) -> FastMCP:
    tools = LocalFileTools(store, seat_id=seat_id, settings=settings)
    server = FastMCP("arb-files-local")
    for name in (
        "file_list",
        "file_head",
        "file_get",
        "file_put",
        "file_delete",
        "file_get_url",
        "file_put_url",
    ):
        server.add_tool(getattr(tools, name), name=name)
    return server
