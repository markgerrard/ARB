from __future__ import annotations

import re


_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_BAD_SEGMENTS = {"", ".", ".."}


def validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError("invalid file name")
    if name.startswith("/"):
        raise ValueError("file name must not start with '/'")
    segments = name.split("/")
    if segments[0] == ".trash":
        raise ValueError("file name must not use reserved .trash namespace")
    for segment in segments:
        if segment in _BAD_SEGMENTS:
            raise ValueError(f"invalid path segment {segment!r}")


def to_key(prefix: str, name: str) -> str:
    validate_name(name)
    return f"{prefix}{name}"
