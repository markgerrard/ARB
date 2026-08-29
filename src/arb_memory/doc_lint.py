"""Structure/staleness lint for markdown artefacts — seed of a store-side write hook.

WHY THIS EXISTS (provenance, 2026-07-26)
----------------------------------------
While authoring `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md`, two
documentation defects got through repeated *reading*-based checking and were caught
only by accident:

1. **A section heading vanished.** An `Edit` used ``## 6. Exempt lane mechanics`` as
   its anchor and failed to reproduce it in the replacement. §5.3's body ran straight
   into §6's body. Every subsequent read of the document looked fine — an absent
   heading produces no discrepancy to notice, only a slightly shorter document.
2. **A count claim went stale behind a schema change.** Prose said the gate credential
   held "two views" while the SQL defined three. It had *already* been fixed once
   ("one view" -> "two views") and went stale again the next time a view was added.

Both are the same shape: an assertion the document could no longer support, invisible
to re-reading because re-reading surfaces *discrepancies between artefact and
expectation*, never *absences*. The corrective is the standing one — move the check
into machinery. See `docs/defect-classes/verification-is-context-triggered-not-risk-triggered.md`.

DESIGN NOTE — why not a literal pattern list
--------------------------------------------
The obvious implementation is a grep list ("flag the string 'one view'"). It was tried
and is demonstrably noisy: the same document legitimately contains "one view-rewrite",
which a literal grep flags. Count claims are therefore checked *relationally* — the
prose claim is compared against the document's own definitions — so the check has a
ground truth rather than a blocklist. New defect classes should be added the same way:
a predicate over the document, not a string to fear.

Output follows the house refusal style used by `arb_memory.close`
(`{"outcome", "exit_code", "gaps"}`): gaps NAME what is missing so a caller is routed,
not merely bounced.
"""

from __future__ import annotations

import re


LINT_OK = "ok"
LINT_REFUSED = "refused_lint"
LINT_EXIT_CODE = 5  # 2/3/4 are taken by arb_memory.close outcomes.

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_TOP_HEADING = re.compile(r"^## (\d+)\.", re.M)
_CREATE_VIEW = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)", re.M | re.I)
# `(?![\w-])` keeps "one view-rewrite" and "viewing" from being read as count claims.
_VIEW_COUNT_CLAIM = re.compile(
    r"\b(" + "|".join(_WORD_NUMBERS) + r")\s+views?(?![\w-])", re.I
)


def check_structure(text: str) -> list[str]:
    """Numbered top-level sections must be present, unique and contiguous from 1.

    Catches the silent-deletion case: a heading consumed by an edit anchor leaves a
    gap in the sequence, which is mechanically visible even though the prose reads fine.
    """
    found = [int(n) for n in _TOP_HEADING.findall(text)]
    if not found:
        return []  # not a numbered-section document; nothing to assert.

    gaps: list[str] = []

    duplicates = sorted({n for n in found if found.count(n) > 1})
    if duplicates:
        gaps.append(f"duplicate top-level sections: {duplicates}")

    expected = list(range(1, max(found) + 1))
    missing = [n for n in expected if n not in found]
    if missing:
        gaps.append(f"missing top-level sections: {missing} (found {sorted(set(found))})")

    if found != sorted(found):
        gaps.append(f"top-level sections out of order: {found}")

    return gaps


def check_view_count_claims(text: str) -> list[str]:
    """Prose claiming "N views" must agree with the document's own CREATE VIEW count.

    Only fires when the document actually defines views — a prose count with no
    definitions to check against is not a claim this lint can adjudicate, and
    guessing would make the lint itself an unverified claim.
    """
    defined = {name.lower() for name in _CREATE_VIEW.findall(text)}
    if not defined:
        return []

    gaps: list[str] = []
    for word in {m.group(1).lower() for m in _VIEW_COUNT_CLAIM.finditer(text)}:
        claimed = _WORD_NUMBERS[word]
        if claimed != len(defined):
            gaps.append(
                f'prose claims "{word} views" but {len(defined)} are defined '
                f"({sorted(defined)})"
            )
    return gaps


CHECKS = (check_structure, check_view_count_claims)


def lint(text: str) -> dict:
    """Run every check; return the house-style result.

    Refuses with named gaps rather than a boolean, so a store write hook can tell the
    author WHICH property failed — the same reason the gate's refusal codes are distinct.
    """
    gaps: list[str] = []
    for check in CHECKS:
        gaps.extend(check(text))

    if gaps:
        return {"outcome": LINT_REFUSED, "exit_code": LINT_EXIT_CODE, "gaps": gaps}
    return {"outcome": LINT_OK, "exit_code": 0, "gaps": []}
