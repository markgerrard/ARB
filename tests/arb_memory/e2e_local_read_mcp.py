from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import psycopg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


pytestmark = pytest.mark.e2e


def _local_dsn() -> str | None:
    dsn = os.environ.get("ARB_MEMORY_LOCAL_DSN")
    if dsn and "password=" in dsn:
        return dsn
    env_file = Path("envs/arb-memory-dev.env")
    if not env_file.exists():
        return dsn
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ARB_MEMORY_LOCAL_DSN="):
            return line.split("=", 1)[1]
    return dsn


@pytest.mark.anyio
async def test_local_read_mcp_live_reader_role_e2e():
    dsn = _local_dsn()
    if not dsn:
        pytest.skip("no ARB_MEMORY_LOCAL_DSN")

    with psycopg.connect(dsn) as conn:
        current_user = conn.execute("SELECT current_user").fetchone()[0]
        assert current_user == "arbmem_local_reader"
        print(f"local-read-mcp current_user={current_user}")
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'hints', 'SELECT')"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT has_table_privilege(current_user, 'hints', 'INSERT')"
        ).fetchone()[0] is False

    env = os.environ.copy()
    env["ARB_MEMORY_LOCAL_DSN"] = dsn
    env["PYTHONPATH"] = f"{os.getcwd()}:{os.getcwd()}/src"
    env["PATH"] = os.environ.get("PATH", "")
    if os.environ.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    else:
        env.pop("OPENAI_API_KEY", None)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arb_memory", "local-read-mcp"],
        env=env,
    )

    with tempfile.TemporaryFile("w+") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == {
                    "memory_search",
                    "memory_get",
                    "memory_recent",
                    "memory_related",
                    "memory_references",
                }
                assert "memory_store" not in names
                assert "memory_remember" not in names
                print(f"local-read-mcp tools={sorted(names)}")

                recent = await session.call_tool("memory_recent", {"limit": 1})
                assert recent.isError is False
                print("local-read-mcp memory_recent ok")

                if recent.content:
                    assert recent.structuredContent is not None
                    assert "result" in recent.structuredContent
                    recent_rows = recent.structuredContent["result"]
                    assert recent_rows, "memory_recent returned content but no result rows"
                    first = recent_rows[0]
                    got = await session.call_tool(
                        "memory_get",
                        {
                            "artefact_id": first["artefact_id"],
                            "version": first["version"],
                        },
                    )
                    assert got.isError is False
                    print("local-read-mcp memory_get ok")

                search = await session.call_tool("memory_search", {"query": "x", "k": 1})
                if os.environ.get("OPENAI_API_KEY"):
                    assert search.isError is False
                else:
                    assert search.isError is True
                    assert any(
                        "OPENAI_API_KEY" in getattr(item, "text", "")
                        for item in search.content
                    )

        errlog.seek(0)
        stderr = errlog.read()

    assert "Traceback" not in stderr
