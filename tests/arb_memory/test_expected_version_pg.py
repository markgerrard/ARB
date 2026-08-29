"""Live-Postgres tests for B19c expected_version (AC4, AC16).

Requires ARB_MEMORY_DSN; silent skip is refused by scripts/graph-sql-gate (AC11).
"""

from __future__ import annotations

import os
import threading

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
from pgvector.psycopg import register_vector

from arb_memory import store as store_module
from arb_memory.store import upsert_artefact, write_artefact_and_hints


class _PauseAfterCompare:
    """Test-only conn wrapper: after the head compare SELECT, signal ready and wait.

    Lets two real upsert_artefact calls both pass the compare against head=1 before
    either inserts (deterministic production race for AC4 / R-1).
    """

    def __init__(self, conn, ready: threading.Event, go: threading.Event):
        self._conn = conn
        self._ready = ready
        self._go = go
        self._paused_once = False

    def execute(self, query, params=None):
        if params is None:
            result = self._conn.execute(query)
        else:
            result = self._conn.execute(query, params)
        if (
            not self._paused_once
            and isinstance(query, str)
            and "SELECT version, content_hash FROM artefacts" in query
        ):
            self._paused_once = True
            self._ready.set()
            if not self._go.wait(timeout=15):
                raise TimeoutError("release event not set after compare SELECT")
        return result

    def transaction(self):
        return self._conn.transaction()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _session_conn(dsn: str, schema: str):
    c = psycopg.connect(dsn)
    c.autocommit = True
    c.execute(f'SET search_path TO "{schema}", public')
    register_vector(c)
    c.autocommit = False
    return c


def test_toctou_unique_violation_then_refusal_on_retry(scratch, monkeypatch):
    """AC4/R-1: two real upserts race after both pass compare; loser is 23505 then refuse.

    Deterministic barrier: T2 pauses after the compare SELECT; T1 commits version 2;
    T2 resumes and INSERTs expected+1 (=2) → UniqueViolation sqlstate 23505; a fresh
    transaction then refuses against the moved head. Raw-SQL INSERT simulation is not
    acceptable evidence (R-2.1).

    ``_register_vector`` is no-op'd because pgvector's TypeInfo.fetch requires a real
    Connection (not the pause wrapper); both sessions already register_vector at setup.
    """
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    upsert_artefact(scratch, "doc", content="v1-body")

    dsn = os.environ["ARB_MEMORY_DSN"]
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]

    t1 = _session_conn(dsn, schema)
    t2_raw = _session_conn(dsn, schema)
    ready = threading.Event()
    go = threading.Event()
    t2 = _PauseAfterCompare(t2_raw, ready, go)
    held: list[BaseException] = []

    def t2_run():
        try:
            with t2.transaction():
                upsert_artefact(t2, "doc", content="t2-loses", expected_version=1)
        except BaseException as exc:  # noqa: BLE001 — capture for main thread
            held.append(exc)

    try:
        thread = threading.Thread(target=t2_run)
        thread.start()
        assert ready.wait(timeout=15), f"T2 never reached compare SELECT barrier; held={held!r}"

        with t1.transaction():
            aid1, v1, o1 = upsert_artefact(t1, "doc", content="t1-wins", expected_version=1)
            assert (aid1, v1, o1) == ("doc", 2, "stored")

        go.set()
        thread.join(timeout=15)
        assert not thread.is_alive(), f"T2 thread did not finish; held={held!r}"
        assert held, "T2 was expected to raise UniqueViolation; got success"
        exc = held[0]
        assert isinstance(exc, psycopg.errors.UniqueViolation), type(exc)
        assert exc.sqlstate == "23505"

        # Fresh transaction: compare sees head 2, refuses.
        with t2_raw.transaction():
            result = upsert_artefact(t2_raw, "doc", content="t2-loses", expected_version=1)
            assert result == ("doc", 2, "refused_version_mismatch")
    finally:
        go.set()  # unblock T2 if we failed mid-barrier
        t1.close()
        t2_raw.close()


def test_refusal_does_not_retire_winner_index_hint(scratch, fake_embed):
    """AC16: refusing write must not soft-delete the winner's artefact_index hint.

    Seed head at v2 *without* an index-hint retirement of v1's index, then refuse a
    stale guarded write that would carry an artefact_index hint. Observational head
    on refusal is 2; without the short-circuit, the retirement UPDATE would use
    artefact_version=2 and soft-delete the v1 index (deleted_at IS NOT NULL). With
    the short-circuit, deleted_at stays NULL. Failability was verified by temporarily
    removing the short-circuit (red observed, not committed).
    """
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "doc", "content": "winner-v1"},
        hints=[{
            "text": "winner index v1",
            "embedding": fake_embed("winner index v1"),
            "metadata": {"kind": "artefact_index", "artefact_id": "doc"},
        }],
    )
    # Advance head without writing a replacement index hint so v1's index stays live.
    upsert_artefact(scratch, "doc", content="winner-v2")
    live = scratch.execute(
        "SELECT deleted_at FROM hints WHERE artefact_id='doc' AND artefact_version=1"
        " AND metadata->>'kind' = 'artefact_index'"
    ).fetchone()
    assert live is not None and live[0] is None

    receipt = write_artefact_and_hints(
        scratch,
        artefact={
            "artefact_id": "doc",
            "content": "loser-body",
            "expected_version": 1,  # stale; head is 2 → refuse, observational version 2
        },
        hints=[{
            "text": "loser index",
            "embedding": fake_embed("loser index"),
            "metadata": {"kind": "artefact_index", "artefact_id": "doc"},
        }],
    )
    assert receipt["artefact_outcome"] == "refused_version_mismatch"
    assert receipt["version"] == 2
    assert receipt["hints_stored"] == 0

    still = scratch.execute(
        "SELECT deleted_at FROM hints WHERE artefact_id='doc' AND artefact_version=1"
        " AND metadata->>'kind' = 'artefact_index'"
    ).fetchone()
    assert still is not None and still[0] is None
