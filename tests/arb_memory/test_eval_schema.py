def test_eval_tables_and_unique_constraint(scratch):
    cols = {r[0] for r in scratch.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='eval_event_raw'"
    ).fetchall()}
    assert {"run_id", "task_id", "seat_id", "event_type", "sent_at", "payload",
            "stream_entry_id", "inserted_at"} <= cols
    # stream_entry_id unique
    scratch.execute(
        "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
        "VALUES ('r','t','task_started', now(), '{}', 'e1')"
    )
    dup_ok = scratch.execute(
        "INSERT INTO eval_event_raw (run_id, task_id, event_type, sent_at, payload, stream_entry_id) "
        "VALUES ('r','t','task_started', now(), '{}', 'e1') ON CONFLICT (stream_entry_id) DO NOTHING "
        "RETURNING id"
    ).fetchone()
    assert dup_ok is None  # conflict -> no row


def test_eval_deadletter_exists(scratch):
    cols = {r[0] for r in scratch.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='eval_deadletter'"
    ).fetchall()}
    assert {"run_id", "task_id", "event_type", "raw_entry", "error"} <= cols
