"""Shared BriefRef value + parser for dual-accept task wires (Slice 1d-iv Task 5).

One parser for envelope, bridge, and CLI surfaces — integer/string rules must not
diverge. A well-formed ref is exactly ``{"artefact_id": <nonblank str>,
"version": <positive int>}`` with no extra keys required and no body bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class BriefRefError(ValueError):
    """Malformed task ref. Reason token is stable for envelope/bridge surfaces."""

    def __init__(self, reason: str = "invalid-payload-task-ref") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class BriefRef:
    artefact_id: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {"artefact_id": self.artefact_id, "version": self.version}


def parse_brief_ref(value: Any) -> BriefRef:
    """Parse a task value as a BriefRef, or raise BriefRefError.

    Accepts a BriefRef, or a mapping with nonblank string artefact_id and a
    positive (non-bool) int version. Rejects strings, lists, missing keys,
    blank ids, bool versions, and non-positive versions.
    """
    if isinstance(value, BriefRef):
        return value
    if not isinstance(value, Mapping):
        raise BriefRefError("invalid-payload-task-ref")
    if "artefact_id" not in value or "version" not in value:
        raise BriefRefError("invalid-payload-task-ref")
    artefact_id = value.get("artefact_id")
    version = value.get("version")
    if not isinstance(artefact_id, str) or not artefact_id.strip():
        raise BriefRefError("invalid-payload-task-ref")
    # bool is a subclass of int — reject explicitly.
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BriefRefError("invalid-payload-task-ref")
    return BriefRef(artefact_id=artefact_id.strip(), version=version)


def is_brief_ref_shape(value: Any) -> bool:
    """True when value looks like a ref object (dict with artefact_id), even if malformed."""
    return isinstance(value, Mapping) and "artefact_id" in value
