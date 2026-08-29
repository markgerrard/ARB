from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from arb_memory.mcp.read_tools import LocalReadSettings, ReadMemoryTools


def build_local_server(settings: LocalReadSettings, *, conn_factory=None, embed=None) -> FastMCP:
    tools = ReadMemoryTools(settings, conn_factory=conn_factory, embed=embed)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            tools.close()

    server = FastMCP("arb-memory-local", lifespan=lifespan)
    server.add_tool(tools.memory_search, name="memory_search")
    server.add_tool(tools.memory_get, name="memory_get")
    server.add_tool(tools.memory_recent, name="memory_recent")
    server.add_tool(tools.memory_related, name="memory_related")
    server.add_tool(tools.memory_references, name="memory_references")
    return server
