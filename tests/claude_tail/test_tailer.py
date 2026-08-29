import ast
import json
from pathlib import Path

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from agent_redis_bridge.claude_tail.identity import Identity, cold_identity, warm_identity
from agent_redis_bridge.claude_tail.mapper import DriftError
from agent_redis_bridge.claude_tail.offset import OffsetStore, Position, offset_key
import agent_redis_bridge.claude_tail.tailer as tailer_module
from agent_redis_bridge.claude_tail.tailer import TranscriptTailer, _DriftThresholdExceeded


class FakeRedis:
    def __init__(self, *, fail_xadd=False):
        self.values = {}
        self.xadds = []
        self.sets = []
        self.fail_xadd = fail_xadd

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        self.sets.append((key, value))

    def xadd(self, key, fields, **kwargs):
        if self.fail_xadd:
            raise RuntimeError("xadd failed")
        self.xadds.append((key, fields, kwargs))
        return f"{len(self.xadds)}-0"


def _write_jsonl(path, *objects):
    path.write_text("".join(json.dumps(obj, separators=(",", ":")) + "\n" for obj in objects), encoding="utf-8")


def _event_types(redis):
    return [fields.get("event_type") for key, fields, _ in redis.xadds if key.endswith("events:live")]


def _live_payloads(redis):
    return [json.loads(fields["data"]) for key, fields, _ in redis.xadds if key.endswith("events:live")]


def _trace_fields(redis):
    return [fields for key, fields, _ in redis.xadds if key.endswith("arbmem:trace")]


def _eval_fields(redis):
    return [fields for key, fields, _ in redis.xadds if key.endswith("eval:events")]


def _eval_payloads(redis):
    return [json.loads(fields["payload"]) for fields in _eval_fields(redis)]


def _xadd_keys(redis):
    return [key for key, _, _ in redis.xadds]


def _redactor(text):
    return text.replace("sk-abc", "REDACTED")


def test_event_ts_carries_transcript_timestamp_into_eval_payload(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=redis, trace_redis=redis, eval_redis=redis, eval_stream="eval:events",
        prefix="agent_scratch:", redactor=_redactor,
    )

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads, "expected eval edges from the tool_result"
    assert all(p.get("event_ts") == "2026-07-13T19:42:40.000Z" for p in payloads)


