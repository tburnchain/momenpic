"""Revenue engine (§2): funnel identity, system profit, break-even views, and the G3 admission test.

    E[R_{c,p}] = I · CTR · CVR · AOV · r                                   (§2.1)
    Π          = Σ_c Σ_p E[R_{c,p}] − N_c·C_unit − C_fixed − L_invalid − T     (§2.1)
    I*         = C_unit / (CTR · CVR · AOV · r)                             (§2.3)
    P(≥1 tail) = 1 − (1 − q)^n                                              (§2.2)

With the §2.5 example (C_unit 150, CTR 0.8 %, CVR 3 %, AOV 30 000, r 3 %) I* = 694.4 ≈ 700.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from acvm.quant.distributions import LogNormal


@dataclass(frozen=True, slots=True)
class Funnel:
    ctr: float
    cvr: float
    aov_krw: float
    commission_rate: float

    def __post_init__(self) -> None:
        for name in ("ctr", "cvr", "commission_rate"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name} must be a probability, got {v}")
        if self.aov_krw < 0:
            raise ValueError("aov must be non-negative")

    @property
    def commission_per_order(self) -> float:
        return self.aov_krw * self.commission_rate

    @property
    def revenue_per_impression(self) -> float:
        """RPI — expected commission per view."""
        return self.ctr * self.cvr * self.aov_krw * self.commission_rate

    @property
    def epc(self) -> float:
        """Earnings per click."""
        return self.cvr * self.aov_krw * self.commission_rate


def expected_revenue(impressions: float, funnel: Funnel) -> float:
    return impressions * funnel.revenue_per_impression


def breakeven_impressions(c_unit: float, funnel: Funnel) -> float:
    rpi = funnel.revenue_per_impression
    return math.inf if rpi <= 0 else c_unit / rpi


def system_profit(expected_revenues: Iterable[float], n_contents: int, c_unit: float, c_fixed: float,
                  l_invalid: float = 0.0, tax: float = 0.0) -> float:
    return sum(expected_revenues) - n_contents * c_unit - c_fixed - l_invalid - tax


def prob_at_least_one_tail(q: float, n: int) -> float:
    """P(at least one of n independent tries lands in the top-q quantile)."""
    if not 0 <= q <= 1:
        raise ValueError("q must be a probability")
    return 1.0 - (1.0 - q) ** n


def trials_for_tail(q: float, target: float) -> int:
    """Smallest n with 1 − (1 − q)^n ≥ target."""
    if not 0 < q < 1 or not 0 < target < 1:
        raise ValueError("q and target must be in (0, 1)")
    return math.ceil(math.log(1.0 - target) / math.log(1.0 - q))


@dataclass(frozen=True, slots=True)
class ContentEconomics:
    expected_impressions: float
    expected_revenue: float
    expected_profit: float
    breakeven_views: float
    p_breakeven: float  # P(I ≥ I*) for one content
    tail_share_top1: float
    tail_share_top5: float


def content_economics(views: LogNormal, funnel: Funnel, c_unit: float) -> ContentEconomics:
    i_star = breakeven_impressions(c_unit, funnel)
    mean_views = views.mean
    revenue = expected_revenue(mean_views, funnel)
    return ContentEconomics(
        expected_impressions=mean_views,
        expected_revenue=revenue,
        expected_profit=revenue - c_unit,
        breakeven_views=i_star,
        p_breakeven=views.sf(i_star),
        tail_share_top1=views.top_share(0.01),
        tail_share_top5=views.top_share(0.05),
    )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """G3: expand a channel only once its median views clear I* (§11)."""

    admitted: bool
    median_views: float
    median_ci: tuple[float, float]
    breakeven_views: float
    ratio: float  # KPI #1 (§2.3): channel median / I*
    n: int
    reason: str


def channel_admission(view_samples: Sequence[float], funnel: Funnel, c_unit: float, *,
                      min_samples: int = 30, confidence: float = 0.8,
                      require_lower_bound: bool = False) -> AdmissionDecision:
    """Decide whether a channel clears the break-even bar.

    The point test is the design rule (median ≥ I*). ``require_lower_bound`` tightens it to the
    lower confidence bound of the median, which protects against admitting a channel on the
    strength of a lucky first month.
    """
    i_star = breakeven_impressions(c_unit, funnel)
    positive = [v for v in view_samples if v > 0]
    n = len(positive)
    if n < max(2, min_samples):
        # Not enough evidence to *decide*, but the observed median is still reported — a dashboard that
        # shows "median 0" for a channel whose posts got thousands of views would mislead the operator.
        if n == 0:
            return AdmissionDecision(False, 0.0, (0.0, math.inf), i_star, 0.0, 0, "no posts with views yet")
        med = statistics.median(positive)
        ci = LogNormal.fit(positive).median_ci(n, confidence) if n >= 2 else (0.0, math.inf)
        return AdmissionDecision(False, med, ci, i_star, med / i_star if i_star > 0 else math.inf, n,
                                 f"insufficient data: {n} < {min_samples} posts (median {med:,.0f} vs I* {i_star:,.0f})")
    dist = LogNormal.fit(positive)
    lo, hi = dist.median_ci(n, confidence)
    med = dist.median
    test = lo if require_lower_bound else med
    ratio = med / i_star if i_star > 0 else math.inf
    admitted = test >= i_star
    reason = (f"median {med:,.0f} (CI {lo:,.0f}–{hi:,.0f}) vs I* {i_star:,.0f}: "
              + ("admitted" if admitted else "held — expansion deferred"))
    return AdmissionDecision(admitted, med, (lo, hi), i_star, ratio, n, reason)


def platform_worth_publishing(expected_views: float, expected_epc: float, expected_ctr: float,
                              marginal_cost_krw: float) -> bool:
    """§7.2: publish to a platform only if its expected revenue covers the marginal variant cost.
    Expected revenue = views · CTR · EPC. (Marginal cost of an extra platform variant is small —
    re-cut + upload — but non-zero, and it also consumes scarce quota.)"""
    return expected_views * expected_ctr * expected_epc >= marginal_cost_krw


@dataclass(frozen=True, slots=True)
class MoneyView:
    """§2.4 — GMV, confirmed commission and net profit are never conflated."""

    gmv_krw: float
    commission_krw: float
    cancelled_krw: float
    cost_krw: float

    @property
    def net_commission_krw(self) -> float:
        return self.commission_krw - self.cancelled_krw

    @property
    def net_profit_krw(self) -> float:
        return self.net_commission_krw - self.cost_krw

    @property
    def effective_rate(self) -> float:
        return self.net_commission_krw / self.gmv_krw if self.gmv_krw else 0.0
