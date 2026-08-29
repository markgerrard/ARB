import os
import hashlib
import math
import time
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
from pgvector.psycopg import register_vector
from psycopg import sql

from arb_memory.mcp.config import mcp_role_name
from arb_memory.mcp.grants import apply_mcp_grants


EMBED_DIM = 1536


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: live smoke tests gated on external credentials")
    config.addinivalue_line("markers", "e2e: local end-to-end protocol tests")


def _fake_embed(text):
    vals = []
    for i in range(EMBED_DIM):
        digest = hashlib.sha256(f"{text}\0{i}".encode("utf-8")).digest()
        raw = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        vals.append((raw * 2.0) - 1.0)
    norm = math.sqrt(sum(v * v for v in vals))
    return [v / norm for v in vals]


@pytest.fixture
def fake_embed():
    return _fake_embed


@pytest.fixture
def make_slow_embed():
    return lambda base, delay: (lambda text: (time.sleep(delay), base(text))[1])


def _wait_until(fn, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def _row_exists(conn, artefact_id):
    return conn.execute(
        "SELECT count(*) FROM artefacts WHERE artefact_id = %s",
        (artefact_id,),
    ).fetchone()[0] > 0


def _deadlettered(conn, ulid):
    return conn.execute(
        "SELECT count(*) FROM write_deadletter WHERE ulid = %s",
        (ulid,),
    ).fetchone()[0] > 0


def _ensure_mcp_role(conn, role):
    exists = conn.execute(
        "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = %s)",
        (role,),
    ).fetchone()[0]
    if exists:
        return
    try:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()


@pytest.fixture
def scratch():
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        pytest.skip("no ARB_MEMORY_DSN")

    schema = f"arb_test_{uuid.uuid4().hex}"
    schema_sql = Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql"
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute(schema_sql.read_text(encoding="utf-8"))
        role = mcp_role_name()
        _ensure_mcp_role(conn, role)
        apply_mcp_grants(conn, role)
        yield conn
    finally:
        conn.rollback()
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()


@pytest.fixture
def empty_schema_conn():
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        pytest.skip("no ARB_MEMORY_DSN")

    schema = f"arb_test_empty_{uuid.uuid4().hex}"
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    try:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}", public')
        yield conn
    finally:
        conn.rollback()
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()


@pytest.fixture
def conn_factory(scratch):
    dsn = os.environ.get("ARB_MEMORY_DSN")
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    conns = []

    def make_conn():
        conn = psycopg.connect(dsn)
        conn.autocommit = True
        conn.execute(f'SET search_path TO "{schema}", public')
        register_vector(conn)
        conns.append(conn)
        return conn

    try:
        yield make_conn
    finally:
        for conn in conns:
            if conn.closed:
                continue
            conn.rollback()
            conn.close()


@pytest.fixture
def redis_bus():
    redis = pytest.importorskip("redis")
    url = os.environ.get("ARB_MEMORY_REDIS_URL", "redis://127.0.0.1:6379/15")
    client = redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("no redis")

    client.prefix = f"arbmem_test_{uuid.uuid4().hex}:"
    try:
        yield client
    finally:
        keys = []
        cursor = 0
        while True:
            cursor, batch = client.scan(cursor=cursor, match=f"{client.prefix}*", count=1000)
            keys.extend(batch)
            if cursor == 0:
                break
        if keys:
            client.delete(*keys)
        client.close()