def test_event_ts_is_idempotent_across_a_byte_zero_reread(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    offset_redis = FakeRedis()
    store = OffsetStore(offset_redis, "p:")
    ident = Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o")

    def _new():
        return TranscriptTailer(str(transcript), ident, store, live_redis=redis, trace_redis=redis,
                                eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    _new().poll()
    first = [p["event_ts"] for p in _eval_payloads(redis) if "event_ts" in p]
    offset_redis.values.clear()
    _new().poll()
    all_ts = [p["event_ts"] for p in _eval_payloads(redis) if "event_ts" in p]
    assert first and all_ts[len(first):] == first


def test_absent_timestamp_omits_event_ts_and_bumps_counter(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="s", orchestrator="o"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=redis, trace_redis=redis, eval_redis=redis, eval_stream="eval:events",
        prefix="agent_scratch:", redactor=_redactor,
    )

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads, "expected eval edges"
    assert all("event_ts" not in p for p in payloads)
    assert tailer.claude_tail_missing_ts >= 1


def test_tool_edges_carry_same_canonical_tool_call_id(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "timestamp": "2026-07-13T19:42:41.000Z",
         "message": {"content": [{"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "pwd"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:42:42.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    tool_ids = [p["tool_call_id"] for p in _eval_payloads(redis) if "tool_call_id" in p]
    assert tool_ids, "expected tool edges with tool_call_id"
    assert set(tool_ids) == {"toolu_9"}


def test_attempt_epoch_is_constant_one_on_every_eval_event(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "timestamp": "2026-07-13T19:42:40.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript),
                              Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    payloads = _eval_payloads(redis)
    assert payloads
    assert all(p.get("attempt_epoch") == 1 for p in payloads)


def _eval_by_event(redis):
    return [(fields["event_type"], json.loads(fields["payload"])) for fields in _eval_fields(redis)]


def test_one_prompt_two_tool_rounds_is_one_logical_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "do it"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:02.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:03.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "b"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:04.000Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()
    by_event = _eval_by_event(redis)
    assert [e for e, _ in by_event].count("turn_started") == 1
    assert [e for e, _ in by_event].count("turn_completed") == 0
    started = next(p for e, p in by_event if e == "turn_started")
    assert started["turn_index"] == 1 and started["turn_started_ts"] == "2026-07-13T19:00:00.000Z"
    tool_edges = [p for e, p in by_event if e in ("command_started", "command_finished", "command_output")]
    assert tool_edges and all(p["turn_index"] == 1 for p in tool_edges)


def test_second_human_prompt_closes_turn_one_and_opens_turn_two(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "text", "text": "answer one"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z",
         "message": {"content": [{"type": "text", "text": "second"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()
    by_event = _eval_by_event(redis)
    completed = [p for e, p in by_event if e == "turn_completed"]
    started = [p for e, p in by_event if e == "turn_started"]
    assert len(completed) == 1 and completed[0]["turn_index"] == 1
    assert [p["turn_index"] for p in started] == [1, 2]
    assert completed[0]["event_ts"] == "2026-07-13T19:00:01.000Z"


def test_ismeta_and_sidechain_records_do_not_advance_logical_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z",
         "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "user", "isMeta": True, "promptId": "meta", "timestamp": "2026-07-13T19:00:00.500Z",
         "message": {"content": [{"type": "text", "text": "meta"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                              OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                              eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor)
    tailer.poll()
    by_event = _eval_by_event(redis)
    assert [e for e, _ in by_event].count("turn_started") == 1
    tool_edges = [p for e, p in by_event if e == "command_started"]
    assert tool_edges and all(p["turn_index"] == 1 for p in tool_edges)


def test_turn_index_is_restart_stable_across_nonzero_offset_resume(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, *[
        {"type": "user", "promptId": f"p{i}", "timestamp": f"2026-07-13T19:0{i}:00.000Z",
         "message": {"content": [{"type": "text", "text": f"prompt {i}"}]}} for i in range(1, 4)
    ])
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    ident = Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o")
    TranscriptTailer(str(transcript), ident, store, live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p4", "timestamp": "2026-07-13T19:04:00.000Z",
                             "message": {"content": [{"type": "text", "text": "prompt 4"}]}}) + "\n")
    redis2 = FakeRedis()
    TranscriptTailer(str(transcript), ident, store, live_redis=redis2, trace_redis=redis2,
                     eval_redis=redis2, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    starts = [p["turn_index"] for e, p in _eval_by_event(redis2) if e == "turn_started"]
    assert starts == [4]


def test_legacy_bare_int_offset_forces_recount_not_index_zero_resume(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
                 {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:01:00.000Z",
                  "message": {"content": [{"type": "text", "text": "one"}]}},
                 {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:02:00.000Z",
                  "message": {"content": [{"type": "text", "text": "two"}]}})
    redis = FakeRedis(); offset_redis = FakeRedis()
    ident = Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o")
    key = offset_key(str(transcript), transcript.stat().st_ino)
    offset_redis.set(f"p:claude:offset:{key}", "40")
    TranscriptTailer(str(transcript), ident, OffsetStore(offset_redis, "p:"), live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    starts = [p["turn_index"] for e, p in _eval_by_event(redis) if e == "turn_started"]
    assert starts == [1, 2]


def test_uuid_rides_live_data_for_correlation_but_never_reaches_eval(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "uuid": "u-abc", "timestamp": "2026-07-13T19:00:01.000Z",
                              "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}}]}})
    redis = FakeRedis()
    TranscriptTailer(str(transcript), Identity(run_id="r", task_id="t", seat_id="s", orchestrator="o"),
                     OffsetStore(FakeRedis(), "p:"), live_redis=redis, trace_redis=redis,
                     eval_redis=redis, eval_stream="eval:events", prefix="agent_scratch:", redactor=_redactor).poll()
    assert all("uuid" not in p for p in _eval_payloads(redis))
    live = [json.loads(fields["data"]) for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert any(d.get("uuid") == "u-abc" for d in live)


def _final_flag(redis):
    for event, payload in _eval_by_event(redis):
        if event == "turn_completed":
            return payload.get("turn_clock_monotonic")
    return None


def test_i_trace_only_backward_child_no_tool_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:20.000Z", "message": {"content": [{"type": "text", "text": "next"}]}},)
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_ii_intermediate_inversion_between_bookends_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "assistant", "timestamp": "2026-07-13T18:59:59.000Z", "message": {"content": [{"type": "thinking", "thinking": "back"}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:03.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "next"}]}},)
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_iii_unclean_line_in_turn_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    good1 = json.dumps({"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    good2 = json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "next"}]}})
    transcript.write_text(good1 + "\n{ this is not valid json\n" + good2 + "\n", encoding="utf-8")
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_iii_drifterror_unmappable_line_in_turn_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    good1 = json.dumps({"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    unmappable = json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "text", "text": "?"}]}})
    good2 = json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "next"}]}})
    transcript.write_text(good1 + "\n" + unmappable + "\n" + good2 + "\n", encoding="utf-8")
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_iv_fresh_generation_never_closes_a_straddled_turn_true(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    redis1 = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    _tailer(transcript, redis1, store).poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "thinking", "thinking": "back"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:20.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    redis2 = FakeRedis(); _tailer(transcript, redis2, store).poll()
    assert _final_flag(redis2) is not True


def test_vi_same_object_byte0_reread_replayed_opening_does_not_close_true(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:"); tailer = _tailer(transcript, redis, store)
    tailer.poll(); key = offset_key(str(transcript), transcript.stat().st_ino); offset_redis.values.pop(f"p:claude:offset:{key}", None)
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "thinking", "thinking": "back"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:30.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.poll()
    completed = [(e, p) for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert all(p.get("turn_clock_monotonic") is not True for _, p in completed)
    assert not any(p.get("turn_index") == 0 for _, p in completed)


def test_clean_contiguous_turn_closed_at_next_human_user_is_true(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "text", "text": "next"}]}},)
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is True


def test_vii_single_dispatch_no_next_human_emits_no_turn_completed(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}}]}},
        {"type": "user", "timestamp": "2026-07-13T19:00:02.000Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:03.000Z", "message": {"content": [{"type": "text", "text": "done [ARB_SEAT_DONE]"}]}},)
    redis = FakeRedis(); tailer = _tailer(transcript, redis); tailer.poll(); tailer.finish(ok=True)
    by_event = _eval_by_event(redis)
    assert any(e == "turn_started" for e, _ in by_event)
    assert not any(e == "turn_completed" for e, _ in by_event)


def test_missing_timestamp_on_in_turn_record_is_false(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "text", "text": "next"}]}})
    redis = FakeRedis(); _tailer(transcript, redis).poll()
    assert _final_flag(redis) is False


def test_missing_timestamp_on_opening_record_is_false_and_counted(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}},
        {"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": [{"type": "text", "text": "next"}]}})
    redis = FakeRedis(); tailer = _tailer(transcript, redis); tailer.poll()
    assert _final_flag(redis) is False
    assert tailer.claude_tail_missing_ts >= 1


