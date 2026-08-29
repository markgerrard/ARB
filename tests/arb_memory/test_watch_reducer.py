from datetime import datetime, timedelta, timezone
import json

from arb_memory.visibility import STALE_GRACE_S as VISIBILITY_STALE_GRACE_S
from arb_memory.visibility import format_timeline_summary as visibility_format_timeline_summary
from arb_memory.visibility import _reduce_seat as visibility_reduce_seat
from arb_memory.watch.reducer import format_timeline_summary
from arb_memory.watch.reducer import format_timeline_event
from arb_memory.watch.reducer import reduce_seat


def _entry(task_id, seat_id, event_type, *, sent_at=None, data=None):
    return {
        "run_id": "run-1",
        "task_id": task_id,
        "seat_id": seat_id,
        "orchestrator": "claude-bridge-dev",
        "event_type": event_type,
        "sent_at": sent_at or datetime.now(timezone.utc).isoformat(),
        "data": json.dumps(data or {}),
    }


def _assert_matches_visibility(state, entry):
    assert reduce_seat(state, entry) == visibility_reduce_seat(state, entry)


def test_reduce_seat_matches_visibility_lifecycle_vote_and_stale():
    state = {}
    for entry in [
        _entry("t1", "codex", "task_started"),
        _entry("t1", "codex", "task_continuing"),
        _entry("t1", "codex", "vote", data={"stance": "approve"}),
        _entry("t1", "codex", "task_finished", data={"ok": True}),
    ]:
        _assert_matches_visibility(state, entry)
        state = reduce_seat(state, entry)

    _assert_matches_visibility(
        {"task_id": "t2", "seat_id": "agy", "state": "running"},
        _entry("t2", "agy", "task_finished", data={"ok": False}),
    )

    old = (datetime.now(timezone.utc) - timedelta(seconds=VISIBILITY_STALE_GRACE_S + 1)).isoformat()
    _assert_matches_visibility({"task_id": "t-old", "state": "running", "last_event_ts": old}, {})


def test_reduce_seat_matches_visibility_terminal_state_stays_terminal_on_late_vote():
    done = reduce_seat({}, _entry("t3", "pi", "task_finished", data={"ok": True}))
    failed = reduce_seat({}, _entry("t4", "kimi", "task_finished", data={"ok": False}))

    _assert_matches_visibility(done, _entry("t3", "pi", "vote", data={"stance": "reject"}))
    _assert_matches_visibility(failed, _entry("t4", "kimi", "vote", data={"stance": "approve"}))


def test_transcript_timeline_summary_matches_visibility_for_new_kinds():
    samples = [
        {
            "source": "transcript",
            "ts": "2026-06-25T10:00:00+00:00",
            "kind": "model_text",
            "content": "hello ‹redacted›",
        },
        {
            "source": "transcript",
            "ts": "2026-06-25T10:00:01+00:00",
            "kind": "model_thinking",
            "content": "checking plan",
        },
        {
            "source": "transcript",
            "ts": "2026-06-25T10:00:02+00:00",
            "kind": "command_finished",
            "tool_name": "apply_patch",
            "content": "patch",
            "meta": {"file": "foo.py", "added": 3, "removed": 1},
        },
        {
            "source": "transcript",
            "ts": "2026-06-25T10:00:03+00:00",
            "kind": "command_output",
            "tool_name": "bash",
            "content": "$ pytest\npassed",
        },
    ]

    assert [format_timeline_summary(sample) for sample in samples] == [
        visibility_format_timeline_summary(sample) for sample in samples
    ]


def test_tui_transcript_timeline_uses_thinking_and_diff_affordances():
    thinking = {
        "source": "transcript",
        "ts": "2026-06-25T10:00:01+00:00",
        "kind": "model_thinking",
        "content": "checking plan",
    }
    patch = {
        "source": "transcript",
        "ts": "2026-06-25T10:00:02+00:00",
        "kind": "command_finished",
        "tool_name": "apply_patch",
        "content": "patch",
        "meta": {"file": "foo.py", "added": 3, "removed": 1},
    }

    assert format_timeline_event(thinking) == (
        "2026-06-25T10:00:01+00:00 transcript model_thinking [dim][thinking][/dim]\nchecking plan"
    )
    assert format_timeline_event(patch) == (
        "2026-06-25T10:00:02+00:00 transcript command_finished edited `foo.py` +3/-1\n[dim]diff[/dim]\npatch"
    )


def test_non_codex_tool_name_renders_even_when_kind_is_tool_name():
    event = {
        "source": "transcript",
        "ts": "2026-06-25T10:00:04+00:00",
        "kind": "Read",
        "tool_name": "Read",
        "content": "",
    }

    assert visibility_format_timeline_summary(event) == "2026-06-25T10:00:04+00:00 transcript Read Read"
    assert format_timeline_summary(event) == "2026-06-25T10:00:04+00:00 transcript Read Read"
    assert format_timeline_event(event) == "2026-06-25T10:00:04+00:00 transcript Read Read"
