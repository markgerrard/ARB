from datetime import datetime, timezone

import anyio

from arb_memory.visibility import _apply_status_backfill, _reduce_seat


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.hgets = []

    def hget(self, key, field):
        self.hgets.append((key, field))
        return self.values.get((key, field))


def test_status_hash_backfill_reads_stalled_at_by_task_id():
    redis = FakeRedis({("agent_scratch:task:t1:status", "stalled_at"): "2026-07-07T12:00:00+00:00"})
    seat = {"task_id": "t1", "state": "running"}

    anyio.run(_apply_status_backfill, seat, redis, "agent_scratch:")

    assert redis.hgets == [("agent_scratch:task:t1:status", "stalled_at")]
    assert seat["stalled_at"] == "2026-07-07T12:00:00+00:00"


def test_status_hash_backfill_clears_absent_stalled_at():
    redis = FakeRedis({})
    seat = {"task_id": "t1", "state": "running", "stalled_at": "2026-07-07T12:00:00+00:00"}

    anyio.run(_apply_status_backfill, seat, redis, "agent_scratch:")

    assert "stalled_at" not in seat


def test_reduce_seat_carries_explicit_stalled_at_without_changing_running_state():
    fresh = datetime.now(timezone.utc).isoformat()
    seat = _reduce_seat(
        {"state": "running"},
        {
            "task_id": "t1",
            "seat_id": "codex-1",
            "event_type": "stall_detected",
            "sent_at": fresh,
            "stalled_at": "2026-07-07T12:00:00+00:00",
        },
    )

    assert seat["state"] == "running"
    assert seat["stalled_at"] == "2026-07-07T12:00:00+00:00"