def test_same_object_retry_after_emit_failure_does_not_misstamp_turn_completed(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript,
        {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer one"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    real_route = tailer._route_event; state = {"boom": True}
    def flaky_route(event):
        if state["boom"] and event.get("event_type") == "turn_started":
            state["boom"] = False; raise RuntimeError("injected emit bug")
        return real_route(event)
    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError): tailer.poll()
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert completed and all(p.get("turn_index") == 1 and p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)


def test_multiline_committed_prefix_inner_restore_not_masked_by_outer_guard(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "first"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    real_route = tailer._route_event; state = {"boom": True}
    def flaky_route(event):
        if state["boom"] and event.get("event_type") == "turn_started":
            state["boom"] = False; raise RuntimeError("injected emit bug")
        return real_route(event)
    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError): tailer.poll()
    pos = store.load(offset_key(str(transcript), transcript.stat().st_ino)); assert pos.offset > eof1
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert completed and all(p.get("turn_index") == 1 and p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)


def test_multiline_poll_emit_fail_on_close_does_not_drop_turn_completed(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "first"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer one"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    real_route = tailer._route_event; state = {"boom": True}
    def flaky_route(event):
        if state["boom"] and event.get("event_type") == "turn_completed":
            state["boom"] = False; raise RuntimeError("injected close bug")
        return real_route(event)
    tailer._route_event = flaky_route
    with pytest.raises(RuntimeError): tailer.poll()
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert completed and all(p.get("turn_index") == 1 and p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)


def test_poll_emits_lifecycle_and_routes_and_redacts(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat .env; SECRET=sk-abc"}}]}},
    )
    redis = FakeRedis()
    identity = warm_identity("sess", "bridge", "dev")
    store = OffsetStore(redis, "p:")
    tailer = TranscriptTailer(str(transcript), identity, store, live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    count = tailer.poll()

    assert count == 2
    assert _event_types(redis) == ["task_started", "command_started"]
    assert {fields["kind"] for fields in _trace_fields(redis)} == {"task_started", "command_started"}
    serialized = json.dumps([fields for _, fields, _ in redis.xadds], sort_keys=True)
    assert "sk-abc" not in serialized
    # Since a74c9d0 the live tee applies the recursive server-side redact() at
    # the XADD boundary (marker "‹redacted›"), so the secret is scrubbed
    # before the injected per-tailer redactor sees it. Pin the OUTCOME (secret
    # gone, a redaction marker present) rather than which layer did it.
    assert "redacted" in serialized.lower()  # matches REDACTED and ‹redacted› alike
    assert store.load(offset_key(str(transcript), transcript.stat().st_ino)).offset == transcript.stat().st_size


def test_model_text_routes_to_trace_only(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    assert _event_types(redis) == ["task_started"]
    assert [fields["kind"] for fields in _trace_fields(redis)] == ["task_started", "model_text"]


def test_offset_commits_only_after_publish_succeeds(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})
    redis = FakeRedis(fail_xadd=True)
    store = OffsetStore(redis, "p:")
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), store, live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    with pytest.raises(RuntimeError, match="xadd failed"):
        tailer.poll()

    assert store.load(offset_key(str(transcript), transcript.stat().st_ino)).offset == 0


def test_offset_resets_on_shrink_and_rereads(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "new"}]}})
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    key = offset_key(str(transcript), transcript.stat().st_ino)
    store.store(key, transcript.stat().st_size + 100, 0)
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), store, live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    redis_key = f"p:claude:offset:{key}"
    assert json.loads(redis.sets[1][1]) == {"v": 1, "offset": 0, "turn_index": 0}
    assert json.loads(redis.sets[-1][1]) == {"v": 1, "offset": transcript.stat().st_size, "turn_index": 0}
    assert [fields["kind"] for fields in _trace_fields(redis)] == ["task_started", "model_text"]


