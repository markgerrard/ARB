"""Wilson score interval — the one statistical primitive the floor suite rests on.

Pure stdlib (no scipy): the floor suite must run anywhere the bridge runs.
Reference: Wilson (1927) score interval for a binomial proportion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# z for common two-sided alphas (avoids a scipy dependency for the normal quantile).
_Z = {0.10: 1.6448536269514722, 0.05: 1.959963984540054, 0.01: 2.5758293035489004}


def z_for(alpha: float) -> float:
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0,1), got {alpha}")
    if alpha in _Z:
        return _Z[alpha]
    # Acklam's inverse-normal approximation for arbitrary alpha (two-sided).
    p = 1.0 - alpha / 2.0
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow = 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= 1 - plow:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float
    point: float  # observed proportion k/n


def wilson(k: int, n: int, alpha: float = 0.05) -> Interval:
    """Wilson score interval for k successes in n trials at the given two-sided alpha."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"wilson: need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return Interval(0.0, 1.0, 0.0)
    z = z_for(alpha)
    phat = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    return Interval(max(0.0, center - half), min(1.0, center + half), phat)


def cohen_kappa(a: list[str], b: list[str]) -> float:
    if len(a) != len(b):
        raise ValueError("cohen_kappa: raters must have the same number of labels")
    if not a:
        raise ValueError("cohen_kappa: empty inputs")
    n = len(a)
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = 0.0
    for label in labels:
        pa = sum(1 for x in a if x == label) / n
        pb = sum(1 for x in b if x == label) / n
        expected += pa * pb
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)
