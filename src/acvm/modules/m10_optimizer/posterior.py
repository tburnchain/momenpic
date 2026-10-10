"""Arm posterior bookkeeping for the M10 bandit (§4 M10; model in `acvm.quant.bayes`).

Reward model per arm: clicks|views ~ Binomial(views, CTR) with CTR ~ Beta(α, β); orders|clicks ~
Poisson(λ·clicks) with λ ~ Gamma(a_λ, b_λ); commission/order ~ Exp(θ) with θ ~ Gamma(a_θ, b_θ).

Sufficient statistics of one publication's observation o = (I, C, O, K) — views, clicks, orders,
commission — after clamping s = min(C, I):

    Δα = s,  Δβ = I − s,  Δa_λ = O,  Δb_λ = C,  Δa_θ = O,  Δb_θ = K

Every update is a sum of these, so a *revised* observation o' of the same publication (insights and
reports keep growing during the 7-day reward horizon) is applied as the exact delta T(o') − T(o):
re-processing the same observation is a no-op and the posterior always equals prior + Σ latest
observations, regardless of how many times ``perf.updated`` was delivered.

Hierarchical prior (partial pooling): a new arm starts at revenue_prior(m_ctr, m_cvr, m_commission, κ)
where the means m are the posterior means of its parent — the pooled category × platform arms on top of
the §2.5 economics prior — and κ = settings.optimizer.prior_strength contents of pseudo-evidence
(CTR: κ·1 000 views, orders/click: κ·10 clicks, commission/order: κ·0.5 orders).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from acvm.config import EconomicsDefaults
from acvm.domain.models import ArmPosteriorState
from acvm.quant.bayes import BetaPosterior, GammaPosterior, RevenuePosterior


@dataclass(frozen=True, slots=True)
class Observation:
    """Cumulative reward of one publication inside the reward horizon."""

    impressions: float
    clicks: float
    orders: float
    commission_krw: float

    def sufficient(self) -> tuple[float, float, float, float, float, float]:
        """(Δα, Δβ, Δa_λ, Δb_λ, Δa_θ, Δb_θ) — see module docstring."""
        i = max(float(self.impressions), 0.0)
        c = max(float(self.clicks), 0.0)
        o = max(float(self.orders), 0.0)
        k = max(float(self.commission_krw), 0.0)
        s = min(c, i)
        return s, i - s, o, c, o, k

    @property
    def empty(self) -> bool:
        return self.impressions <= 0 and self.clicks <= 0 and self.orders <= 0 and self.commission_krw <= 0


_ZERO = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def to_posterior(state: ArmPosteriorState) -> RevenuePosterior:
    return RevenuePosterior(
        ctr=BetaPosterior(state.ctr_alpha, state.ctr_beta),
        orders_per_click=GammaPosterior(state.order_shape, state.order_rate),
        commission_rate_param=GammaPosterior(state.comm_shape, state.comm_rate),
        impressions=state.impressions, clicks=state.clicks, orders=state.orders,
        commission_krw=state.commission_krw, trials=state.trials)


def state_from_prior(arm_key: str, prior: RevenuePosterior, at: datetime) -> ArmPosteriorState:
    return ArmPosteriorState(
        arm_key=arm_key, ctr_alpha=prior.ctr.alpha, ctr_beta=prior.ctr.beta,
        order_shape=prior.orders_per_click.shape, order_rate=prior.orders_per_click.rate,
        comm_shape=prior.commission_rate_param.shape, comm_rate=prior.commission_rate_param.rate,
        impressions=0.0, clicks=0.0, orders=0.0, commission_krw=0.0, trials=0, retired=False, updated_at=at)


def apply_delta(state: ArmPosteriorState, new: Observation | None, old: Observation | None,
                at: datetime) -> ArmPosteriorState:
    """posterior ← posterior + T(new) − T(old); trials count publications (+1 new, −1 removed)."""
    n = new.sufficient() if new is not None else _ZERO
    o = old.sufficient() if old is not None else _ZERO
    d = [a - b for a, b in zip(n, o, strict=True)]

    def raw(field: str) -> float:
        return (max(getattr(new, field), 0.0) if new else 0.0) - (max(getattr(old, field), 0.0) if old else 0.0)

    return state.model_copy(update={
        "ctr_alpha": state.ctr_alpha + d[0], "ctr_beta": state.ctr_beta + d[1],
        "order_shape": state.order_shape + d[2], "order_rate": state.order_rate + d[3],
        "comm_shape": state.comm_shape + d[4], "comm_rate": state.comm_rate + d[5],
        "impressions": state.impressions + raw("impressions"), "clicks": state.clicks + raw("clicks"),
        "orders": state.orders + raw("orders"), "commission_krw": state.commission_krw + raw("commission_krw"),
        "trials": state.trials + (1 if new is not None else 0) - (1 if old is not None else 0),
        "updated_at": at,
    })


def revenue_prior(ctr_mean: float, cvr_mean: float, commission_mean_krw: float, strength: float) -> RevenuePosterior:
    """Prior worth ``strength`` contents: the CTR Beta carries ``strength × 1 000`` pseudo-impressions.

    A weaker CTR prior (e.g. ≈ 160 pseudo-views at κ = 20) lets the *maximum* of Thompson draws over
    hundreds of unexplored grid arms beat a proven winner in most exploit slots — see
    tests/unit/test_quant.py::test_revenue_prior_strength_is_in_contents.
    """
    return RevenuePosterior.prior(ctr_mean, cvr_mean, commission_mean_krw, strength)


def economics_prior(econ: EconomicsDefaults, strength: float) -> RevenuePosterior:
    """§2.5 funnel as a prior worth ``strength`` contents (CTR 0.8 %, CVR 3 %, 900 KRW/order)."""
    return revenue_prior(econ.ctr, econ.cvr, econ.aov_krw * econ.commission_rate, strength)


def pooled(states: Iterable[ArmPosteriorState]) -> Observation:
    imp = clk = ords = comm = 0.0
    for s in states:
        imp += s.impressions
        clk += s.clicks
        ords += s.orders
        comm += s.commission_krw
    return Observation(imp, clk, ords, comm)


def pooled_posterior(prior: RevenuePosterior, states: Iterable[ArmPosteriorState]) -> RevenuePosterior:
    """``prior`` updated with the pooled observed totals of ``states`` (trials = Σ arm trials)."""
    states = list(states)
    tot = pooled(states)
    post = prior.observe(max(tot.impressions, 0.0), max(tot.clicks, 0.0), max(tot.orders, 0.0),
                         max(tot.commission_krw, 0.0))
    return RevenuePosterior(post.ctr, post.orders_per_click, post.commission_rate_param, post.impressions,
                            post.clicks, post.orders, post.commission_krw, sum(s.trials for s in states))


def centred_prior(centre: RevenuePosterior, strength: float) -> RevenuePosterior:
    """Prior with the posterior means of ``centre`` and ``strength`` pseudo-contents of evidence."""
    return revenue_prior(centre.ctr.mean, centre.orders_per_click.mean, centre.mean_commission_per_order, strength)


def summary(state: ArmPosteriorState) -> dict[str, float]:
    """ArmUpdated.posterior payload: means, 90 % CTR interval and evidence counts."""
    post = to_posterior(state)
    lo, hi = post.ctr.interval(0.9)
    return {"ctr_mean": post.ctr.mean, "ctr_lo90": lo, "ctr_hi90": hi, "epc_mean": post.mean_epc,
            "rpi_mean": post.mean_rpi, "trials": float(state.trials), "impressions": state.impressions,
            "clicks": state.clicks, "orders": state.orders, "commission_krw": state.commission_krw,
            "retired": 1.0 if state.retired else 0.0}
