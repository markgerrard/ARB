"""S5: hint_read retention purge (TDD).

Frozen guide §8 — purge parents only; cascade hits; never touch deadletter.
"""
from __future__ import annotations

import inspect
import os
import uuid
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg import sql

from arb_memory import run
from arb_memory.hint_reads import purge_expired
from arb_memory.mcp.grants import apply_retention_grants

pytest_plugins = ("tests.arb_memory.conftest",)


def _insert_hint_read(scratch, *, served_at_sql: str, hit: bool = False) -> str:
    read_id = str(uuid.uuid4())
    scratch.execute(
        f"""
        INSERT INTO hint_read (read_id, door, outcome, k, hit_count, served_at)
        VALUES (%s, 'local', 'ok', 3, %s, {served_at_sql})
        """,
        (read_id, 1 if hit else 0),
    )
    if hit:
        scratch.execute(
            """
            INSERT INTO hint_read_hit (read_id, rank, hint_id)
            VALUES (%s, 1, 99)
            """,
            (read_id,),
        )
    return read_id


def _insert_deadletter(scratch) -> str:
    stream_entry_id = f"dl-{uuid.uuid4().hex}"
    scratch.execute(
        """
        INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error)
        VALUES (%s, '{}'::jsonb, 'poison')
        """,
        (stream_entry_id,),
    )
    return stream_entry_id


def test_purge_expired_deletes_rows_older_than_window_and_keeps_younger(scratch):
    old_id = _insert_hint_read(scratch, served_at_sql="now() - interval '40 days'")
    young_id = _insert_hint_read(scratch, served_at_sql="now() - interval '10 days'")

    deleted = purge_expired(scratch, older_than_days=30)

    assert deleted == 1
    assert (
        scratch.execute("SELECT count(*) FROM hint_read WHERE read_id = %s", (old_id,)).fetchone()[0]
        == 0
    )
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read WHERE read_id = %s", (young_id,)
        ).fetchone()[0]
        == 1
    )


def test_purge_expired_cascades_to_hint_read_hit(scratch):
    old_id = _insert_hint_read(
        scratch, served_at_sql="now() - interval '45 days'", hit=True
    )
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read_hit WHERE read_id = %s", (old_id,)
        ).fetchone()[0]
        == 1
    )

    deleted = purge_expired(scratch, older_than_days=30)

    assert deleted == 1
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read_hit WHERE read_id = %s", (old_id,)
        ).fetchone()[0]
        == 0
    )


def test_purge_expired_never_deletes_hint_read_deadletter(scratch):
    _insert_hint_read(scratch, served_at_sql="now() - interval '50 days'")
    dl_id = _insert_deadletter(scratch)

    purge_expired(scratch, older_than_days=30)

    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read_deadletter WHERE stream_entry_id = %s",
            (dl_id,),
        ).fetchone()[0]
        == 1
    )


def test_run_hint_read_purge_default_retention_days_is_30(monkeypatch):
    captured = {}

    def fake_purge(conn, older_than_days, *, batch_size=10000):
        captured["days"] = older_than_days
        return 0

    monkeypatch.delenv("ARB_HINT_READ_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(run, "_memory_conn", lambda: MagicMock())
    monkeypatch.setattr("arb_memory.hint_reads.purge_expired", fake_purge)
    # re-bind import path used inside run_hint_read_purge
    import arb_memory.hint_reads as hint_reads

    monkeypatch.setattr(hint_reads, "purge_expired", fake_purge)

    run.run_hint_read_purge()

    assert captured["days"] == 30


def test_run_hint_read_purge_env_overrides_default(monkeypatch):
    captured = {}

    def fake_purge(conn, older_than_days, *, batch_size=10000):
        captured["days"] = older_than_days
        return 0

    monkeypatch.setenv("ARB_HINT_READ_RETENTION_DAYS", "7")
    monkeypatch.setattr(run, "_memory_conn", lambda: MagicMock())
    import arb_memory.hint_reads as hint_reads

    monkeypatch.setattr(hint_reads, "purge_expired", fake_purge)

    run.run_hint_read_purge()

    assert captured["days"] == 7


def test_purge_expired_batch_size_default_is_10000():
    sig = inspect.signature(purge_expired)
    assert sig.parameters["batch_size"].default == 10000


def test_purge_expired_completes_across_multiple_batches(scratch):
    ids = [
        _insert_hint_read(scratch, served_at_sql="now() - interval '60 days'")
        for _ in range(5)
    ]
    young = _insert_hint_read(scratch, served_at_sql="now() - interval '5 days'")

    deleted = purge_expired(scratch, older_than_days=30, batch_size=2)

    assert deleted == 5
    remaining = scratch.execute(
        "SELECT count(*) FROM hint_read WHERE read_id = ANY(%s)", (ids,)
    ).fetchone()[0]
    assert remaining == 0
    assert (
        scratch.execute(
            "SELECT count(*) FROM hint_read WHERE read_id = %s", (young,)
        ).fetchone()[0]
        == 1
    )


def _retention_role(scratch):
    role = f"arb_hint_read_retention_{uuid.uuid4().hex}"
    try:
        scratch.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE")
    return role


def test_retention_role_can_delete_hint_read_via_set_role(scratch):
    """Real SET ROLE DELETE — not a catalog lookup."""
    role = _retention_role(scratch)
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    try:
        apply_retention_grants(scratch, role)
        old_id = _insert_hint_read(scratch, served_at_sql="now() - interval '90 days'")
        scratch.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role)))
        with psycopg.connect(os.environ["ARB_MEMORY_DSN"]) as role_conn:
            role_conn.autocommit = True
            role_conn.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
            )
            role_conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            deleted = purge_expired(role_conn, older_than_days=30)
        assert deleted == 1
        assert (
            scratch.execute(
                "SELECT count(*) FROM hint_read WHERE read_id = %s", (old_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_hint_read_purge_cli_dispatches_to_run_hint_read_purge(monkeypatch):
    called = {}

    def _handler():
        called["hint-read-purge"] = True

    monkeypatch.setattr(run, "run_hint_read_purge", _handler)
    assert run.main(["hint-read-purge"]) == 0
    assert called.get("hint-read-purge") is True
