from arb_memory.eval import PostgresEvalSink, purge_expired as purge_eval_expired
from arb_memory.transcript import purge_expired


def _eval_event(entry):
    return {
        "run_id": "purge-run", "task_id": "purge-task", "seat_id": "codex",
        "orchestrator": "test", "event_type": "turn_started", "sent_at": "2026-07-15T00:00:00+00:00",
        "payload": {"attempt_epoch": 1, "turn_index": 0}, "stream_entry_id": entry,
    }


def test_transcript_purge_uses_ingestion_time_and_index(scratch):
    scratch.execute(
        """
        INSERT INTO transcript_io
            (run_id, task_id, item_id, kind, content, ts, stream_entry_id, inserted_at)
        VALUES
            ('r', 'recent-ingest', 'i1', 'text', 'old producer time', now() - interval '90 days', 'recent', now()),
            ('r', 'old-ingest', 'i2', 'text', 'recent producer time', now(), 'old', now() - interval '90 days')
        """
    )
    assert scratch.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_indexes WHERE schemaname=current_schema()
            AND tablename='transcript_io' AND indexname='transcript_io_inserted_at_idx'
        )
        """
    ).fetchone()[0] is True
    assert purge_expired(scratch, older_than_days=30) == 1
    assert scratch.execute("SELECT count(*) FROM transcript_io WHERE stream_entry_id='recent'").fetchone()[0] == 1
    assert scratch.execute("SELECT count(*) FROM transcript_io WHERE stream_entry_id='old'").fetchone()[0] == 0


def test_eval_raw_purge_leaves_derived_span_rows(scratch):
    PostgresEvalSink().write(scratch, _eval_event("raw-old"))
    scratch.execute("UPDATE eval_event_raw SET inserted_at=now() - interval '90 days'")
    assert purge_eval_expired(scratch, older_than_days=30) == 1
    assert scratch.execute(
        "SELECT count(*) FROM eval_turn WHERE run_id='purge-run' AND task_id='purge-task'"
    ).fetchone()[0] == 1
