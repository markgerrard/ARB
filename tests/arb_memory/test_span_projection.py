import pytest

import arb_memory.eval as eval_module
from arb_memory.eval import SPAN_COUNTERS, PostgresEvalSink
from arb_memory.eval import project_spans

def _event(entry, event_type, payload=None, *, epoch=1, seat="codex", sent="2026-07-15T00:00:00+00:00"):
    return {
        "run_id": "span-run",
        "task_id": "span-task",
        "seat_id": seat,
        "orchestrator": "test",
        "event_type": event_type,
        "schema_version": "1",
        "sent_at": sent,
        "payload": {"attempt_epoch": epoch, **(payload or {})} if epoch is not None else (payload or {}),
        "stream_entry_id": entry,
    }


def _write(conn, event):
    return PostgresEvalSink().write(conn, event)


def test_first_non_task_event_establishes_epoch_ledger(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}))
    assert conn.execute(
        "SELECT attempt_epoch FROM eval_task WHERE run_id='span-run' AND task_id='span-task'"
    ).fetchone()[0] == 1


def test_stale_epoch_cannot_insert_ghost_row(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 1}, epoch=1))
    _write(conn, _event("2", "turn_started", {"turn_index": 3}, epoch=1))
    _write(conn, _event("3", "turn_started", {"turn_index": 1}, epoch=2))
    _write(conn, _event("4", "turn_started", {"turn_index": 3}, epoch=1))
    assert conn.execute(
        "SELECT count(*) FROM eval_turn WHERE run_id='span-run' AND task_id='span-task'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT attempt_epoch FROM eval_turn WHERE run_id='span-run' AND task_id='span-task'"
    ).fetchone()[0] == 2


def test_raw_and_span_projection_rollback_together_on_projection_failure(conn_factory, monkeypatch):
    conn = conn_factory()

    def fail_projection(_conn, _event):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(eval_module, "project_spans", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        PostgresEvalSink().write(conn, _event("atomic", "turn_started", {"turn_index": 0}))
    assert conn.execute("SELECT count(*) FROM eval_event_raw").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM eval_turn").fetchone()[0] == 0


def test_epoch_bump_replaces_previous_attempt_and_redelivery_is_noop(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, epoch=1))
    _write(conn, _event("2", "command_started", {"turn_index": 0, "tool_call_id": "old"}, epoch=1))
    _write(conn, _event("3", "turn_started", {"turn_index": 0}, epoch=2))
    assert conn.execute("SELECT count(*) FROM eval_turn WHERE attempt_epoch=1").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM eval_tool_call WHERE attempt_epoch=1").fetchone()[0] == 0
    _write(conn, _event("3", "turn_started", {"turn_index": 0}, epoch=2))
    assert conn.execute("SELECT count(*) FROM eval_turn").fetchone()[0] == 1


def test_redelivered_start_does_not_regress_completion(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, sent="2026-07-15T00:00:00+00:00"))
    _write(conn, _event("2", "turn_completed", {"turn_index": 0, "ok": True},
                        sent="2026-07-15T00:00:02+00:00"))
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, sent="2026-07-15T00:00:00+00:00"))
    row = conn.execute(
        "SELECT completed_at, outcome FROM eval_turn WHERE run_id='span-run' AND task_id='span-task'"
    ).fetchone()
    assert row[0] is not None and row[1] == "finished"


def test_equal_epoch_completion_redelivery_does_not_rewrite_completion_fields(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, sent="2026-07-15T00:00:00+00:00"))
    _write(conn, _event("2", "turn_completed", {"turn_index": 0, "ok": True}, sent="2026-07-15T00:00:02+00:00"))
    first = conn.execute(
        "SELECT completed_at, latency_ms, outcome, close_basis, ok FROM eval_turn"
    ).fetchone()
    _write(conn, _event("3", "turn_completed", {"turn_index": 0, "ok": False}, sent="2026-07-15T00:00:03+00:00"))
    assert conn.execute(
        "SELECT completed_at, latency_ms, outcome, close_basis, ok FROM eval_turn"
    ).fetchone() == first


def test_absent_epoch_skips_all_span_projection_and_counts():
    before = SPAN_COUNTERS["span_skipped_no_epoch"]
    # The counter path is independent of PostgreSQL and is deliberately exercised
    # before the live fixture tests.
    assert project_spans(None, _event("no-epoch", "task_started", epoch=None)) == "skipped-no-epoch"
    assert SPAN_COUNTERS["span_skipped_no_epoch"] == before + 1


def test_tool_finish_pairs_by_id_and_rollups_once(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}))
    _write(conn, _event("2", "command_started", {
        "turn_index": 0, "tool_call_id": "tool-1", "tool_name": "shell",
    }, sent="2026-07-15T00:00:00+00:00"))
    finish = _event("3", "command_finished", {
        "tool_call_id": "tool-1", "ok": True, "exit_code": 0,
    }, sent="2026-07-15T00:00:02+00:00")
    _write(conn, finish)
    _write(conn, finish)
    assert conn.execute(
        "SELECT outcome, latency_ms FROM eval_tool_call WHERE tool_call_id='tool-1'"
    ).fetchone() == ("finished", 2000)
    assert conn.execute("SELECT tool_call_count FROM eval_turn").fetchone()[0] == 1
    assert conn.execute("SELECT tool_call_count FROM eval_task").fetchone()[0] == 1