def test_drift_error_emits_event_and_threshold(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "message": {"content": [{"type": "NEW"}]}},
        {"type": "assistant", "message": {"content": [{"type": "NEW"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)
    tailer.drift_threshold = 1

    with pytest.raises(RuntimeError, match="drift threshold exceeded"):
        tailer.poll()

    assert _event_types(redis) == ["drift_error", "drift_error"]
    assert tailer.drift_count == 2


def test_quiet_poll_emits_task_continuing_after_start(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)
    tailer.continuing_interval_s = 0
    tailer.poll()

    count = tailer.poll()

    assert count == 0
    assert _event_types(redis)[-1] == "task_continuing"


def test_finish_emits_task_finished_once_to_live_and_trace(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    event = tailer.finish(ok=False)
    again = tailer.finish(ok=True)

    assert event == {"event_type": "task_finished", "data": {"ok": False}}
    assert again is None
    assert _event_types(redis) == ["task_finished"]
    assert _live_payloads(redis)[0]["ok"] is False
    assert [fields["kind"] for fields in _trace_fields(redis)] == ["task_finished"]


def test_cold_identity_marker_applies_to_opening_and_later_events(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "pwd"}}]}},
    )
    redis = FakeRedis()
    fallback = cold_identity("agent-1", "session-fallback", "")
    tailer = TranscriptTailer(
        str(transcript),
        fallback,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="session-fallback",
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    trace = _trace_fields(redis)
    assert [fields["event_type"] for fields in live] == ["task_started", "command_started"]
    assert {fields["run_id"] for fields in live + trace} == {"run-1"}
    assert {fields["seat_id"] for fields in live + trace} == {"cold-seat-1"}
    assert {fields["orchestrator"] for fields in live + trace} == {"warm-orch"}
    assert live[0]["run_id"] == "run-1"


def test_locked_cold_identity_run_id_is_overridden_by_marker_but_seat_and_orchestrator_are_not(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    # The marker's run_id (run-1) DOES override the locked run_id (sess-x) -- that's this fix's
    # whole purpose. seat_id and orchestrator stay locked regardless (the marker's seat-1/warm-orch
    # values are deliberately ignored -- see spec "Only run_id is patched").
    assert {fields["run_id"] for fields in live} == {"run-1"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}


def test_locked_cold_identity_without_marker_is_fully_unchanged(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "please review this diff, no marker here"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"sess-x"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}


def test_locked_cold_identity_marker_mid_paragraph_still_overrides_run_id(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {
            "type": "user",
            "message": {
                "content": (
                    "You are reviewing a diff. Context first, then the tag: "
                    "[ARB_RUN:mid-label ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] "
                    "now go read the files."
                )
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"mid-label"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}


def test_unlocked_cold_identity_still_upgrades_past_a_leading_drop_type_line(tmp_path):
    # Round-1 spec-review bug: a transcript that opens with a DROP_TYPES line (e.g. "system")
    # resolves the empty-marker fallback via _ensure_identity_resolved() before the real first
    # user line (carrying an ARB marker) ever arrives. identity_locked=False must still let that
    # later marker line upgrade the identity, exactly as it does today.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "system", "subtype": "init"},
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    fallback = cold_identity("agent-1", "session-fallback", "")
    tailer = TranscriptTailer(
        str(transcript),
        fallback,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="session-fallback",
        identity_locked=False,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"run-1"}
    assert {fields["seat_id"] for fields in live} == {"cold-seat-1"}
    assert {fields["orchestrator"] for fields in live} == {"warm-orch"}


def test_cold_seat_done_marker_sets_completed(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "verdict: ship it\n[ARB_SEAT_DONE]"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript), cold_identity("agent-1", "session-fallback", ""), OffsetStore(redis, "p:"),
        live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor,
        cold_agent_id="agent-1", cold_session_id="session-fallback",
    )

    assert tailer.completed is False
    tailer.poll()
    assert tailer.completed is True


def test_warm_seat_ignores_done_marker(tmp_path):
    # A warm orchestrator quoting the marker (e.g. discussing this very feature) must NOT finish.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the marker is [ARB_SEAT_DONE]"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    assert tailer.completed is False


def test_turn_index_advances_on_assistant_turns(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"}]}},
    )
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    tailer.poll()

    assert [fields["turn_index"] for fields in _trace_fields(redis)] == ["1", "1", "2"]


def test_unknown_event_type_routes_to_trace_not_live(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    redis = FakeRedis()
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), OffsetStore(redis, "p:"), live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)

    tailer._route_event({"event_type": "future_model_delta", "data": {"delta": "private prose"}})

    assert _event_types(redis) == []
    assert [fields["kind"] for fields in _trace_fields(redis)] == ["future_model_delta"]


def test_past_threshold_drift_commits_consumed_offset_to_avoid_loop(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "NEW"}]}})
    redis = FakeRedis()
    store = OffsetStore(redis, "p:")
    tailer = TranscriptTailer(str(transcript), warm_identity("sess", "bridge", "dev"), store, live_redis=redis, trace_redis=redis, prefix="agent_scratch:", redactor=_redactor)
    tailer.drift_threshold = 0

    with pytest.raises(RuntimeError, match="drift threshold exceeded"):
        tailer.poll()

    assert store.load(offset_key(str(transcript), transcript.stat().st_ino)).offset == transcript.stat().st_size
    assert tailer.poll() == 0
    assert _event_types(redis) == ["drift_error"]


def test_live_and_trace_use_separate_redis_clients(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}}]}})
    live_redis = FakeRedis()
    trace_redis = FakeRedis()
    offset_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        warm_identity("sess", "bridge", "dev"),
        OffsetStore(offset_redis, "p:"),
        live_redis=live_redis,
        trace_redis=trace_redis,
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer.poll()

    assert _event_types(live_redis) == ["task_started", "command_started"]
    assert [fields["kind"] for fields in _trace_fields(trace_redis)] == ["task_started", "command_started"]
    assert _trace_fields(live_redis) == []
    assert _event_types(trace_redis) == []


