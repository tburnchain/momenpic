"""Bayesian machinery for M1 (EPC shrinkage) and M10 (Thompson-sampling bandit).

Reward model for one arm (category × hook × template × persona × platform × slot):

    clicks | impressions ~ Binomial(I, CTR),            CTR ~ Beta(α, β)
    orders | clicks      ~ Poisson(λ · clicks),         λ   ~ Gamma(a_λ, b_λ)     (orders per click)
    commission per order ~ Exponential(θ),              θ   ~ Gamma(a_θ, b_θ)     (mean 1/θ)

All three are conjugate, so posteriors update in O(1):

    α += clicks, β += I − clicks;  a_λ += orders, b_λ += clicks;  a_θ += orders, b_θ += Σ commission

Revenue per impression is RPI = CTR · λ · (1/θ); a Thompson draw samples each factor independently
from its posterior (they are a-posteriori independent because they are informed by disjoint data).

Hierarchical shrinkage: a sparse arm's prior is centred on its parent's posterior mean with
strength κ pseudo-observations, i.e. partial pooling. A brand-new arm therefore starts at its
category/platform average rather than at an uninformative prior that would over-explore it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from acvm.quant.special import beta_ppf, betainc


@dataclass(frozen=True, slots=True)
class BetaPosterior:
    alpha: float = 1.0
    beta: float = 1.0

    def update(self, successes: float, failures: float) -> BetaPosterior:
        if successes < 0 or failures < 0:
            raise ValueError("counts must be non-negative")
        return BetaPosterior(self.alpha + successes, self.beta + failures)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        s = self.alpha + self.beta
        return self.alpha * self.beta / (s * s * (s + 1))

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)

    def cdf(self, x: float) -> float:
        return betainc(self.alpha, self.beta, x)

    def prob_greater_than(self, x: float) -> float:
        return 1.0 - self.cdf(x)

    def interval(self, confidence: float = 0.9) -> tuple[float, float]:
        lo = (1 - confidence) / 2
        return (beta_ppf(lo, self.alpha, self.beta), beta_ppf(1 - lo, self.alpha, self.beta))

    @classmethod
    def from_mean(cls, mean: float, strength: float) -> BetaPosterior:
        mean = min(max(mean, 1e-6), 1 - 1e-6)
        return cls(mean * strength, (1 - mean) * strength)


@dataclass(frozen=True, slots=True)
class GammaPosterior:
    """Gamma(shape, rate) — conjugate for Poisson rates and Exponential rates."""

    shape: float = 1.0
    rate: float = 1.0

    def update(self, count: float, exposure: float) -> GammaPosterior:
        if count < 0 or exposure < 0:
            raise ValueError("count/exposure must be non-negative")
        return GammaPosterior(self.shape + count, self.rate + exposure)

    @property
    def mean(self) -> float:
        return self.shape / self.rate

    @property
    def variance(self) -> float:
        return self.shape / (self.rate * self.rate)

    def sample(self, rng: random.Random) -> float:
        return rng.gammavariate(self.shape, 1.0 / self.rate)

    @classmethod
    def from_mean(cls, mean: float, strength: float) -> GammaPosterior:
        mean = max(mean, 1e-9)
        return cls(mean * strength, strength)


def shrink_epc(clicks: float, revenue_krw: float, prior_epc: float, kappa: float) -> float:
    """§4 M1 EPC_bayes: posterior-mean EPC with κ pseudo-clicks at the category prior.

        EPC_bayes = (revenue + κ · EPC_prior) / (clicks + κ)

    A new product with 3 lucky clicks cannot outrank an established one: with κ = 50 its estimate
    moves only 3/53 ≈ 6 % of the way from the prior towards the observed value.
    """
    if clicks < 0 or kappa <= 0:
        raise ValueError("clicks ≥ 0 and κ > 0 required")
    return (revenue_krw + kappa * prior_epc) / (clicks + kappa)


def fit_beta_prior(rates: Sequence[float], weights: Sequence[float] | None = None,
                   max_strength: float = 1_000.0) -> BetaPosterior:
    """Empirical-Bayes method-of-moments Beta fit across sibling arms."""
    if not rates:
        return BetaPosterior(1.0, 1.0)
    w = list(weights) if weights is not None else [1.0] * len(rates)
    tw = sum(w)
    m = sum(r * wi for r, wi in zip(rates, w, strict=True)) / tw
    v = sum(wi * (r - m) ** 2 for r, wi in zip(rates, w, strict=True)) / tw
    m = min(max(m, 1e-6), 1 - 1e-6)
    if v <= 0 or v >= m * (1 - m):
        return BetaPosterior.from_mean(m, min(max_strength, 50.0))
    strength = min(m * (1 - m) / v - 1, max_strength)
    return BetaPosterior.from_mean(m, max(strength, 2.0))


IMPRESSIONS_PER_CONTENT = 1_000.0  # pseudo-views one content's worth of prior evidence contributes to CTR


@dataclass(frozen=True, slots=True)
class RevenuePosterior:
    """Posterior over revenue-per-impression for one arm."""

    ctr: BetaPosterior
    orders_per_click: GammaPosterior
    commission_rate_param: GammaPosterior  # θ; commission/order ~ Exp(θ) → mean 1/θ
    impressions: float = 0.0
    clicks: float = 0.0
    orders: float = 0.0
    commission_krw: float = 0.0
    trials: int = 0

    @classmethod
    def prior(cls, ctr_mean: float, cvr_mean: float, commission_mean_krw: float,
              strength: float) -> RevenuePosterior:
        """Prior worth ``strength`` contents' worth of evidence.

        CTR strength is expressed in impressions (strength × 1 000 views); order-rate strength in
        clicks; commission strength in orders — each scaled so the prior is weak relative to a
        few weeks of real data but strong enough to stop single-sample whiplash.
        """
        ctr = BetaPosterior.from_mean(ctr_mean, max(strength * IMPRESSIONS_PER_CONTENT, 2.0))
        clicks_strength = max(strength * 10.0, 1.0)
        opc = GammaPosterior.from_mean(cvr_mean, clicks_strength)
        orders_strength = max(strength * 0.5, 1.0)
        # θ prior: Gamma(a, b) with E[1/θ] = b/(a−1) = commission_mean
        a = orders_strength + 1.0
        b = orders_strength * commission_mean_krw
        return cls(ctr, opc, GammaPosterior(a, b))

    def observe(self, impressions: float, clicks: float, orders: float,
                commission_krw: float) -> RevenuePosterior:
        clicks = min(clicks, impressions) if impressions > 0 else clicks
        return replace(
            self,
            ctr=self.ctr.update(clicks, max(impressions - clicks, 0.0)),
            orders_per_click=self.orders_per_click.update(orders, clicks),
            commission_rate_param=self.commission_rate_param.update(orders, commission_krw),
            impressions=self.impressions + impressions,
            clicks=self.clicks + clicks,
            orders=self.orders + orders,
            commission_krw=self.commission_krw + commission_krw,
            trials=self.trials + 1,
        )

    @property
    def mean_commission_per_order(self) -> float:
        a, b = self.commission_rate_param.shape, self.commission_rate_param.rate
        return b / (a - 1.0) if a > 1.0 else b  # E[1/θ]

    @property
    def mean_rpi(self) -> float:
        return self.ctr.mean * self.orders_per_click.mean * self.mean_commission_per_order

    @property
    def mean_epc(self) -> float:
        return self.orders_per_click.mean * self.mean_commission_per_order

    def sample_rpi(self, rng: random.Random) -> float:
        theta = self.commission_rate_param.sample(rng)
        return self.ctr.sample(rng) * self.orders_per_click.sample(rng) / max(theta, 1e-12)

    def sample_epc(self, rng: random.Random) -> float:
        theta = self.commission_rate_param.sample(rng)
        return self.orders_per_click.sample(rng) / max(theta, 1e-12)


@dataclass(slots=True)
class ArmCandidate:
    key: str
    posterior: RevenuePosterior
    expected_views: float = 1.0
    retired: bool = False
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Allocation:
    key: str
    mode: str  # "explore" | "exploit"
    sampled_value: float


def thompson_allocate(arms: Sequence[ArmCandidate], n: int, rng: random.Random, *,
                      exploration_budget: float = 0.20, young_trials: int = 30) -> list[Allocation]:
    """Allocate n production slots across arms (§4 M10, 20 % exploration budget).

    * ⌈budget·n⌉ slots go to *young* arms (< ``young_trials`` trials) by Thompson sampling — this is
      where new hooks/templates earn evidence;
    * the remaining slots go to *mature* arms by Thompson sampling on expected revenue per content
      (sampled RPI × expected views), which concentrates on winners while still hedging among them.

    Why exploit draws exclude young arms: with K untried arms the maximum of K posterior draws grows
    like μ·exp(σ·Φ⁻¹(1 − 1/K)) — for K ≈ 300 and a prior CV ≈ 0.5 that is ≈ 4× the prior mean, which
    beats any realistic proven winner. Plain Thompson over everything would therefore spend ~100 % of
    production exploring, silently violating the budget. Until at least one arm is mature, every slot
    is exploration by definition and all live arms compete.

    Retired arms (§4 M10 retirement rule) receive nothing. Each draw is fresh, so the same arm can
    win several slots when its posterior dominates — that is the intended exploit behaviour.
    """
    live = [a for a in arms if not a.retired]
    if n <= 0 or not live:
        return []
    young = [a for a in live if a.posterior.trials < young_trials]
    mature = [a for a in live if a.posterior.trials >= young_trials]
    if not mature:
        young, n_explore = live, n
    else:
        n_explore = min(math.ceil(exploration_budget * n), n) if young else 0
    exploit_pool = mature or live
    out: list[Allocation] = []

    def draw(pool: Sequence[ArmCandidate], mode: str) -> Allocation:
        best_key, best_val = "", -math.inf
        for arm in pool:
            val = arm.posterior.sample_rpi(rng) * arm.expected_views
            if val > best_val:
                best_key, best_val = arm.key, val
        return Allocation(best_key, mode, best_val)

    for _ in range(n_explore):
        out.append(draw(young, "explore"))
    for _ in range(n - n_explore):
        out.append(draw(exploit_pool, "exploit"))
    return out


def retirement_threshold(posterior_epcs: Sequence[float], percentile: float = 0.10) -> float:
    """EPC value below which mature arms are retired (bottom-decile rule)."""
    if not posterior_epcs:
        return -math.inf
    ordered = sorted(posterior_epcs)
    idx = max(0, min(len(ordered) - 1, math.floor(percentile * (len(ordered) - 1))))
    return ordered[idx]
