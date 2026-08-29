"""AC10: hash-preimage disclosure must be served in registered tool descriptions."""

from __future__ import annotations

import anyio

from arb_memory.mcp.local_server import build_local_server
from arb_memory.mcp.read_tools import LocalReadSettings


class _FakeConn:
    closed = False


def _tool_descriptions(server) -> dict[str, str]:
    tools = anyio.run(server.list_tools)
    return {t.name: (t.description or "") for t in tools}


def test_local_server_memory_get_description_discloses_hash_preimage():
    server = build_local_server(
        LocalReadSettings(dsn="postgresql://ignored"),
        conn_factory=_FakeConn,
        embed=lambda t: [0.0] * 1536,
    )
    descs = _tool_descriptions(server)
    assert "arbmem:artefact:v1" in descs["memory_get"]


def test_public_server_descriptions_disclose_hash_preimage():
    """AC10 public half: list_tools opens no DB — stub conn_factory, no scratch/DSN."""
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server
    from arb_memory.mcp.config import Settings

    settings = Settings(
        public_base_url="https://mem.example.com",
        mcp_dsn="postgresql://example",
        login_secret="passphrase",
        totp_secret="totp",
    )
    # list_tools() never opens a connection; a stub factory keeps the suite DSN-less.
    conn_factory = _FakeConn
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=conn_factory)
    server = build_server(
        settings=settings,
        provider=provider,
        conn_factory=conn_factory,
        embed=lambda t: [0.0] * 1536,
    )
    descs = _tool_descriptions(server)
    assert "arbmem:artefact:v1" in descs["memory_get"]
    assert "arbmem:artefact:v1" in descs["memory_store"]
