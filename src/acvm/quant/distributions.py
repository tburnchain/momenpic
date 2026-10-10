"""Heavy-tailed view-count model (§2.2).

Short-form views are modelled as I ~ LogNormal(μ, σ). Key identities used across ACVM:

* mean           E[I] = median · e^{σ²/2}                    (σ = 2 → 7.39× the median)
* tail share     share of total views produced by the top-q fraction of contents
                 S(q) = 1 − Φ(Φ⁻¹(1 − q) − σ)               (σ = 2: top 1 % → 37 %, top 5 % → 64 %)
* conditional    E[I | I > t] = E[I] · Φ(σ − z) / (1 − Φ(z)),  z = (ln t − μ)/σ
* exceedance     P(I ≥ t) = 1 − Φ(z)

These make the "100 tries, one hits" folklore precise and give the platform-admission gate (G3) a
confidence interval instead of a point estimate.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

N01 = statistics.NormalDist()


def phi(x: float) -> float:
    return N01.cdf(x)


def phi_inv(p: float) -> float:
    return N01.inv_cdf(min(max(p, 1e-15), 1 - 1e-15))


@dataclass(frozen=True, slots=True)
class LogNormal:
    mu: float
    sigma: float

    def __post_init__(self) -> None:
        if self.sigma <= 0:
            raise ValueError("sigma must be positive")

    # ── construction ──
    @classmethod
    def from_median(cls, median: float, sigma: float) -> LogNormal:
        if median <= 0:
            raise ValueError("median must be positive")
        return cls(math.log(median), sigma)

    @classmethod
    def fit(cls, samples: Sequence[float], *, robust: bool = True, min_sigma: float = 0.05) -> LogNormal:
        """Fit on log-values. ``robust`` uses median/MAD (1.4826·MAD ≈ σ under normality), which a
        single viral outlier cannot drag — important because the outliers are the point here."""
        logs = [math.log(x) for x in samples if x > 0]
        if len(logs) < 2:
            raise ValueError("need at least two positive samples")
        if robust:
            mu = statistics.median(logs)
            mad = statistics.median([abs(v - mu) for v in logs])
            sigma = 1.4826 * mad
            if sigma < min_sigma:  # degenerate MAD (many ties) → fall back to the sample stdev
                sigma = statistics.stdev(logs)
        else:
            mu = statistics.fmean(logs)
            sigma = statistics.stdev(logs)
        return cls(mu, max(sigma, min_sigma))

    # ── moments & quantiles ──
    @property
    def median(self) -> float:
        return math.exp(self.mu)

    @property
    def mean(self) -> float:
        return math.exp(self.mu + self.sigma**2 / 2)

    @property
    def mean_to_median(self) -> float:
        return math.exp(self.sigma**2 / 2)

    def quantile(self, p: float) -> float:
        return math.exp(self.mu + self.sigma * phi_inv(p))

    def cdf(self, x: float) -> float:
        return 0.0 if x <= 0 else phi((math.log(x) - self.mu) / self.sigma)

    def sf(self, x: float) -> float:
        return 1.0 - self.cdf(x)

    def top_share(self, q: float) -> float:
        """Fraction of the total (expected) mass contributed by the top-q fraction of draws."""
        if not 0 < q < 1:
            raise ValueError("q must be in (0, 1)")
        return 1.0 - phi(phi_inv(1.0 - q) - self.sigma)

    def conditional_mean_above(self, t: float) -> float:
        if t <= 0:
            return self.mean
        z = (math.log(t) - self.mu) / self.sigma
        tail = 1.0 - phi(z)
        if tail < 1e-300:
            return t
        return self.mean * phi(self.sigma - z) / tail

    def percentile_of(self, x: float) -> float:
        return self.cdf(x)

    def median_ci(self, n: int, confidence: float = 0.8) -> tuple[float, float]:
        """CI for the median when μ was estimated from n log-samples: μ̂ ± z·σ/√n."""
        if n <= 0:
            return (0.0, math.inf)
        z = phi_inv(0.5 + confidence / 2)
        half = z * self.sigma / math.sqrt(n)
        return (math.exp(self.mu - half), math.exp(self.mu + half))


def percentile_rank(value: float, population: Sequence[float]) -> float:
    """Mid-rank empirical percentile in [0, 1] (ties count half)."""
    if not population:
        return 0.5
    below = sum(1 for v in population if v < value)
    equal = sum(1 for v in population if v == value)
    return (below + 0.5 * equal) / len(population)


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> tuple[float, float]:
    if trials <= 0:
        return (0.0, 1.0)
    z = phi_inv(0.5 + confidence / 2)
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0
