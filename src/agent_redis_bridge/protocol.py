from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


STRUCTURED_STATUSES = {"DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"}
STRUCTURED_REPLY_DIRECTIVE = """\
Return your normal concise result first.

At the end of your reply, include a structured status block exactly like this:

```json
{
  "status": "DONE",
  "summary": "One sentence summary.",
  "concerns": [],
  "next_steps": []
}
```

The status must be one of: DONE, DONE_WITH_CONCERNS, BLOCKED, NEEDS_CONTEXT.
Use DONE_WITH_CONCERNS when work is complete but there are residual risks.
Use BLOCKED when you cannot proceed without an external fix.
Use NEEDS_CONTEXT when the requester must provide more information.
"""


class StructuredReplyError(ValueError):
    pass


@dataclass(frozen=True)
class StructuredParseResult:
    structured: dict[str, Any] | None
    error: str | None = None


def structured_reply_directive() -> str:
    return STRUCTURED_REPLY_DIRECTIVE


def build_task_prompt(task: str, *, system_prompt: str | None = None, expect_structured: bool = False) -> str:
    parts: list[str] = []
    stripped_system_prompt = system_prompt.strip() if system_prompt else None
    if stripped_system_prompt:
        parts.append(
            f"<system_guidance>\n"
            f"{stripped_system_prompt}\n"
            f"</system_guidance>"
        )
    parts.append(task)
    if expect_structured:
        parts.append(
            f"<structured_reply_instructions>\n"
            f"{structured_reply_directive().strip()}\n"
            f"</structured_reply_instructions>"
        )
    return "\n\n".join(parts)


def parse_structured_reply(text: str) -> StructuredParseResult:
    candidates = fenced_json_candidates(text)
    last_error: str | None = None
    parsed, last_error = validate_candidates(candidates, last_error)
    if parsed is not None:
        return StructuredParseResult(structured=parsed)

    fallback_candidates = inline_json_candidates(text)
    parsed, last_error = validate_candidates(fallback_candidates, last_error)
    if parsed is not None:
        return StructuredParseResult(structured=parsed)
    if not candidates and not fallback_candidates:
        return StructuredParseResult(structured=None, error="missing-structured-block")
    return StructuredParseResult(structured=None, error=last_error or "missing-structured-block")


def validate_candidates(candidates: list[Any], last_error: str | None) -> tuple[dict[str, Any] | None, str | None]:
    for value in candidates:
        try:
            return validate_structured_reply(value), last_error
        except StructuredReplyError as exc:
            if last_error is None:
                last_error = str(exc)
    return None, last_error


def extract_json_candidate(text: str) -> str | None:
    fenced = list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE))
    for match in reversed(fenced):
        candidate = match.group(1)
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate

    decoder = json.JSONDecoder()
    for start in reversed([index for index, char in enumerate(text) if char == "{"]):
        try:
            _value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        return text[start : start + end]
    return None


def fenced_json_candidates(text: str) -> list[Any]:
    fenced = list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE))
    values: list[Any] = []
    for match in reversed(fenced):
        try:
            values.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return values


def inline_json_candidates(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    for start in reversed([index for index, char in enumerate(text) if char == "{"]):
        try:
            value, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        values.append(value)
    return values


def validate_structured_reply(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StructuredReplyError("structured-reply-not-object")
    status = value.get("status")
    if status not in STRUCTURED_STATUSES:
        raise StructuredReplyError("invalid-structured-status")

    structured: dict[str, Any] = {"status": status}
    if isinstance(value.get("summary"), str):
        structured["summary"] = value["summary"]
    for field in ("concerns", "next_steps", "questions", "artifacts"):
        if isinstance(value.get(field), list):
            structured[field] = value[field]
    return structured
