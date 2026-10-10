"""Evidence → raw POS signals: M_trend, EPC_bayes, P_risk and r̂_cat (§4 M1 feature table).

* **M_trend** "검색량·리뷰 증가 속도 (7일)": from ``product_snapshot`` history in the last 7 days. Three
  relative growth components, each needing ≥ 12 h between its first and last observation:

      g_reviews = (n_last − n_first) / n_first · (7 d / span)        clipped to [−1, 3]
      g_rank    = (rank_first − rank_last) / rank_first · (7 d / span)  (climbing the best list > 0), [−1, 1]
      g_price   = (p_first − p_last) / p_first                       (a price drop > 0), [−0.5, 0.5]
      g = Σ wᵢ gᵢ / Σ wᵢ over available components, w = (0.60, 0.25, 0.15)

  No usable component → ``None`` (POS imputes 0.5 and records it).
* **EPC_bayes** "과거 클릭당 수익, 카테고리 사전분포로 축소추정": clicks and commission of every subId ever
  issued for the product (M9 ``affiliate_report`` via ``PerfStore.report_totals``), shrunk with
  ``quant.bayes.shrink_epc`` towards E_prior = CVR·AOV·r with κ = 50 pseudo-clicks.
* **P_risk** "반품률 높은 카테고리, 규제 카테고리, 품절 이력": 1 − (1 − p_cat)·(1 − 0.35)^k, k = the
  product's links flagged sold-out/discontinued by the M3 health checker (capped at 3); regulated → 1.
* **r̂_cat**: observed commission/GMV over the category's subIds (last 90 days), shrunk to the category
  prior with 300 000 KRW of pseudo-GMV (≈ 10 orders): r̂ = (C + G₀·r₀) / (GMV + G₀).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from acvm.domain.models import Product
from acvm.modules.m1_product import queries
from acvm.modules.m1_product.pos import PosModel, RawSignals
from acvm.modules.m1_product.priors import prior_for
from acvm.quant.bayes import shrink_epc

if TYPE_CHECKING:
    from acvm.context import AppContext

TREND_HORIZON: Final = timedelta(days=7)
TREND_MIN_SPAN: Final = timedelta(hours=12)
TREND_WEIGHTS: Final = {"reviews": 0.60, "rank": 0.25, "price": 0.15}
EPC_KAPPA: Final = 50.0
RISK_PER_INCIDENT: Final = 0.35
RISK_MAX_INCIDENTS: Final = 3
RATE_PSEUDO_GMV_KRW: Final = 300_000.0
EVIDENCE_LOOKBACK: Final = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class SnapshotPoint:
    at: datetime
    price: int
    review_count: int | None
    rank: int | None


def _clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def trend_growth(points: Sequence[SnapshotPoint]) -> float | None:
    """Blended 7-day relative growth g (module docstring); ``None`` when history is insufficient."""
    pts = sorted(points, key=lambda p: p.at)
    parts: dict[str, float] = {}

    reviews = [p for p in pts if p.review_count is not None]
    if len(reviews) >= 2 and reviews[-1].at - reviews[0].at >= TREND_MIN_SPAN and (reviews[0].review_count or 0) > 0:
        first, last = float(reviews[0].review_count or 0), float(reviews[-1].review_count or 0)
        scale = TREND_HORIZON / (reviews[-1].at - reviews[0].at)
        parts["reviews"] = _clip((last - first) / first * scale, -1.0, 3.0)

    ranked = [p for p in pts if p.rank is not None and p.rank > 0]
    if len(ranked) >= 2 and ranked[-1].at - ranked[0].at >= TREND_MIN_SPAN:
        first_r, last_r = float(ranked[0].rank or 1), float(ranked[-1].rank or 1)
        scale = TREND_HORIZON / (ranked[-1].at - ranked[0].at)
        parts["rank"] = _clip((first_r - last_r) / first_r * scale, -1.0, 1.0)

    priced = [p for p in pts if p.price > 0]
    if len(priced) >= 2 and priced[-1].at - priced[0].at >= TREND_MIN_SPAN:
        parts["price"] = _clip((priced[0].price - priced[-1].price) / priced[0].price, -0.5, 0.5)

    if not parts:
        return None
    total_w = sum(TREND_WEIGHTS[k] for k in parts)
    return sum(TREND_WEIGHTS[k] * v for k, v in parts.items()) / total_w


def risk_score(return_risk: float, incidents: int, *, regulated: bool = False) -> float:
    """P_risk ∈ [0, 1] (module docstring)."""
    if regulated:
        return 1.0
    k = min(max(incidents, 0), RISK_MAX_INCIDENTS)
    return 1.0 - (1.0 - _clip(return_risk, 0.0, 1.0)) * (1.0 - RISK_PER_INCIDENT) ** k


def shrink_rate(commission_krw: float, gmv_krw: float, prior_rate: float,
                pseudo_gmv_krw: float = RATE_PSEUDO_GMV_KRW) -> float:
    """Posterior-mean commission rate r̂_cat with ``pseudo_gmv_krw`` of prior evidence."""
    return (max(commission_krw, 0.0) + pseudo_gmv_krw * prior_rate) / (max(gmv_krw, 0.0) + pseudo_gmv_krw)


class EvidenceCollector:
    """Gathers DB + analytics evidence for a batch of products and turns it into ``RawSignals``.

    DB reads happen in one short unit of work; the analytics store is queried afterwards (never inside
    a transaction), once for the whole batch.
    """

    def __init__(self, ctx: AppContext, model: PosModel) -> None:
        self.ctx = ctx
        self.model = model

    async def signals(self, products: Sequence[Product], v_demo: Mapping[int, float],
                      now: datetime) -> dict[int, RawSignals]:
        if not products:
            return {}
        pids = [p.id for p in products]
        categories = sorted({p.category_id for p in products})
        snapshots: dict[int, list[SnapshotPoint]] = {}
        async with self.ctx.uow() as repos:
            for p in products:
                rows = await repos.products.snapshots(p.id, now - TREND_HORIZON)
                snapshots[p.id] = [SnapshotPoint(r.at, r.price, r.review_count, r.rank) for r in rows]
            links = await queries.links_by_product(repos, pids)
            cat_subs = await queries.category_sub_ids(repos, categories)

        since: date = (now - EVIDENCE_LOOKBACK).date()
        product_subs = {sid for entries in links.values() for sid, _ in entries}
        all_subs = sorted(product_subs | {sid for subs in cat_subs.values() for sid in subs})
        totals = await self.ctx.perf.report_totals(all_subs, since=since) if all_subs else {}

        rates: dict[str, float] = {}
        for cat in categories:
            subs = cat_subs.get(cat, [])
            commission = sum(totals.get(s, {}).get("commission_krw", 0) for s in subs)
            gmv = sum(totals.get(s, {}).get("gmv_krw", 0) for s in subs)
            rates[cat] = shrink_rate(float(commission), float(gmv), prior_for(cat).commission_rate)

        out: dict[int, RawSignals] = {}
        for p in products:
            entries = links.get(p.id, [])
            clicks = sum(totals.get(sid, {}).get("clicks", 0) for sid, _ in entries)
            commission = sum(totals.get(sid, {}).get("commission_krw", 0) for sid, _ in entries)
            incidents = sum(1 for _, health in entries if health in queries.UNHEALTHY)
            out[p.id] = RawSignals(
                category_rate=rates[p.category_id],
                v_demo=v_demo.get(p.id, prior_for(p.category_id).demo_base),
                trend_growth=trend_growth(snapshots[p.id]),
                epc_bayes_krw=shrink_epc(float(clicks), float(commission), self.model.prior_epc_krw, EPC_KAPPA),
                p_risk=risk_score(prior_for(p.category_id).return_risk, incidents, regulated=p.regulated),
            )
        return out
