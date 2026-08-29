"""Power budget (schema v0.2 §0.5) — the A/D/E consolidation.

Set ONE target (V = viable effect size above noise, alpha, I_min, R_min, matcher_gate),
DERIVE everything else (trials T, instance/repeat split, gold-set size, required control
loci, expected UNKNOWN fraction). Do not set the derived numbers independently — that was
the v0.1 bug (three incoherent numbers).

HARD RULE (build-review P0): an unresolvable target must RAISE, never silently return the
loop cap as if it were a converged answer. Returning cap=2000 as a real budget is the exact
shape of the T=3 bug — a coherently-wrong number a user trusts.
"""
from __future__ import annotations

from dataclasses import dataclass

from .stats import wilson


class Unachievable(ValueError):
    """The requested target cannot be resolved within the trial cap (raise, never return cap)."""


@dataclass(frozen=True)
class PowerTarget:
    V: float = 0.40           # viable effect size: true catch-rate must exceed noise by >= V to PASS
    nu: float = 0.10          # assumed noise floor for the worked calc (measured per-seat at run time)
    alpha: float = 0.05
    I_min: int = 5            # min distinct instances/class for a CLASS-level claim (else instance-level)
    R_min: int = 3            # min repeats/instance for stochastic stability
    matcher_gate: float = 0.85
    matcher_true_recall: float = 0.95  # assumed true matcher recall for sizing the gold set

    def validate(self) -> None:
        if not (0.0 < self.alpha < 1.0):
            raise Unachievable(f"alpha must be in (0,1), got {self.alpha}")
        if not (0.0 < self.V < 1.0):
            raise Unachievable(f"V must be in (0,1), got {self.V}")
        if not (0.0 <= self.nu < 1.0):
            raise Unachievable(f"nu must be in [0,1), got {self.nu}")
        if self.nu + self.V >= 1.0:
            raise Unachievable(f"nu+V must be < 1 (got nu={self.nu}, V={self.V})")
        if not (0.0 < self.matcher_gate < 1.0):
            raise Unachievable(f"matcher_gate must be in (0,1), got {self.matcher_gate}")
        if not (self.matcher_gate < self.matcher_true_recall < 1.0):
            raise Unachievable("matcher_true_recall must be in (matcher_gate, 1)")
        if self.I_min < 1 or self.R_min < 1:
            raise Unachievable("I_min and R_min must be >= 1")


@dataclass(frozen=True)
class PowerBudget:
    target: PowerTarget
    trials: int                   # T per (seat, class)
    instances: int                # >= I_min
    repeats: int                  # >= R_min   (instances * repeats >= T)
    gold_per_seat: int            # G_s
    control_loci_required: int    # >= this many control loci/class or the budget under-delivers (P1-2)
    expected_unknown_band: float  # width of the true-rate band that resolves UNKNOWN at T


def _min_trials_above_noise(V: float, nu: float, alpha: float, cap: int = 2000) -> int:
    """Smallest n at which a seat with true catch-rate (nu+V) RESOLVES PASS under the
    *actual* viability test the oracle runs: caught lower-CI > noise upper-CI. Both CIs
    are real (noise estimated over n control loci -> L=n assumption, surfaced by the
    caller). RAISES if not resolvable within `cap` (never returns cap as a budget)."""
    target_rate = nu + V
    for n in range(2, cap + 1):
        caught = wilson(round(target_rate * n), n, alpha)
        noise = wilson(round(nu * n), n, alpha)
        if caught.lo > noise.hi:
            return n
    raise Unachievable(
        f"effect size V={V} not resolvable at nu={nu}, alpha={alpha} within {cap} trials "
        f"— increase V or nu, or loosen alpha"
    )


def _split(T: int, I_min: int, R_min: int) -> tuple[int, int]:
    """Pick (instances, repeats) with both >= minima and product >= T, instances slightly
    favored (instance diversity is the construct-validity axis; repeats only stochastic)."""
    import math
    instances = max(I_min, math.ceil(math.sqrt(T)))
    repeats = max(R_min, math.ceil(T / instances))
    return instances, repeats


def _min_gold(gate: float, true_recall: float, alpha: float, cap: int = 2000) -> int:
    """Smallest per-seat gold size whose Wilson-lower of an observed `true_recall` clears
    the matcher gate (fixes v0.1 P1-D). RAISES if unachievable within cap."""
    for n in range(2, cap + 1):
        if wilson(round(true_recall * n), n, alpha).lo >= gate:
            return n
    raise Unachievable(
        f"matcher_gate={gate} not clearable at true_recall={true_recall}, alpha={alpha} "
        f"within {cap} gold pairs"
    )


def _unknown_band(T: int, nu: float, alpha: float) -> float:
    """Width of the true-rate interval above nu that still resolves UNKNOWN at T."""
    noise_hi = wilson(round(nu * T), T, alpha).hi
    r = nu
    while r <= 1.0:
        if wilson(round(r * T), T, alpha).lo > noise_hi:
            return round(r - nu, 3)
        r += 0.01
    return round(1.0 - nu, 3)


def compute(target: PowerTarget | None = None) -> PowerBudget:
    t = target or PowerTarget()
    t.validate()                                  # raise on bad params (P2) before any calc
    T = _min_trials_above_noise(t.V, t.nu, t.alpha)   # raises Unachievable, never returns cap (P0)
    instances, repeats = _split(T, t.I_min, t.R_min)
    gold = _min_gold(t.matcher_gate, t.matcher_true_recall, t.alpha)
    band = _unknown_band(T, t.nu, t.alpha)
    return PowerBudget(t, T, instances, repeats, gold, control_loci_required=T,
                       expected_unknown_band=band)