def test_live_and_trace_use_separate_prefixes(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pwd"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    )
    live_redis = FakeRedis()
    trace_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        warm_identity("sess", "bridge", "dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=live_redis,
        trace_redis=trace_redis,
        prefix="agent_scratch:",
        trace_prefix="",
        redactor=_redactor,
    )

    tailer.poll()

    assert _xadd_keys(live_redis) == ["agent_scratch:events:live", "agent_scratch:events:live"]
    assert _xadd_keys(trace_redis) == ["arbmem:trace", "arbmem:trace", "arbmem:trace"]
    assert "agent_scratch:arbmem:trace" not in _xadd_keys(trace_redis)


def test_live_and_trace_event_with_run_id_emits_eval_record(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    live_redis = FakeRedis()
    trace_redis = FakeRedis()
    eval_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="claude-bridge-dev", orchestrator="claude-bridge-dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=live_redis,
        trace_redis=trace_redis,
        eval_redis=eval_redis,
        eval_stream="fleet:eval:events",
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer._route_event({"event_type": "command_finished", "data": {"exit_code": 0, "status": "ok", "content": "private"}})

    assert _event_types(live_redis) == ["command_finished"]
    assert [fields["kind"] for fields in _trace_fields(trace_redis)] == ["command_finished"]
    eval_fields = _eval_fields(eval_redis)
    assert len(eval_fields) == 1
    assert eval_fields[0]["run_id"] == "run-1"
    assert eval_fields[0]["task_id"] == "task-1"
    assert eval_fields[0]["seat_id"] == "claude-bridge-dev"
    assert eval_fields[0]["orchestrator"] == "claude-bridge-dev"
    assert eval_fields[0]["event_type"] == "command_finished"
    assert eval_fields[0]["schema_version"] == "1"
    assert json.loads(eval_fields[0]["payload"]) == {
        "exit_code": 0,
        "attempt_epoch": 1,
        "tool_call_id": "task-1:0:1:command_finished",
        "turn_index": 0,
    }


def test_live_only_event_without_run_id_skips_eval_but_still_emits_live(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    live_redis = FakeRedis()
    eval_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="", task_id="task-1", seat_id="claude-bridge-dev", orchestrator="claude-bridge-dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=live_redis,
        trace_redis=FakeRedis(),
        eval_redis=eval_redis,
        eval_stream="eval:events",
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer._route_event({"event_type": "task_continuing", "data": {"attempt": 2}})

    assert _event_types(live_redis) == ["task_continuing"]
    assert _eval_fields(eval_redis) == []


def test_model_event_does_not_emit_eval(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    trace_redis = FakeRedis()
    eval_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="claude-bridge-dev", orchestrator="claude-bridge-dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=FakeRedis(),
        trace_redis=trace_redis,
        eval_redis=eval_redis,
        eval_stream="eval:events",
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer._route_event({"event_type": "model_text", "data": {"delta": "private prose"}})

    assert [fields["kind"] for fields in _trace_fields(trace_redis)] == ["model_text"]
    assert _eval_fields(eval_redis) == []


def test_eval_xadd_failure_does_not_block_live_or_trace(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    live_redis = FakeRedis()
    trace_redis = FakeRedis()
    eval_redis = FakeRedis(fail_xadd=True)
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="claude-bridge-dev", orchestrator="claude-bridge-dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=live_redis,
        trace_redis=trace_redis,
        eval_redis=eval_redis,
        eval_stream="eval:events",
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer._route_event({"event_type": "command_started", "data": {"tool_name": "Bash"}})

    assert _event_types(live_redis) == ["command_started"]
    assert [fields["kind"] for fields in _trace_fields(trace_redis)] == ["command_started"]


def test_unarmed_eval_client_does_not_block_live(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("", encoding="utf-8")
    live_redis = FakeRedis()
    tailer = TranscriptTailer(
        str(transcript),
        Identity(run_id="run-1", task_id="task-1", seat_id="claude-bridge-dev", orchestrator="claude-bridge-dev"),
        OffsetStore(FakeRedis(), "p:"),
        live_redis=live_redis,
        trace_redis=FakeRedis(),
        eval_redis=None,
        eval_stream=None,
        prefix="agent_scratch:",
        redactor=_redactor,
    )

    tailer._route_event({"event_type": "task_continuing", "data": {"attempt": 2}})

    assert _event_types(live_redis) == ["task_continuing"]


def _tailer(transcript, redis, store=None, **kwargs):
    identity = warm_identity("sess", "bridge", "dev")
    store = store or OffsetStore(redis, "p:")
    return TranscriptTailer(
        str(transcript), identity, store,
        live_redis=redis, trace_redis=redis,
        eval_redis=redis, eval_stream="eval:events",
        prefix="agent_scratch:", redactor=lambda s: s, **kwargs,
    )


def _offset_value(redis, transcript):
    import os as _os
    key = offset_key(str(transcript), _os.stat(transcript).st_ino)
    raw = redis.get(f"p:claude:offset:{key}")
    return json.loads(raw)["offset"] if raw else 0


def test_non_dict_json_lines_are_skipped_and_counted(tmp_path, caplog):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(
        'null\n[]\n"str"\n'
        + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        + "\n",
        encoding="utf-8",
    )
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    emitted = tailer.poll()

    assert tailer.skipped_lines == 3
    assert emitted > 0  # the good line still emitted
    assert tailer.at_eof is True
    # skip log records carry path+offset ONLY, never the line bytes (GLM G1)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "null" not in joined and '"str"' not in joined
    # next poll does not re-read the skipped lines
    assert tailer.poll() == 0
    assert tailer.skipped_lines == 3


def test_invalid_json_line_skipped_offset_advances(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{not json}\n", encoding="utf-8")
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.skipped_lines == 1
    assert _offset_value(redis, transcript) == transcript.stat().st_size


def test_emit_stage_redis_error_propagates_without_offset_advance(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})

    class InfraFailRedis(FakeRedis):
        def xadd(self, key, fields, **kwargs):
            raise RedisConnectionError("broken pipe")

    redis = InfraFailRedis()
    tailer = _tailer(transcript, redis)

    with pytest.raises(RedisConnectionError):
        tailer.poll()
    assert _offset_value(redis, transcript) == 0  # infra never advances offsets
    assert tailer.skipped_lines == 0  # NOT classified as a data error
    assert tailer.emit_failing is False  # infra-crash path, NOT the code-bug
    # path — this assertion is what makes deny-proof 1 reddable (plan panel,
    # grok P1 + cold-Opus P2-1: without it, a single-line fixture cannot
    # distinguish the arms because line_start == offset commits nothing).


def test_emit_stage_code_bug_prefix_commits_and_marks_failing(tmp_path):
    # Lines 0..N-1 emit clean, line N's emit raises a non-RedisError: offset
    # commits through N-1 (prefix commit, panel r3 cold-Opus P1), the failure
    # propagates, emit_failing is sticky (spec §A).
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
    _write_jsonl(transcript, good, good, good)
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    calls = {"n": 0}
    real_route = tailer._route_event

    def flaky_route(event):
        calls["n"] += 1
        if calls["n"] >= 4:  # first line emits task_started + model event = 2-3 calls
            raise AttributeError("injected emit bug")
        return real_route(event)

    tailer._route_event = flaky_route

    with pytest.raises(AttributeError):
        tailer.poll()

    assert tailer.emit_failing is True
    committed = _offset_value(redis, transcript)
    line_len = len(json.dumps(good, separators=(",", ":")) + "\n")
    assert committed % line_len == 0 and 0 < committed < transcript.stat().st_size

    # a later clean poll clears the sticky flag
    tailer._route_event = real_route
    tailer.poll()
    assert tailer.emit_failing is False


def test_line_budget_chunks_and_commits_per_chunk(tmp_path):
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    _write_jsonl(transcript, *([good] * 7))
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll_budget_lines = 3

    tailer.poll()
    assert tailer.at_eof is False
    assert tailer.progressed is True
    first = _offset_value(redis, transcript)
    assert 0 < first < transcript.stat().st_size

    tailer.poll()
    second = _offset_value(redis, transcript)
    assert second > first  # monotonic progress per chunk (deny-proof hinge 5b)

    tailer.poll()
    assert tailer.at_eof is True
    assert _offset_value(redis, transcript) == transcript.stat().st_size


def test_wall_clock_budget_finishes_current_line_then_returns(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    good = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    _write_jsonl(transcript, *([good] * 5))
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)
    tailer.poll_budget_secs = 0.0  # every loop-top check is already expired

    tailer.poll()

    # exactly one complete line processed (budget checked between lines; a
    # started line always finishes — offsets are line-granular, spec §A)
    line_len = len(json.dumps(good, separators=(",", ":")) + "\n")
    assert _offset_value(redis, transcript) == line_len
    assert tailer.at_eof is False


def test_partial_trailing_line_counts_as_eof(tmp_path):
    # spec §A (panel r4 grok pin): a torn final line must not block at_eof.
    transcript = tmp_path / "s.jsonl"
    good = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}})
    transcript.write_text(good + "\n" + '{"type": "assis', encoding="utf-8")
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.at_eof is True
    # offset stops at the end of the last COMPLETE line
    assert _offset_value(redis, transcript) == len(good) + 1


def test_drift_error_keeps_dedicated_arm_not_parse_skip(tmp_path):
    # spec §A (panel r3 grok P1 + r4 agy P2): unknown line types still emit
    # drift_error, still count toward the threshold, are NOT skipped.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "mystery_type_zz", "message": {}})
    redis = FakeRedis()
    tailer = _tailer(transcript, redis)

    tailer.poll()

    assert tailer.skipped_lines == 0
    assert tailer.drift_count == 1
    assert "drift_error" in _event_types(redis)


class _FlakyReadFile:
    def __init__(self, fh, ok_reads):
        self._fh = fh
        self._left = ok_reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def seek(self, *args):
        return self._fh.seek(*args)

    def tell(self):
        return self._fh.tell()

    def readline(self):
        if self._left <= 0:
            raise OSError("injected mid-poll I/O failure")
        self._left -= 1
        return self._fh.readline()


def _arm_flaky_open(monkeypatch, ok_reads):
    state = {"armed": True}

    def flaky_open(path, mode="rb", *args, **kwargs):
        fh = open(path, mode, *args, **kwargs)
        if state["armed"]:
            state["armed"] = False
            return _FlakyReadFile(fh, ok_reads)
        return fh

    monkeypatch.setattr(tailer_module, "open", flaky_open, raising=False)


def test_drift_threshold_keeps_cursor_aligned_no_false_null(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    redis = FakeRedis(); offset_redis = FakeRedis(); store = OffsetStore(offset_redis, "p:")
    tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": []}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:00:10.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.drift_threshold = 0
    with pytest.raises(_DriftThresholdExceeded):
        tailer.poll()
    key = offset_key(str(transcript), transcript.stat().st_ino); pos = store.load(key)
    assert pos.offset > eof1 and (tailer._cursor_inode, tailer._cursor_offset) == (transcript.stat().st_ino, pos.offset)
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(completed) == 1 and completed[0].get("turn_index") == 1 and completed[0].get("turn_clock_monotonic") is False
    assert 2 in [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"]


def test_readline_oserror_midpoll_does_not_replay_mutated_turn_state(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "first"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    _arm_flaky_open(monkeypatch, 2)
    with pytest.raises(OSError): tailer.poll()
    assert [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_completed"] == [1]
    assert tailer.logical_turn_index == 1 and tailer._turn_started_ts == "2026-07-13T19:00:00.000Z"
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert all(p.get("turn_index") == 1 and p.get("event_ts") == "2026-07-13T19:00:01.000Z" for p in completed)
    assert 3 not in [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"]


def test_readline_oserror_after_inloop_skip_still_restores_poll_start(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "first"}]}})
    redis = FakeRedis(); tailer = _tailer(transcript, redis); tailer.poll()
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write("{ invalid json\n")
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}}) + "\n")
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "second"}]}}) + "\n")
    _arm_flaky_open(monkeypatch, 3)
    with pytest.raises(OSError): tailer.poll()
    assert tailer.logical_turn_index == 1 and tailer._turn_started_ts == "2026-07-13T19:00:00.000Z"
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert completed and all(p.get("turn_index") == 1 and p.get("turn_clock_monotonic") is False for p in completed)
    assert 3 not in [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"]


def test_same_object_truncate_heal_abandons_open_turn(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}}, {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "long answer " * 20}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    short = json.dumps({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n"
    with open(transcript, "r+", encoding="utf-8") as fh: fh.seek(0); fh.write(short); fh.truncate()
    assert transcript.stat().st_size < eof1
    tailer.poll()
    assert not [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"] == [1, 1]


def test_truncate_to_empty_persists_heal_before_regrowth(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}}, {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    with open(transcript, "r+", encoding="utf-8") as fh: fh.truncate(0)
    tailer.poll(); key = offset_key(str(transcript), transcript.stat().st_ino)
    assert store.load(key) == Position(0, 0)
    pad = "x" * eof1
    with open(transcript, "a", encoding="utf-8") as fh:
        for obj in ({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z", "message": {"content": [{"type": "text", "text": pad}]}}, {"type": "assistant", "timestamp": "2026-07-13T19:10:01.000Z", "message": {"content": [{"type": "text", "text": "reply"}]}}, {"type": "user", "promptId": "p10", "timestamp": "2026-07-13T19:11:00.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}): fh.write(json.dumps(obj) + "\n")
    tailer.poll()
    assert [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"] == [1, 1, 2]
    assert [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_completed"] == [1]


def test_drift_emit_failure_prefix_commits_and_replays_drift_line(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "answer"}]}}) + "\n")
        fh.write(json.dumps({"type": "some_unknown_future_type", "timestamp": "2026-07-13T19:00:05.000Z", "message": {"content": []}}) + "\n")
    real_emit = tailer._emit_drift_error; state = {"boom": True}
    def flaky_emit(exc):
        if state["boom"]: state["boom"] = False; raise RuntimeError("injected drift-emit bug")
        return real_emit(exc)
    tailer._emit_drift_error = flaky_emit
    with pytest.raises(RuntimeError): tailer.poll()
    key = offset_key(str(transcript), transcript.stat().st_ino); pos = store.load(key)
    assert pos.offset > eof1 and (tailer._cursor_inode, tailer._cursor_offset) == (transcript.stat().st_ino, pos.offset)
    tailer.poll()
    drift = [p for p in _live_payloads(redis) if p.get("kind") == "drift_error"]
    assert len(drift) == 1 and drift[0].get("count") == 1 and tailer.drift_count == 1
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptId": "p2", "timestamp": "2026-07-13T19:05:00.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}) + "\n")
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(completed) == 1 and completed[0].get("turn_clock_monotonic") is False


