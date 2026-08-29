from __future__ import annotations

import json
from typing import Any


Event = dict[str, Any]
DROP_TYPES = {
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
}


class DriftError(RuntimeError):
    """Claude transcript shape changed in a way this mapper does not understand."""


def map_line(obj: dict) -> list[Event]:
    line_type = obj.get("type")
    if line_type in DROP_TYPES:
        return []
    if line_type == "assistant":
        return _map_assistant(obj)
    if line_type == "user":
        return _map_user(obj)
    raise DriftError(f"unknown Claude transcript line type: {line_type!r}")


def _content_blocks(obj: dict) -> list[dict[str, Any]]:
    message = obj.get("message")
    if not isinstance(message, dict):
        raise DriftError("Claude transcript line missing message object")
    content = message.get("content")
    if not isinstance(content, list):
        raise DriftError("Claude transcript message.content is not a list")
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise DriftError("Claude transcript content block is not an object")
        blocks.append(block)
    return blocks


def _map_assistant(obj: dict) -> list[Event]:
    events: list[Event] = []
    for block in _content_blocks(obj):
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise DriftError("assistant text block missing text")
            events.append({"event_type": "model_text", "data": {"delta": text}})
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise DriftError("assistant thinking block missing thinking")
            events.append({"event_type": "model_thinking", "data": {"delta": thinking}})
        elif block_type == "tool_use":
            events.append({"event_type": "command_started", "data": _tool_use_data(block)})
        else:
            raise DriftError(f"unknown assistant content block type: {block_type!r}")
    return events


def _map_user(obj: dict) -> list[Event]:
    message = obj.get("message")
    if not isinstance(message, dict):
        raise DriftError("Claude transcript line missing message object")
    if isinstance(message.get("content"), str):
        return []

    events: list[Event] = []
    for block in _content_blocks(obj):
        block_type = block.get("type")
        if block_type in ("text", "image"):
            # Plain user text and pasted screenshots carry no seat activity to trace; drop them
            # (an image block's base64 payload must never reach the transcript stream).
            continue
        if block_type != "tool_result":
            raise DriftError(f"unknown user content block type: {block_type!r}")
        events.extend(_tool_result_events(block))
    return events


def _tool_use_data(block: dict[str, Any]) -> dict[str, Any]:
    name = block.get("name")
    if not isinstance(name, str):
        raise DriftError("tool_use block missing name")
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        raise DriftError("tool_use block missing input object")
    tool_use_id = block.get("id")
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else name
    return {
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else "",
        "tool_name": name,
        "command": command,
        "input": tool_input,
    }


def _tool_result_events(block: dict[str, Any]) -> list[Event]:
    tool_use_id = block.get("tool_use_id")
    content = _stringify_tool_result_content(block.get("content", ""))
    is_error = bool(block.get("is_error", False))
    return [
        {
            "event_type": "command_output",
            "data": {
                "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else "",
                "delta": content,
            },
        },
        {
            "event_type": "command_finished",
            "data": {
                "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else "",
                "command": None,
                "status": "failed" if is_error else "completed",
                "exit_code": 1 if is_error else 0,
            },
        },
    ]


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(_strip_image_data(content), ensure_ascii=False, separators=(",", ":"))


def _strip_image_data(value: Any) -> Any:
    # Replace image content blocks with a small placeholder so base64 image payloads (from reading
    # screenshots / image files) never reach the trace stream and bloat or corrupt the renderer.
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "image", "source": "[image stripped]"}
        return {key: _strip_image_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_image_data(item) for item in value]
    return value
