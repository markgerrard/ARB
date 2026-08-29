from arb_memory.eval import purge_expired


def test_purge_expired_deletes_old_eval_rows_by_inserted_at_in_batches(scratch):
    scratch.execute(
        """
        INSERT INTO eval_event_raw
            (run_id, task_id, event_type, sent_at, payload, stream_entry_id, inserted_at)
        VALUES
            ('old', 'task-old-1', 'task_started', now(), '{}', 'old-1', now() - interval '40 days'),
            ('old', 'task-old-2', 'task_finished', now(), '{}', 'old-2', now() - interval '31 days'),
            ('new', 'task-new', 'task_started', now(), '{}', 'new-1', now() - interval '10 days')
        """
    )

    index_exists = scratch.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'eval_event_raw'
              AND indexname = 'eval_event_raw_inserted_at_idx'
        )
        """
    ).fetchone()[0]

    deleted = purge_expired(scratch, older_than_days=30, batch_size=1)

    assert index_exists is True
    assert deleted == 2
    assert scratch.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='old'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM eval_event_raw WHERE run_id='new'").fetchone()[0] == 1
