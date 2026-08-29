"""Parse a panel seat's structured stance block out of raw reply text.

One parser for BOTH transports (bridge reply text + in-session final message). NOT the bridge's
--expect-structured field (a fixed status schema that cannot carry a stance block; see spec v2/P0).
"""
import json
import re

STANCES = {"approve", "needs-changes", "block", "abstain", "timed-out"}
SEVERITIES = {"none", "P2", "P1", "P0"}

_FENCE = re.compile(r"```(?:vote|json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_FENCE_LABELED = re.compile(r"```(?:vote|json)\s+(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class StanceError(ValueError):
    """Raised when a reply has no parseable / valid stance block. Fail-loud; never invent a vote."""


def _candidates(text, *, require_fence=False):
    # Fenced blocks first (last wins), then (unless require_fence) a bare trailing {...} object.
    fence = _FENCE_LABELED if require_fence else _FENCE
    for m in reversed(list(fence.finditer(text))):
        yield m.group(1)
    if require_fence:
        return
    start = text.rfind("{")
    if start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            yield json.dumps(value)
        except json.JSONDecodeError:
            return


def _validate(value) -> dict:
    if not isinstance(value, dict):
        raise StanceError("stance block is not an object")
    stance = value.get("stance")
    if stance not in STANCES:
        raise StanceError(f"invalid stance {stance!r}; must be one of {sorted(STANCES)}")
    # Absent severity defaults to "none" so a valid stance is never DROPPED for a
    # missing field (the vote is what drives the merge decision; severity is
    # advisory triage). A present-but-invalid severity (typo) is still rejected
    # so malformed fences don't pass silently (#12, 2026-07-08).
    severity = value.get("severity", "none")
    if severity is None:
        severity = "none"
    if severity not in SEVERITIES:
        raise StanceError(f"invalid severity {severity!r}; must be one of {sorted(SEVERITIES)}")
    return {
        "stance": stance,
        "severity": severity,
        "refs": list(value.get("refs", [])),
        "note": str(value.get("note", "")),
    }


def parse_stance(text: str, *, require_fence: bool = False) -> dict:
    last_error = None
    found = False
    for raw in _candidates(text, require_fence=require_fence):
        found = True
        try:
            value = json.loads(raw)
            return _validate(value)
        except (json.JSONDecodeError, StanceError) as exc:
            last_error = exc
    if not found:
        raise StanceError("no stance block found in reply")
    if isinstance(last_error, json.JSONDecodeError):
        raise StanceError(f"stance block is not valid JSON: {last_error}") from last_error
    raise last_error
