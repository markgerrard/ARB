"""The viability oracle (schema v0.2 §0.5 / §4).

A seat is *viable* on a class iff its detection rate is DEMONSTRABLY above its own
spurious-match (noise) floor. This is a capability floor (can-it-catch), NOT a graded
reliability score (how-often) — see docs/eval-instrument1-v0-schema.md and
docs/measurement-principles.md P2 (the wall stops the writer, not the reader).

The precision gate (P0-B) is INTRINSIC here: a flag-everything seat has
caught_rate ~= noise_rate, so it is not above noise -> FAIL, with no bolted-on fp_max.
"""
from __future__ import annotations

from dataclasses import dataclass

from .stats import wilson

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


@dataclass(frozen=True)
class ViabilityResult:
    verdict: str            # PASS / FAIL / UNKNOWN
    caught_k: int
    caught_n: int
    noise_k: int
    noise_n: int
    alpha: float
    # raw CIs kept here for the DETAIL file only; never rendered in the headline grid (P1-G).
    caught_lo: float
    caught_hi: float
    noise_lo: float
    noise_hi: float


def classify(caught_k: int, caught_n: int, noise_k: int, noise_n: int,
             alpha: float = 0.05) -> ViabilityResult:
    c = wilson(caught_k, caught_n, alpha)
    nu = wilson(noise_k, noise_n, alpha)
    if c.lo > nu.hi:
        verdict = PASS          # detection demonstrably ABOVE own noise -> viable
    elif c.hi <= nu.lo:
        verdict = FAIL          # detection demonstrably BELOW own noise -> a real failure mode
    else:
        verdict = UNKNOWN       # intervals overlap (incl. AT noise) -> can't tell; NOT a FAIL.
        # NOTE: a seat exactly AT its noise floor resolves UNKNOWN, not FAIL — that is correct
        # conservative behaviour. The power budget therefore powers PASS-detection of a *viable*
        # (>= nu+V) seat; it does NOT promise to resolve a noise seat to FAIL (impossible: equal
        # rates -> overlapping CIs). See docs/eval-instrument1-build-review.md finding #4.
    return ViabilityResult(verdict, caught_k, caught_n, noise_k, noise_n, alpha,
                           c.lo, c.hi, nu.lo, nu.hi)
