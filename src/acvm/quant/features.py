"""Feature transforms for the Product Opportunity Score (§4 M1).

    POS = w₁·log(1+reviews) + w₂·rating + w₃·𝟙_rocket + w₄·f_price(p) + w₅·r_cat + w₆·V_demo
          + w₇·M_trend + w₈·EPC_bayes − λ·P_risk

Raw features live on wildly different scales (log reviews ≈ 0–12, rating 0–5, EPC in KRW), so each
is mapped to [0, 1] before weighting; weights then mean "share of the score" and stay comparable
when M10 re-fits them. The design's min_pos = 0.7 threshold refers to this normalised scale.
"""

from __future__ import annotations

import math


def saturating_log(x: float, x_ref: float) -> float:
    """log(1+x)/log(1+x_ref), clipped to [0, 1]; x_ref is the count treated as 'fully proven'."""
    if x <= 0:
        return 0.0
    return min(1.0, math.log1p(x) / math.log1p(x_ref))


def price_affinity(price_krw: float, lo: float = 10_000, hi: float = 50_000, decay: float = 0.6) -> float:
    """f_price: 1.0 inside the impulse-buy band [1만, 5만], Gaussian decay in log-price outside.

    decay is the log-space σ: at 5 000 KRW (half of lo) f ≈ exp(−(ln 2)²/(2·0.36)) ≈ 0.51.
    """
    if price_krw <= 0:
        return 0.0
    if lo <= price_krw <= hi:
        return 1.0
    edge = lo if price_krw < lo else hi
    d = math.log(price_krw / edge)
    return math.exp(-(d * d) / (2 * decay * decay))


def rating_score(rating: float | None, prior: float = 4.3) -> float:
    r = prior if rating is None else rating
    return min(1.0, max(0.0, (r - 3.0) / 2.0))  # 3★ → 0, 5★ → 1


def trend_momentum(growth_7d: float, scale: float = 0.5) -> float:
    """M_trend from 7-day relative growth g (reviews or search volume): logistic squash of g/scale,
    centred so zero growth → 0.5."""
    return 1.0 / (1.0 + math.exp(-growth_7d / scale))


def normalise_epc(epc_krw: float, epc_ref_krw: float) -> float:
    """EPC on [0, 1]: half-saturation at the reference (category median) EPC."""
    if epc_krw <= 0:
        return 0.0
    return epc_krw / (epc_krw + epc_ref_krw)
