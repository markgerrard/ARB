from arb_memory.transcript import (
    TRANSCRIPT_GROUP,
    PostgresTranscriptSink,
    TranscriptConsumer,
    _parse_apply_patch,
    purge_expired,
)


def _xadd(redis, stream, **fields):
    return redis.xadd(stream, fields)


def _drain(consumer):
    n = 0
    while consumer.step() is not None:
        n += 1
    return n


def _make(redis_bus, conn_factory):
    prefix = redis_bus.prefix
    stream = f"{prefix}arbmem:trace"
    consumer = TranscriptConsumer(redis_bus, conn_factory, prefix=prefix, block_ms=50)
    return stream, consumer


def test_transcript_event_lands_with_stream_entry_id(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="run-1",
        task_id="task-1",
        seat_id="codex",
        orchestrator="claude",
        turn_index="0",
        item_id="turn-1:text",
        seq="1",
        kind="model_text",
        tool_name="",
        content="sentinel content",
        ts="2026-06-25T00:00:00+00:00",
    )

    _drain(consumer)

    conn = conn_factory()
    row = conn.execute(
        """
        SELECT run_id, task_id, kind, content, stream_entry_id
        FROM transcript_io
        WHERE stream_entry_id = %s
        """,
        (eid,),
    ).fetchone()
    assert row == ("run-1", "task-1", "model_text", "sentinel content", eid)


def test_transcript_redelivery_is_idempotent(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="run-dup",
        task_id="task-dup",
        seat_id="codex",
        turn_index="0",
        item_id="turn:text",
        seq="1",
        kind="model_text",
        content="once",
        ts="2026-06-25T00:00:00+00:00",
    )
    _drain(consumer)

    conn = conn_factory()
    sink = PostgresTranscriptSink()
    result = sink.write(
        conn,
        {
            "run_id": "run-dup",
            "task_id": "task-dup",
            "seat_id": "codex",
            "orchestrator": None,
            "turn_index": 0,
            "item_id": "turn:text",
            "seq": 1,
            "kind": "model_text",
            "tool_name": None,
            "content": "twice",
            "ts": "2026-06-25T00:00:00+00:00",
            "meta": {},
            "stream_entry_id": eid,
        },
    )

    assert result == "duplicate"
    assert conn.execute("SELECT count(*) FROM transcript_io WHERE run_id='run-dup'").fetchone()[0] == 1


def test_transcript_event_with_empty_run_id_persists_by_task_id(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="",
        task_id="task-no-run",
        seat_id="codex",
        turn_index="0",
        item_id="turn:text",
        seq="1",
        kind="model_text",
        content="watchable without run id",
        ts="2026-06-25T00:00:00+00:00",
    )

    assert consumer.step() == "written"

    conn = conn_factory()
    row = conn.execute(
        "SELECT run_id, task_id, content FROM transcript_io WHERE stream_entry_id = %s",
        (eid,),
    ).fetchone()
    assert row == ("", "task-no-run", "watchable without run id")
    assert (
        conn.execute("SELECT count(*) FROM transcript_deadletter WHERE stream_entry_id = %s", (eid,)).fetchone()[0]
        == 0
    )


def test_tool_name_only_event_with_empty_content_persists(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="",
        task_id="task-tool-only",
        seat_id="pi",
        turn_index="0",
        item_id="turn:read",
        seq="2",
        kind="Read",
        tool_name="Read",
        content="",
        ts="2026-06-25T00:00:00+00:00",
    )

    assert consumer.step() == "written"

    conn = conn_factory()
    row = conn.execute(
        "SELECT run_id, task_id, kind, tool_name, content FROM transcript_io WHERE stream_entry_id = %s",
        (eid,),
    ).fetchone()
    assert row == ("", "task-tool-only", "Read", "Read", "")
    assert (
        conn.execute("SELECT count(*) FROM transcript_deadletter WHERE stream_entry_id = %s", (eid,)).fetchone()[0]
        == 0
    )


def test_event_with_empty_content_and_empty_tool_name_deadletters(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="run-empty",
        task_id="task-empty",
        seat_id="pi",
        turn_index="0",
        item_id="turn:empty",
        seq="3",
        kind="Read",
        tool_name="",
        content="",
        ts="2026-06-25T00:00:00+00:00",
    )

    assert consumer.step() == "dead-lettered"

    conn = conn_factory()
    row = conn.execute(
        "SELECT stream_entry_id, error FROM transcript_deadletter WHERE stream_entry_id = %s",
        (eid,),
    ).fetchone()
    assert row[0] == eid
    assert "empty content and tool_name" in row[1]


def test_apply_patch_parse_counts_file_added_removed() -> None:
    command = """apply_patch <<'PATCH'
*** Begin Patch
*** Update File: foo.py
@@
-old
+new
+another
*** End Patch
PATCH"""

    assert _parse_apply_patch(command) == {"file": "foo.py", "added": 2, "removed": 1}
    assert _parse_apply_patch("pytest -q") == {}


def test_command_apply_patch_meta_is_persisted(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    command = """apply_patch <<'PATCH'
*** Begin Patch
*** Update File: foo.py
@@
-old
+new
*** End Patch
PATCH"""
    eid = _xadd(
        redis_bus,
        stream,
        run_id="run-patch",
        task_id="task-patch",
        seat_id="codex",
        turn_index="0",
        item_id="cmd-1",
        seq="7",
        kind="command_started",
        tool_name="apply_patch",
        content=command,
        ts="2026-06-25T00:00:00+00:00",
    )
    _drain(consumer)

    conn = conn_factory()
    meta = conn.execute("SELECT meta FROM transcript_io WHERE stream_entry_id = %s", (eid,)).fetchone()[0]
    assert meta == {"file": "foo.py", "added": 1, "removed": 1}


def test_missing_required_field_deadletters(redis_bus, conn_factory):
    stream, consumer = _make(redis_bus, conn_factory)
    eid = _xadd(
        redis_bus,
        stream,
        run_id="run-bad",
        item_id="item",
        seq="1",
        kind="model_text",
        content="bad",
        ts="2026-06-25T00:00:00+00:00",
    )

    assert consumer.step() == "dead-lettered"
    conn = conn_factory()
    row = conn.execute(
        "SELECT stream_entry_id, error FROM transcript_deadletter WHERE stream_entry_id = %s",
        (eid,),
    ).fetchone()
    assert row[0] == eid
    assert "missing task_id" in row[1]
    assert redis_bus.xpending(stream, TRANSCRIPT_GROUP)["pending"] == 0


def test_purge_expired_deletes_old_rows_in_batches(scratch):
    scratch.execute(
        """
        INSERT INTO transcript_io
            (run_id, task_id, turn_index, item_id, seq, kind, content, ts, stream_entry_id, inserted_at)
        VALUES
            ('old', 'task-old', 0, 'old:item', 1, 'model_text', 'old', now() - interval '10 days', 'old-1', now() - interval '10 days'),
            ('new', 'task-new', 0, 'new:item', 1, 'model_text', 'new', now(), 'new-1', now())
        """
    )

    deleted = purge_expired(scratch, older_than_days=7, batch_size=1)

    assert deleted == 1
    assert scratch.execute("SELECT count(*) FROM transcript_io WHERE run_id='old'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM transcript_io WHERE run_id='new'").fetchone()[0] == 1
