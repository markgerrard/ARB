from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from arb_memory import run as run_module
from arb_memory.mcp.local_server import build_local_server
from arb_memory.mcp.read_tools import LocalReadSettings


class FakeConn:
    closed = False


def fake_embed(text):
    return [0.0] * 1536


def test_local_read_entrypoint_rejects_cross_store_before_startup(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@dev-db:5544/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "postgresql://arbmem_local_reader@prod-db:25060/arb_memory")
    monkeypatch.delenv("ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE", raising=False)

    def build_should_not_run(*args, **kwargs):
        raise AssertionError("server startup should not run before DSN policy passes")

    monkeypatch.setattr("arb_memory.mcp.local_server.build_local_server", build_should_not_run)

    with pytest.raises(RuntimeError, match="does not match ARB_MEMORY_DSN"):
        run_module.run_local_read_mcp()


def test_local_read_entrypoint_allows_cross_store_with_opt_in(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://arb_memory@dev-db:5544/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "postgresql://arbmem_local_reader@prod-db:25060/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE", "1")
    observed = {}

    class FakeServer:
        def run(self, *, transport):
            observed["transport"] = transport

    def build_local(settings, **kwargs):
        observed["dsn"] = settings.dsn
        return FakeServer()

    monkeypatch.setattr("arb_memory.mcp.local_server.build_local_server", build_local)

    run_module.run_local_read_mcp()

    assert observed == {
        "dsn": "postgresql://arbmem_local_reader@prod-db:25060/arb_memory",
        "transport": "stdio",
    }


def test_local_read_entrypoint_requires_local_dsn(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)

    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN is missing/empty"):
        run_module.run_local_read_mcp()


@pytest.mark.anyio
async def test_local_server_registers_only_read_tools():
    server = build_local_server(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=FakeConn,
        embed=fake_embed,
    )

    names = {tool.name for tool in await server.list_tools()}

    assert names == {
        "memory_search", "memory_get", "memory_recent",
        "memory_related", "memory_references",
    }
    assert "memory_store" not in names
    assert "memory_remember" not in names


@pytest.mark.anyio
async def test_local_server_registers_graph_tools():
    server = build_local_server(LocalReadSettings(dsn="postgresql://ignored"),
                                conn_factory=FakeConn, embed=fake_embed)
    names = {t.name for t in await server.list_tools()}
    assert {"memory_related", "memory_references"} <= names


@pytest.mark.anyio
async def test_stdio_search_error_is_structured_without_traceback():
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{os.getcwd()}:{os.getcwd()}/src"
    env["ARB_MEMORY_LOCAL_DSN"] = "postgresql://local-reader-test.invalid/arb_memory"
    env.pop("ARB_MEMORY_DSN", None)
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
                result = await session.call_tool("memory_search", {"query": "anything", "k": 1})

        errlog.seek(0)
        stderr = errlog.read()
    assert result.isError is True
    assert "Traceback" not in stderr


@pytest.mark.anyio
async def test_stdio_child_exits_and_closes_pg_connection(tmp_path):
    pid_file = tmp_path / "child.pid"
    closed_file = tmp_path / "conn.closed"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        textwrap.dedent(
            f"""
            import os
            from pathlib import Path
            import psycopg

            class FakeRows:
                def fetchall(self):
                    return []

            class FakeConn:
                closed = False

                def __init__(self):
                    Path({str(pid_file)!r}).write_text(str(os.getpid()))

                def execute(self, *args, **kwargs):
                    return FakeRows()

                def close(self):
                    self.closed = True
                    Path({str(closed_file)!r}).write_text("closed")

            psycopg.connect = lambda *args, **kwargs: FakeConn()
            """
        )
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{tmp_path}:{os.getcwd()}:{os.getcwd()}/src"
    env["ARB_MEMORY_LOCAL_DSN"] = "postgresql://local-reader-test.invalid/arb_memory"
    env.pop("ARB_MEMORY_DSN", None)
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
                result = await session.call_tool("memory_recent", {"limit": 1})

    assert result.isError is False
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"stdio child process {pid} was not reaped")
    assert closed_file.read_text() == "closed"
