from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

import httpx


RESUMABLE_EVENT_ID_RE = re.compile(r"^\d+-\d+$")


def is_resumable_event_id(event_id: str | None) -> bool:
    return bool(event_id and RESUMABLE_EVENT_ID_RE.match(event_id))


def parse_frames(buffer: str) -> tuple[list[dict], str]:
    normalized = buffer.replace("\r\n", "\n")
    if "\n\n" not in normalized:
        return [], normalized

    parts = normalized.split("\n\n")
    complete, tail = parts[:-1], parts[-1]
    frames = []
    for raw_frame in complete:
        frame = _parse_frame(raw_frame)
        if frame:
            frames.append(frame)
    return frames, tail


def _parse_frame(raw_frame: str) -> dict | None:
    event_id = None
    event = None
    data_lines = []

    for line in raw_frame.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)

    if event_id is None and event is None and not data_lines:
        return None

    data_raw = "\n".join(data_lines)
    try:
        data = json.loads(data_raw) if data_raw else None
    except json.JSONDecodeError:
        data = data_raw

    return {
        "id": event_id,
        "resumable_id": event_id if is_resumable_event_id(event_id) else None,
        "event": event,
        "data": data,
    }


async def stream(url: str, token: str, last_id: str | None = None) -> AsyncIterator[dict]:
    resumable_last_id = last_id if is_resumable_event_id(last_id) else None
    initial_backoff_s = 0.25
    backoff_s = 0.25
    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            tail = ""
            headers = {"Authorization": f"Bearer {token}"}
            if resumable_last_id:
                headers["Last-Event-ID"] = resumable_last_id

            try:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_text():
                        tail += chunk
                        frames, tail = parse_frames(tail)
                        for frame in frames:
                            if frame["resumable_id"]:
                                resumable_last_id = frame["resumable_id"]
                            backoff_s = initial_backoff_s
                            yield frame
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    raise
            except Exception:
                pass

            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 5.0)
