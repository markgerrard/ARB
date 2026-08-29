from agent_redis_bridge.eval_tee import extract_eval_payload, EVAL_ALLOWLIST


def test_extracts_only_allowlisted_keys():
    out = extract_eval_payload({
        "tool_name": "shell", "tool_call_count": 3, "stop_reason": "end_turn",
        "total_tokens": 1200, "latency_ms": 450, "ok": True,
    })
    assert out == {"tool_name": "shell", "tool_call_count": 3, "stop_reason": "end_turn",
                   "total_tokens": 1200, "latency_ms": 450, "ok": True}


def test_command_output_is_excluded_by_construction():
    # DENY-PROOF: raw tool I/O must NEVER ride the tee
    out = extract_eval_payload({
        "tool_name": "shell",
        "command": "cat /etc/passwd; export SECRET=hunter2",
        "command_output": "root:x:0:0:...",
        "output": "stdout text",
        "model_text": "the assistant said...",
    })
    assert "command" not in out
    assert "command_output" not in out
    assert "output" not in out
    assert "model_text" not in out
    assert out == {"tool_name": "shell"}


def test_unknown_key_absent_by_construction():
    out = extract_eval_payload({"surprise_new_field": "leak", "tool_name": "x"})
    assert "surprise_new_field" not in out


def test_allowlist_has_no_raw_io_keys():
    for forbidden in ("command", "command_output", "output", "model_text", "model_thinking",
                      "args", "stdin", "status", "summary", "error"):
        assert forbidden not in EVAL_ALLOWLIST


def test_free_text_task_finished_fields_excluded():
    # task_finished carries {ok, summary, error}: keep `ok`, drop the free-text summary/error
    out = extract_eval_payload({"ok": True, "summary": "did the thing; here is secret context", "error": None})
    assert out == {"ok": True}


def test_slice5a0_new_allowlist_members_pass_through():
    out = extract_eval_payload({
        "tool_call_id": "call_1",
        "attempt_epoch": 2,
        "event_ts": "2026-07-13T19:42:50.294Z",
        "turn_started_ts": "2026-07-13T19:42:40.000Z",
        "turn_clock_monotonic": True,
        "turn_index": 4,
    })
    assert out == {
        "tool_call_id": "call_1",
        "attempt_epoch": 2,
        "event_ts": "2026-07-13T19:42:50.294Z",
        "turn_started_ts": "2026-07-13T19:42:40.000Z",
        "turn_clock_monotonic": True,
        "turn_index": 4,
    }


def test_slice5a0_new_members_are_bounded_scalars_not_free_text():
    out = extract_eval_payload({
        "event_ts": "2026-07-13T19:42:50.294Z",
        "message": "assistant said a secret",
        "thinking": "chain of thought",
    })
    assert out == {"event_ts": "2026-07-13T19:42:50.294Z"}


def test_slice5a_finality_fields_pass_and_unknown_fields_still_drop():
    out = extract_eval_payload({
        "finality_evidence": "fd_quiescence", "observed_inode": 42, "observed_size": 99,
        "raw_transcript": "must not pass",
    })
    assert out == {"finality_evidence": "fd_quiescence", "observed_inode": 42, "observed_size": 99}
