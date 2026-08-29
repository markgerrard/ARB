"""Reporter + the structural wall (schema v0.2 §5; measurement-principles.md P2).

The wall is now an ALLOWLIST, not a name-denylist (build-review P1-3 / principle P2 / the
closed-taxonomy lesson): the headline grid emitter accepts ONLY
  - seat ids matching a safe charset (no spaces/parens -> kills `"codex (rank 1)"` smuggling),
  - column keys that are real defect classes (taxonomy) -> kills a smuggled `ranking` column,
  - cell values in {PASS, FAIL, UNKNOWN} -> kills any value field.
Anything else RAISES. A finite name-denylist categorically cannot catch a synonym/nested/
whitespace variant it doesn't list; an allowlist of *permitted* shapes can. The denylist
survives only as a secondary tripwire (recursive + whitespace-normalized) for non-grid records.

It holds for its claim. The residual — any per-seat-per-class grid is reader-convertible into a
ranking — is NAMED in the disclaimer as accepted-not-walled, so "accepted residual" cannot decay
into "ignored residual".
"""
from __future__ import annotations

import re

from .schema import TAXONOMY

VERDICTS = frozenset({"PASS", "FAIL", "UNKNOWN"})
SEAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Secondary tripwire only (the allowlist above is primary): forbidden substrings, checked
# recursively and whitespace-normalized so synonyms-by-spacing don't slip the net.
_DENY_SUBSTR = ("rank", "score", "decorrelat", "marginal", "lone", "seatvalue", "seat_value",
                "keep", "drop", "redundant", "best", "worst", "quality", "tier", "ordering",
                "priority", "goodness")

DISCLAIMER = (
    "Floor viability on legible seeded defects — can this seat catch the class above its own "
    "noise. Does NOT measure reviewer value, reliability (how-often), decorrelation, or "
    "seat-keep/drop (ill-posed internally — eval-design v3 §0). A per-seat-per-class grid is "
    "inherently reader-convertible into a ranking this suite never emitted; that residual is "
    "ACCEPTED, NOT WALLED (measurement-principles.md P2). Do not sort these cells and read seat "
    "value into them. A FAIL means 'missed legible seeds in this class', never a drop signal."
)


class WallBreach(ValueError):
    """Raised when a floor record violates the allowlist (or trips the secondary denylist)."""


_GOLD_ALLOWED = frozenset({"gold_adjudicated", "gold_version", "suppressed_seats"})


def assert_gold_field_shape(gold: dict) -> None:
    extra = set(gold) - _GOLD_ALLOWED
    if extra:
        raise WallBreach(f"gold artifact field carries non-allowlisted keys: {sorted(extra)}")


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(s).lower())


def guard(record: dict) -> dict:
    """Secondary tripwire for arbitrary records (defense in depth). Recurses into nested
    dicts and normalizes whitespace/separators, so `' rank '`, `rank_order`, and
    `{'detail': {'rank': 1}}` are all caught — unlike the old top-level exact-name denylist."""
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm(k)
                if any(bad in nk for bad in _DENY_SUBSTR):
                    raise WallBreach(f"forbidden seat-value/ranking field: {k!r}")
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for x in obj:
                walk(x)
    walk(record)
    return record


def assert_verdict_row(seat: str, row: dict) -> None:
    """PRIMARY wall: a headline grid row must be {class -> verdict} and nothing else."""
    if not SEAT_ID_RE.match(str(seat)):
        raise WallBreach(f"seat id {seat!r} not in safe charset (smuggling guard)")
    for k, v in row.items():
        if k not in TAXONOMY:
            raise WallBreach(f"grid column {k!r} is not a defect class (allowlist)")
        if v not in VERDICTS:
            raise WallBreach(f"grid cell for {seat}/{k} = {v!r} is not a verdict literal")


def render_grid(verdicts: dict[str, dict[str, str]], classes: list[str]) -> str:
    """verdicts: {seat: {class: 'PASS'|'FAIL'|'UNKNOWN'}}. Headline = verdicts only, no raw
    rates/CIs (those live in the detail file). Rows alphabetical; columns in scenario order.
    Every seat/class/cell is allowlist-validated before rendering."""
    for c in classes:
        if c not in TAXONOMY:
            raise WallBreach(f"grid column {c!r} is not a defect class (allowlist)")
    for seat, row in verdicts.items():
        assert_verdict_row(seat, row)
    seats = sorted(verdicts)
    w = max([len("seat")] + [len(s) for s in seats])
    header = "  ".join(["seat".ljust(w)] + classes)
    lines = [header, "-" * len(header)]
    for s in seats:
        cells = [verdicts[s].get(c, "UNKNOWN").ljust(len(c)) for c in classes]
        lines.append("  ".join([s.ljust(w)] + cells))
    lines.append("")
    lines.append("[disclaimer] " + DISCLAIMER)
    return "\n".join(lines)