def test_task_finish_derives_only_open_children(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, seat="pi_rpc"))
    _write(conn, _event("2", "turn_completed", {"turn_index": 0}, seat="pi_rpc"))
    _write(conn, _event("3", "turn_started", {"turn_index": 1}, seat="pi_rpc"))
    _write(conn, _event("4", "task_finished", {"ok": True}, seat="pi_rpc"))
    rows = conn.execute(
        "SELECT turn_index, outcome, close_basis FROM eval_turn ORDER BY turn_index"
    ).fetchall()
    assert rows == [(0, "finished", "turn_completed"), (1, "finished", "task_finish_derived")]


def test_claude_epoch_one_is_not_stale_or_superseding(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, seat="claude"))
    _write(conn, _event("2", "turn_started", {"turn_index": 1}, seat="claude"))
    assert conn.execute("SELECT count(*) FROM eval_turn").fetchone()[0] == 2


def test_dispatch_latency_uses_sent_at_and_timeout_basis(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0},
                        sent="2026-07-15T00:00:00+00:00"))
    _write(conn, _event("2", "turn_completed", {"turn_index": 0, "ok": True},
                        sent="2026-07-15T00:00:03+00:00"))
    row = conn.execute("SELECT latency_ms, latency_basis, close_basis, outcome FROM eval_turn").fetchone()
    assert row == (3000, "sent_at", "turn_completed", "finished")


def test_turn_timeout_closes_timeout_with_timeout_basis(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}, sent="2026-07-15T00:00:00+00:00"))
    _write(conn, _event("2", "turn_timeout", {"turn_index": 0}, sent="2026-07-15T00:00:03+00:00"))
    assert conn.execute(
        "SELECT latency_ms, latency_basis, close_basis, outcome FROM eval_turn"
    ).fetchone() == (3000, "sent_at", "turn_timeout", "timeout")


def test_claude_tail_completion_uses_stored_start_without_payload_start(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:00+00:00",
    }, seat="claude"))
    _write(conn, _event("2", "turn_completed", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:02+00:00",
        "turn_clock_monotonic": True,
    }, seat="claude"))
    assert conn.execute(
        "SELECT latency_ms, latency_basis, outcome FROM eval_turn"
    ).fetchone() == (2000, "event_ts", "finished")


def test_tool_event_clock_reversal_is_invalid_and_positive_latency_is_numeric(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "command_started", {
        "turn_index": 0, "tool_call_id": "tool-negative", "event_ts": "2026-07-15T00:00:02+00:00",
    }, seat="claude"))
    _write(conn, _event("2", "command_finished", {
        "tool_call_id": "tool-negative", "event_ts": "2026-07-15T00:00:01+00:00",
    }, seat="claude"))
    _write(conn, _event("3", "command_started", {
        "turn_index": 0, "tool_call_id": "tool-positive", "event_ts": "2026-07-15T00:00:02+00:00",
    }, seat="claude"))
    _write(conn, _event("4", "command_finished", {
        "tool_call_id": "tool-positive", "event_ts": "2026-07-15T00:00:04+00:00",
    }, seat="claude"))
    assert conn.execute(
        "SELECT outcome, latency_ms FROM eval_tool_call ORDER BY tool_call_id"
    ).fetchall() == [("clock_invalid", None), ("finished", 2000)]


def test_claude_invalid_clock_is_fail_closed_without_sent_at_fallback(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:00+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00",
    }, seat="claude", sent="2026-07-15T01:00:00+00:00"))
    _write(conn, _event("2", "turn_completed", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:02+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00",
        "turn_clock_monotonic": False,
    }, seat="claude", sent="2026-07-15T01:00:03+00:00"))
    assert conn.execute("SELECT latency_ms, outcome FROM eval_turn").fetchone() == (None, "clock_invalid")


def test_stall_event_never_closes_turn(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {"turn_index": 0}))
    _write(conn, _event("2", "stall_detected", {"turn_index": 0}))
    assert conn.execute("SELECT outcome, completed_at FROM eval_turn").fetchone() == ("open", None)


def test_finalized_recovers_turn_and_retracted_is_sticky(conn_factory):
    conn = conn_factory()
    _write(conn, _event("1", "turn_started", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:00+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00",
    }, seat="claude"))
    _write(conn, _event("2", "turn_finalized", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:04+00:00",
        "finality_evidence": "fd_quiescence",
    }, seat="claude"))
    assert conn.execute(
        "SELECT latency_ms, outcome, close_basis, finality_evidence FROM eval_turn"
    ).fetchone() == (4000, "recovered", "turn_finalized", "fd_quiescence")
    _write(conn, _event("3", "turn_finality_retracted", {"turn_index": 0}, seat="claude"))
    _write(conn, _event("4", "turn_finalized", {
        "turn_index": 0, "event_ts": "2026-07-15T00:00:05+00:00",
        "turn_started_ts": "2026-07-15T00:00:00+00:00",
    }, seat="claude"))
    assert conn.execute(
        "SELECT outcome, finality_evidence FROM eval_turn"
    ).fetchone() == ("clock_invalid", "retracted")
