from __future__ import annotations

from typing import Any


def canonical_tool_call_id(data: dict[str, Any]) -> str:
    """Coalesce provider and presentation ids into one tool correlation key."""
    for key in ("tool_call_id", "tool_use_id", "item_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
