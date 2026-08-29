from agent_redis_bridge.claude_tail.lifecycle import Lifecycle


def test_started_emits_once():
    lc = Lifecycle()
    assert lc.started() == {"event_type": "task_started", "data": {}}
    assert lc.started() is None


def test_finished_event_defaults_ok_true():
    assert Lifecycle().finished() == {"event_type": "task_finished", "data": {"ok": True}}


def test_finished_event_accepts_ok_false():
    assert Lifecycle().finished(ok=False) == {"event_type": "task_finished", "data": {"ok": False}}


def test_started_state_is_per_instance():
    first = Lifecycle()
    second = Lifecycle()
    assert first.started() == {"event_type": "task_started", "data": {}}
    assert first.started() is None
    assert second.started() == {"event_type": "task_started", "data": {}}
