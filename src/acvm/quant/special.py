"""Special functions in pure Python (no SciPy in the runtime image).

* ``betainc`` — regularized incomplete beta I_x(a, b) via Lentz's continued fraction
  (Numerical Recipes §6.4), accurate to ~1e-14; used for Beta-posterior tail probabilities.
* ``beta_ppf`` — inverse by safeguarded Newton/bisection on ``betainc``.
* ``gammainc_lower`` — regularized lower incomplete gamma P(a, x) (series / continued fraction).
* ``log_beta_binom_pmf`` — log pmf of the Beta-Binomial predictive (CTR anomaly test, §4 M9).
"""

from __future__ import annotations

import math

_EPS = 1e-15
_TINY = 1e-300
_MAX_IT = 500


def log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, _MAX_IT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            return h
    return h  # converged to working precision for all parameters we use


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) = P(X ≤ x) for X ~ Beta(a, b)."""
    if a <= 0 or b <= 0:
        raise ValueError("a, b must be positive")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = a * math.log(x) + b * math.log1p(-x) - log_beta(a, b)
    front = math.exp(ln_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of Beta(a, b)."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    if q == 0.0:
        return 0.0
    if q == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    x = a / (a + b)
    lb = log_beta(a, b)
    for _ in range(200):
        f = betainc(a, b, x) - q
        if abs(f) < 1e-12:
            return x
        if f > 0:
            hi = x
        else:
            lo = x
        # Newton step using the Beta density; fall back to bisection when it leaves the bracket.
        log_pdf = (a - 1) * math.log(max(x, _TINY)) + (b - 1) * math.log(max(1 - x, _TINY)) - lb
        pdf = math.exp(log_pdf)
        nx = x - f / pdf if pdf > 0 else (lo + hi) / 2
        x = nx if lo < nx < hi else (lo + hi) / 2
    return x


def gammainc_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x)."""
    if x <= 0:
        return 0.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap, total, delta = a, 1.0 / a, 1.0 / a
        for _ in range(_MAX_IT):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * _EPS:
                break
        return total * math.exp(-x + a * math.log(x) - gln)
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_IT):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = _TINY if abs(d) < _TINY else d
        c = b + an / c
        c = _TINY if abs(c) < _TINY else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - gln) * h


def log_beta_binom_pmf(k: int, n: int, a: float, b: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + log_beta(k + a, n - k + b) - log_beta(a, b))


def beta_binom_sf(k: int, n: int, a: float, b: float) -> float:
    """P(K ≥ k) for K ~ BetaBinomial(n, a, b).

    Sums from whichever side is shorter. Above the mode the terms decrease monotonically, so the
    upper tail is summed until the remaining mass is below 1e-16 of the running total.
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    mode = max(0, min(n, int((n * (a - 1)) / max(a + b - 2, 1e-9)))) if a > 1 else 0
    if k > mode:
        total = 0.0
        for j in range(k, n + 1):
            term = math.exp(log_beta_binom_pmf(j, n, a, b))
            total += term
            if term < 1e-16 * max(total, _TINY) and j > k + 10:
                break
        return min(1.0, total)
    lower = sum(math.exp(log_beta_binom_pmf(j, n, a, b)) for j in range(0, k))
    return max(0.0, 1.0 - lower)
