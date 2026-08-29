import ast
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest

from arb_memory.mcp.config import mcp_role_name


def _mcp_dsn(schema):
    dsn = os.environ.get("ARB_MEMORY_MCP_DSN")
    if dsn is None:
        dsn = os.environ["ARB_MEMORY_DSN"].replace(
            "arb_memory:", f"{mcp_role_name()}:"
        )
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def test_mcp_package_does_not_import_redis_or_valkey():
    mcp_dir = Path(__file__).parents[2] / "src" / "arb_memory" / "mcp"
    forbidden = {"redis", "valkey"}
    offenders = []
    for path in mcp_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".", 1)[0]}
            else:
                continue
            if names & forbidden:
                offenders.append(str(path.relative_to(mcp_dir)))
    assert offenders == []


def test_mcp_server_runtime_import_does_not_load_redis_or_valkey():
    code = """
import importlib
import json
import sys

importlib.import_module("arb_memory.mcp.server")
print(json.dumps(sorted(name for name in ("redis", "valkey") if name in sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == "[]"


def test_mcp_role_positive_control_denies_memory_write(scratch, fake_embed):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    with psycopg.connect(_mcp_dsn(schema)) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == mcp_role_name()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO hints (text, embedding, content_hash) VALUES (%s, %s, %s)",
                ("positive control", fake_embed("positive control"), "positive-control-hash"),
            )
