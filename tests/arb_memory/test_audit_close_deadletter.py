from arb_memory.close import deadletter_malformed_close_request


def test_close_deadletter_real_insert_is_idempotent(scratch):
    fields = {
        "request_id": "req-malformed",
        "run_id": "run-malformed",
        "verdict": "not-json",
    }

    deadletter_malformed_close_request(scratch, "9-0", fields, ValueError("bad verdict"))
    deadletter_malformed_close_request(scratch, "9-0", fields, ValueError("bad verdict again"))

    row = scratch.execute(
        """
        SELECT request_id, run_id, raw_entry->>'verdict', error, stream_entry_id
        FROM audit_close_deadletter
        WHERE stream_entry_id = %s
        """,
        ("9-0",),
    ).fetchone()
    count = scratch.execute(
        "SELECT count(*) FROM audit_close_deadletter WHERE stream_entry_id = %s",
        ("9-0",),
    ).fetchone()[0]

    assert row == ("req-malformed", "run-malformed", "not-json", "bad verdict", "9-0")
    assert count == 1