def test_heal_then_uncommitted_failure_restores_post_abandon_state(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, {"type": "user", "promptId": "p1", "timestamp": "2026-07-13T19:00:00.000Z", "message": {"content": [{"type": "text", "text": "go"}]}}, {"type": "assistant", "timestamp": "2026-07-13T19:00:01.000Z", "message": {"content": [{"type": "text", "text": "padding " * 120}]}})
    redis = FakeRedis(); store = OffsetStore(FakeRedis(), "p:"); tailer = _tailer(transcript, redis, store); tailer.poll(); eof1 = transcript.stat().st_size
    new_gen = "".join(json.dumps(obj) + "\n" for obj in ({"type": "user", "promptId": "p9", "timestamp": "2026-07-13T19:10:00.000Z", "message": {"content": [{"type": "text", "text": "new gen"}]}}, {"type": "assistant", "timestamp": "2026-07-13T19:10:01.000Z", "message": {"content": [{"type": "text", "text": "reply"}]}}, {"type": "user", "promptId": "p10", "timestamp": "2026-07-13T19:11:00.000Z", "message": {"content": [{"type": "text", "text": "next"}]}}))
    with open(transcript, "r+", encoding="utf-8") as fh: fh.seek(0); fh.write(new_gen); fh.truncate()
    assert transcript.stat().st_size < eof1
    _arm_flaky_open(monkeypatch, 3)
    with pytest.raises(OSError): tailer.poll()
    mid = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert len(mid) == 1 and mid[0].get("turn_index") == 1
    assert store.load(offset_key(str(transcript), transcript.stat().st_ino)).offset == 0
    tailer.poll()
    completed = [p for e, p in _eval_by_event(redis) if e == "turn_completed"]
    assert completed and all(p.get("turn_index") == 1 and p.get("event_ts") == "2026-07-13T19:10:01.000Z" for p in completed)
    starts = [p.get("turn_index") for e, p in _eval_by_event(redis) if e == "turn_started"]
    assert 2 in starts and 3 not in starts and 0 not in starts


