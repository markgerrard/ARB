"""Append-only collector for H2 shadow-mode records."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def shadow_log_path() -> Path:
    explicit = os.environ.get("ARB_H2_SHADOW_LOG")
    if explicit:
        return Path(explicit)

    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "arb" / "h2-shadow-log.jsonl"

    return Path.home() / ".local" / "state" / "arb" / "h2-shadow-log.jsonl"


def append_record(record: Any, *, log_path: Path | None = None) -> None:
    path = log_path or shadow_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(_record_payload(record), sort_keys=True))
        log.write("\n")


def _record_payload(record: Any) -> dict[str, Any]:
    if is_dataclass(record):
        return asdict(record)
    if isinstance(record, Mapping):
        return dict(record)
    raise TypeError("record must be an H2Record dataclass or mapping")
