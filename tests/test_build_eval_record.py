from agent_redis_bridge.bridge import build_eval_record
from agent_redis_bridge.eval_tee import EVAL_SCHEMA_VERSION


def test_build_eval_record_stamps_schema_version():
    rec = build_eval_record(
        run_id="run-1", task_id="task-1", seat_id="codex-x",
        event="turn-end", sent_at="2026-06-25T00:00:00+00:00", data={},
    )
    assert rec is not None
    assert rec["schema_version"] == EVAL_SCHEMA_VERSION == "1"


def test_build_eval_record_includes_orchestrator():
    rec = build_eval_record(
        run_id="run-1", task_id="task-1", seat_id="codex-x",
        event="turn-end", sent_at="2026-06-25T00:00:00+00:00", data={},
        orchestrator="claude-bridge-dev",
    )
    assert rec is not None
    assert rec["orchestrator"] == "claude-bridge-dev"


def test_build_eval_record_none_without_run_id_unaffected():
    assert build_eval_record(
        run_id="", task_id="t", seat_id="s",
        event="turn-end", sent_at="2026-06-25T00:00:00+00:00", data={},
    ) is None
