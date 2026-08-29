import json
from pathlib import Path

import pytest

from agent_redis_bridge.claude_tail.mapper import DROP_TYPES, DriftError, map_line


FIXTURE = Path(__file__).parent / "fixtures" / "lines.jsonl"
BENIGN_USER_FIXTURE = Path(__file__).parent / "fixtures" / "benign_user_lines.jsonl"


def _fixture_lines():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def _benign_user_lines():
    return [json.loads(line) for line in BENIGN_USER_FIXTURE.read_text(encoding="utf-8").splitlines()]


def test_assistant_text_maps_to_model_text():
    ev = map_line({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}})
    assert ev == [{"event_type": "model_text", "data": {"delta": "hi"}}]


def test_assistant_thinking_maps_to_model_thinking():
    ev = map_line({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}})
    assert ev == [{"event_type": "model_thinking", "data": {"delta": "hmm"}}]


def test_tool_use_maps_to_command_started():
    ev = map_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
    )
    assert ev == [
        {
            "event_type": "command_started",
            "data": {
                "tool_use_id": "toolu_1",
                "tool_name": "Bash",
                "command": "ls",
                "input": {"command": "ls"},
            },
        }
    ]


def test_tool_result_maps_to_command_output_and_finished():
    ev = map_line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "out"}]}})
    assert ev == [
        {"event_type": "command_output", "data": {"tool_use_id": "toolu_1", "delta": "out"}},
        {
            "event_type": "command_finished",
            "data": {"tool_use_id": "toolu_1", "command": None, "status": "completed", "exit_code": 0},
        },
    ]


def test_user_image_block_drops():
    # A user-pasted screenshot is an image block — drop it, don't DriftError or tee its base64.
    line = {"type": "user", "message": {"content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 5000}},
    ]}}
    assert map_line(line) == []


def test_tool_result_image_content_is_stripped():
    # Reading an image file yields a tool_result whose content carries base64 image data; it must
    # not land in the trace delta (it bloats/corrupts the arb-watch renderer).
    blob = "B" * 5000
    line = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_9", "content": [
            {"type": "text", "text": "here is the image"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": blob}},
        ]},
    ]}}
    ev = map_line(line)
    delta = ev[0]["data"]["delta"]
    assert blob not in delta
    assert "here is the image" in delta


def test_file_history_snapshot_drops():
    assert "file-history-snapshot" in DROP_TYPES
    assert map_line({"type": "file-history-snapshot"}) == []


def test_all_observed_benign_line_types_drop():
    for line_type in {
        "ai-title",
        "agent-name",
        "mode",
        "permission-mode",
        "system",
        "last-prompt",
        "queue-operation",
        "attachment",
        "file-history-snapshot",
        "bridge-session",
    }:
        assert line_type in DROP_TYPES
        assert map_line({"type": line_type}) == []


def test_user_string_content_drops():
    user_string, _ = _benign_user_lines()
    assert map_line(user_string) == []


def test_user_text_block_drops():
    _, user_text = _benign_user_lines()
    assert map_line(user_text) == []


def test_unknown_line_type_raises_drift():
    with pytest.raises(DriftError):
        map_line({"type": "totally-new"})


def test_unknown_shape_raises_drift():
    with pytest.raises(DriftError):
        map_line({"type": "assistant", "message": {"content": [{"type": "NEW_BLOCK"}]}})


def test_real_fixture_lines_round_trip_observed_shapes():
    assistant_text, assistant_thinking, assistant_tool_use, user_tool_result, snapshot = _fixture_lines()

    text_events = map_line(assistant_text)
    assert text_events[0]["event_type"] == "model_text"
    assert text_events[0]["data"]["delta"] == assistant_text["message"]["content"][0]["text"]

    thinking_events = map_line(assistant_thinking)
    assert thinking_events == [
        {
            "event_type": "model_thinking",
            "data": {"delta": assistant_thinking["message"]["content"][0]["thinking"]},
        }
    ]

    tool_use_events = map_line(assistant_tool_use)
    tool_use_block = assistant_tool_use["message"]["content"][0]
    assert tool_use_events == [
        {
            "event_type": "command_started",
            "data": {
                "tool_use_id": tool_use_block["id"],
                "tool_name": tool_use_block["name"],
                "command": tool_use_block["name"],
                "input": tool_use_block["input"],
            },
        }
    ]

    tool_result_events = map_line(user_tool_result)
    tool_result_block = user_tool_result["message"]["content"][0]
    assert tool_result_events == [
        {"event_type": "command_output", "data": {"tool_use_id": tool_result_block["tool_use_id"], "delta": tool_result_block["content"]}},
        {
            "event_type": "command_finished",
            "data": {"tool_use_id": tool_result_block["tool_use_id"], "command": None, "status": "completed", "exit_code": 0},
        },
    ]

    assert map_line(snapshot) == []
