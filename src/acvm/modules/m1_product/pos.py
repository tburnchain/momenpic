"""Product Opportunity Score, model ``pos-v1`` (§4 M1).

Design formula::

    POS = w₁·log(1+reviews) + w₂·rating + w₃·𝟙_rocket + w₄·f_price(p) + w₅·r_cat + w₆·V_demo
          + w₇·M_trend + w₈·EPC_bayes − λ·P_risk

Every feature is first mapped onto [0, 1] (``acvm.quant.features``), so the positive weights sum to 1 and
each wᵢ reads as "share of a perfect score"; POS is then clipped to [0, 1]. The design's ``min_pos = 0.7``
(부록 A) is a threshold on this normalised scale. Normalisations:

    log_reviews   = log(1+n) / log(1+10 000)                      saturates at 10k reviews ("fully proven")
    rating        = (★ − 3) / 2, clipped                            3★ → 0, 5★ → 1
    rocket        = 𝟙[로켓배송]
    price         = 1 inside the impulse band 1만–5만 원, log-Gaussian decay outside (σ = 0.6)
    r_cat         = min(1, r̂_cat / r_ref)                           r_ref = economics.commission_rate;
                                                                    a commission cut lowers POS automatically
    V_demo        = Scout's 영상화 적합도 (``vdemo``)
    M_trend       = 1 / (1 + e^(−g/0.5))                            g = blended 7-day growth; g = 0 → 0.5
    EPC_bayes     = Ê / (Ê + E_ref)                                 Ê = (revenue + κ·E_prior)/(clicks + κ), κ = 50
                                                                    E_ref = E_prior = CVR·AOV·r (§2.5) → new = 0.5
    P_risk        = 1 − (1 − p_cat)·(1 − 0.35)^k                   p_cat category return-risk prior,
                                                                    k = past sold-out/discontinued incidents

Missing rating / review count (the Partners API returns neither) are imputed from category priors and
named in ``ProductFeatures.imputed``; so is M_trend when there is < 12 h of snapshot history.

Weights (pos-v1): reviews .14, rating .10, rocket .08, price .12, r_cat .06, V_demo .22, M_trend .10,
EPC .18 (Σ = 1), λ = .30. V_demo and EPC dominate because the design's 2차 KPI levers for M1 are
"영상화 적합도" and CVR × AOV (§2.5); λ = .30 means a fully risky product loses ~30 points — combined with
the hard exclusion of regulated goods it can never reach the 0.7 bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

from acvm.config import Settings
from acvm.domain.models import Product, ProductFeatures
from acvm.modules.m1_product.priors import CategoryPrior
from acvm.quant.features import normalise_epc, price_affinity, rating_score, saturating_log, trend_momentum

MODEL_VERSION = "pos-v1"


@dataclass(frozen=True, slots=True)
class PosWeights:
    reviews: float = 0.14
    rating: float = 0.10
    rocket: float = 0.08
    price: float = 0.12
    r_cat: float = 0.06
    v_demo: float = 0.22
    m_trend: float = 0.10
    epc: float = 0.18
    risk_lambda: float = 0.30

    def __post_init__(self) -> None:
        values = [getattr(self, f.name) for f in fields(self)]
        if any(v < 0 for v in values):
            raise ValueError("POS weights must be non-negative")
        positive = sum(values) - self.risk_lambda
        if not math.isclose(positive, 1.0, abs_tol=1e-9):
            raise ValueError(f"positive POS weights must sum to 1, got {positive:.6f}")


@dataclass(frozen=True, slots=True)
class RawSignals:
    """Un-normalised per-product inputs gathered by ``features.EvidenceCollector``."""

    category_rate: float  # shrunk observed commission rate r̂_cat
    v_demo: float  # [0, 1]
    trend_growth: float | None  # blended 7-day relative growth g; None → insufficient history
    epc_bayes_krw: float  # posterior-mean EPC (KRW per click)
    p_risk: float  # [0, 1]


def _clip(x: float) -> float:
    return min(1.0, max(0.0, x))


class PosModel:
    """Versioned POS: normalises raw signals to ``ProductFeatures`` and scores them."""

    model_version = MODEL_VERSION
    review_saturation = 10_000
    trend_scale = 0.5

    def __init__(self, *, epc_ref_krw: float, rate_ref: float, weights: PosWeights | None = None) -> None:
        if epc_ref_krw <= 0 or rate_ref <= 0:
            raise ValueError("EPC and commission-rate references must be positive")
        self.weights = weights or PosWeights()
        self.epc_ref_krw = epc_ref_krw
        self.rate_ref = rate_ref

    @classmethod
    def from_settings(cls, settings: Settings, weights: PosWeights | None = None) -> PosModel:
        e = settings.economics
        return cls(epc_ref_krw=e.cvr * e.aov_krw * e.commission_rate, rate_ref=e.commission_rate, weights=weights)

    @property
    def prior_epc_krw(self) -> float:
        """E_prior = CVR·AOV·r (§2.5: 3 % × 30 000 × 3 % = 27 KRW/click)."""
        return self.epc_ref_krw

    def features(self, product: Product, signals: RawSignals, prior: CategoryPrior) -> ProductFeatures:
        imputed: list[str] = []
        reviews = product.review_count
        if reviews is None:
            reviews = prior.review_count
            imputed.append("log_reviews")
        rating = product.rating
        if rating is None:
            rating = prior.rating
            imputed.append("rating")
        growth = signals.trend_growth
        if growth is None:
            growth = 0.0
            imputed.append("m_trend")
        return ProductFeatures(
            log_reviews=round(saturating_log(float(reviews), float(self.review_saturation)), 6),
            rating=round(rating_score(rating), 6),
            rocket=1.0 if product.is_rocket else 0.0,
            price_affinity=round(price_affinity(float(product.price)), 6),
            r_cat=round(_clip(signals.category_rate / self.rate_ref), 6),
            v_demo=round(_clip(signals.v_demo), 6),
            m_trend=round(trend_momentum(growth, self.trend_scale), 6),
            epc_bayes=round(normalise_epc(signals.epc_bayes_krw, self.epc_ref_krw), 6),
            p_risk=round(1.0 if product.regulated else _clip(signals.p_risk), 6),
            imputed=imputed,
        )

    def contributions(self, f: ProductFeatures) -> dict[str, float]:
        """Signed per-feature contribution to POS (explainability for the console / Analyst agent)."""
        w = self.weights
        return {
            "log_reviews": w.reviews * f.log_reviews,
            "rating": w.rating * f.rating,
            "rocket": w.rocket * f.rocket,
            "price_affinity": w.price * f.price_affinity,
            "r_cat": w.r_cat * f.r_cat,
            "v_demo": w.v_demo * f.v_demo,
            "m_trend": w.m_trend * f.m_trend,
            "epc_bayes": w.epc * f.epc_bayes,
            "p_risk": -w.risk_lambda * f.p_risk,
        }

    def pos(self, f: ProductFeatures) -> float:
        return round(_clip(sum(self.contributions(f).values())), 6)