def _iter_attr_targets(target):
    if isinstance(target, ast.Attribute): yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts: yield from _iter_attr_targets(element)
    elif isinstance(target, ast.Starred): yield from _iter_attr_targets(target.value)


def _enclosing_func_name(tree, node):
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    best = None
    for f in funcs:
        if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and (best is None or f.lineno > best.lineno): best = f
    return best.name if best else "<module>"


def _cursor_sole_writer_census(source):
    tree = ast.parse(source); violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = getattr(node, "value", None); targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for attr in _iter_attr_targets(target):
                    if attr.attr in ("_cursor_offset", "_cursor_inode"):
                        where = _enclosing_func_name(tree, node); none_init = where == "__init__" and isinstance(value, ast.Constant) and value.value is None
                        if where != "_commit" and not none_init: violations.append(f"{where}:{node.lineno} writes {attr.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            recv = getattr(func, "value", None)
            if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in ("_cursor_offset", "_cursor_inode"):
                violations.append(f"{_enclosing_func_name(tree, node)}:{node.lineno} setattr {node.args[1].value}")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute) and func.value.attr == "offset_store":
                where = _enclosing_func_name(tree, node)
                if func.attr == "store" and where != "_commit": violations.append(f"{where}:{node.lineno} calls offset_store.store")
                elif func.attr in ("commit", "get"): violations.append(f"{where}:{node.lineno} calls forbidden offset_store.{func.attr}")
            elif (isinstance(node.func, ast.Attribute) and node.func.attr == "set"
                  and isinstance(recv, ast.Attribute) and recv.attr == "redis"
                  and isinstance(recv.value, ast.Attribute) and recv.value.attr == "offset_store"):
                violations.append(f"{_enclosing_func_name(tree, node)}:{node.lineno} reaches offset_store.redis")
    return violations


