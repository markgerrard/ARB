from agent_redis_bridge.bridge import panel_input_lock_reason


def test_panel_input_lock_reason():
    assert panel_input_lock_reason({"audit_vote_expected": True}) == "panel_task_input_locked"
    assert panel_input_lock_reason({"panel_input_locked": True}) == "panel_task_input_locked"
    assert panel_input_lock_reason({"certifying": True}) == "panel_task_input_locked"
    assert panel_input_lock_reason({}) is None
