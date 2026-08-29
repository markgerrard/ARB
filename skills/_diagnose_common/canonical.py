"""Deterministic canonical byte form for sealed diagnose artifacts."""

from __future__ import annotations

import hashlib
import json


def canonical_bytes(obj: dict) -> bytes:
    _reject_absolute_paths(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def seal(obj: dict) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def _reject_absolute_paths(value) -> None:
    if isinstance(value, str):
        if value.startswith("/"):
            raise ValueError(f"absolute path in sealed brief: {value!r}")
    elif isinstance(value, dict):
        for item in value.values():
            _reject_absolute_paths(item)
    elif isinstance(value, list):
        for item in value:
            _reject_absolute_paths(item)