def _poll_guarded_try_has_no_return(source):
    tree = ast.parse(source); violations = []
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and func.name == "poll":
            for try_node in ast.walk(func):
                if isinstance(try_node, ast.Try):
                    for sub in ast.walk(try_node):
                        if isinstance(sub, ast.Return): violations.append(f"poll:{sub.lineno} return inside the guarded try")
    return violations


def _offset_module_write_census(source):
    tree = ast.parse(source); violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            recv = node.func.value
            if node.func.attr == "set" and isinstance(recv, ast.Attribute) and recv.attr == "redis" and _enclosing_func_name(tree, node) != "store": violations.append("writes redis outside store()")
            if node.func.attr == "store" and isinstance(recv, ast.Name) and recv.id == "self" and _enclosing_func_name(tree, node) != "store": violations.append("self.store outside store()")
    return violations


def test_commit_is_sole_writer_of_cursor_and_store():
    source = Path(tailer_module.__file__).read_text(encoding="utf-8")
    assert _cursor_sole_writer_census(source) == [] and _poll_guarded_try_has_no_return(source) == []
    import agent_redis_bridge.claude_tail.offset as offset_module
    assert _offset_module_write_census(Path(offset_module.__file__).read_text(encoding="utf-8")) == []


def test_sole_writer_census_reds_on_planted_rogue_writers():
    source = Path(tailer_module.__file__).read_text(encoding="utf-8"); anchor = "self.progressed = True"; assert anchor in source
    def planted(rogue): return source.replace(anchor, rogue + "; " + anchor, 1)
    assert any("_cursor_offset" in v for v in _cursor_sole_writer_census(planted("self._cursor_offset = 999")))
    assert any("offset_store.store" in v for v in _cursor_sole_writer_census(planted("self.offset_store.store(key, 0, 0)")))
    assert any("forbidden offset_store.commit" in v for v in _cursor_sole_writer_census(planted("self.offset_store.commit(key, 0)")))
    assert any("forbidden offset_store.get" in v for v in _cursor_sole_writer_census(planted("self.offset_store.get(key)")))
    assert any("setattr _cursor_offset" in v for v in _cursor_sole_writer_census(planted("setattr(self, '_cursor_offset', 999)")))
    assert any("reaches offset_store.redis" in v for v in _cursor_sole_writer_census(planted("self.offset_store.redis.set(key, 0)")))
    assert any("return inside" in v for v in _poll_guarded_try_has_no_return(planted("return 0")))
    import agent_redis_bridge.claude_tail.offset as offset_module
    offset = Path(offset_module.__file__).read_text(encoding="utf-8"); anchor = "raw = raw.decode()"
    assert any("outside store()" in v for v in _offset_module_write_census(offset.replace(anchor, 'self.redis.set("k", "0"); ' + anchor, 1)))
    assert any("self.store outside store()" in v for v in _offset_module_write_census(offset.replace(anchor, "self.store(key, 0, 0); " + anchor, 1)))
