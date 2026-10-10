"""M1 Product Intelligence — decides the "무엇을" of "다섯 개 올려줘" (§4 M1).

``select`` pipeline (one command run = one idempotent activity, §5.2):

1. **Collect** candidates from the Partners API within the call budget: category best-sellers plus an
   optional keyword search (off-category hits dropped). If the budget or the 403 circuit blocks a source
   — or it fails — products of the category seen in the last 7 days are added from the DB instead
   (§4 M1 "키워드 큐 + 6시간 캐시 + 호출 예산 관리자", §9.5 #2 "캐시로 운영").
2. **Upsert** products (regulated flag from ``coupang_catalog.is_regulated``) with a price/rank snapshot.
3. **Hard-exclude** regulated goods (§4 M1 하드 배제), sold-out/discontinued listings, products the
   workspace published or queued in the last 14 days, and caller-supplied exclusions.
4. **Score** with POS ``pos-v1`` (``pos.py``) and persist every ``product_score``.
5. **De-duplicate** near-identical titles (MinHash ≥ 0.8 → keep the higher POS; re-listings of one item by
   several sellers would otherwise fill the slate with copies).
6. **Select** the top-n with POS ≥ ``min_pos``. Fewer qualifying products → fewer selections; the bar is
   never lowered silently (a warning is logged with the shortfall).
7. **Emit** one ``product.selected`` per pick (idempotency key ``{run_id}:{product_id}:selected``) in the
   same transaction as the activity record, so a replay returns the stored selection and emits nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.exc import IntegrityError

from acvm.core.errors import (
    CircuitOpen,
    NotFound,
    PolicyViolation,
    QuotaExhausted,
    UpstreamError,
    ValidationFailed,
)
from acvm.core.events import Event, ProductSelected
from acvm.core.ids import activity_key
from acvm.core.logging import get_logger
from acvm.db.repos import Repos
from acvm.domain.models import Product, ProductScore
from acvm.infra.coupang import CoupangClient, CoupangProduct, canonical_product_url
from acvm.infra.coupang_catalog import CATEGORY_IDS_BY_NAME, CATEGORY_NAMES, REGULATED_CATEGORIES, is_regulated
from acvm.infra.coupang_sandbox import coupang_client_for
from acvm.modules.m1_product import queries
from acvm.modules.m1_product.features import EvidenceCollector
from acvm.modules.m1_product.pos import PosModel
from acvm.modules.m1_product.priors import prior_for
from acvm.modules.m1_product.vdemo import ScoutVDemo
from acvm.quant.similarity import MinHasher

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

STAGE_SELECT: Final = "m1.select"
RECENT_PUBLISH_WINDOW: Final = timedelta(days=14)
DB_FALLBACK_WINDOW: Final = timedelta(days=7)
DEDUPE_THRESHOLD: Final = 0.8
SEARCH_LIMIT: Final = 10


@dataclass(frozen=True, slots=True)
class ScoredProduct:
    product: Product
    score: ProductScore


@dataclass(frozen=True, slots=True)
class _Candidate:
    product: Product
    rank: int | None  # best-seller rank (only from bestcategories; feeds M_trend rank velocity)
    unavailable: bool  # listing flagged sold out / discontinued


class ProductIntelligence:
    """M1 façade. ``client`` defaults to ``ctx.extras["coupang"]`` (built on first use)."""

    def __init__(self, ctx: AppContext, *, client: CoupangClient | None = None,
                 model: PosModel | None = None) -> None:
        self.ctx = ctx
        self._client = client
        self.model = model or PosModel.from_settings(ctx.settings)
        self.vdemo = ScoutVDemo(ctx)
        self.evidence = EvidenceCollector(ctx, self.model)
        self._hasher = MinHasher(num_perm=128, shingle=3, seed=11)

    @property
    def client(self) -> CoupangClient:
        return self._client if self._client is not None else coupang_client_for(self.ctx)

    # ───────────────────────── select ─────────────────────────

    async def select(self, *, run_id: uuid.UUID, workspace_id: uuid.UUID, category_id: str, n: int,
                     min_pos: float = 0.7, keyword: str | None = None,
                     exclude_product_ids: Collection[int] = ()) -> list[ProductSelected]:
        """Pick up to ``n`` products for one command run (see module docstring). Idempotent per run."""
        if n < 1:
            raise ValidationFailed("n must be ≥ 1", n=n)
        if not 0.0 <= min_pos <= 1.0:
            raise ValidationFailed("min_pos must be within [0, 1]", min_pos=min_pos)
        if category_id not in CATEGORY_NAMES:
            raise ValidationFailed("unknown Coupang category", category_id=category_id)
        kw = " ".join((keyword or "").split()) or None
        excluded = sorted({int(x) for x in exclude_product_ids})
        inputs = {"workspace_id": str(workspace_id), "category_id": category_id, "n": n, "min_pos": min_pos,
                  "keyword": kw, "exclude": excluded}
        akey = activity_key(run_id, f"cat{category_id}", STAGE_SELECT, inputs)
        async with self.ctx.uow() as repos:
            stored = await repos.activities.get(akey)
        if stored is not None:
            return self._replay(stored)
        if category_id in REGULATED_CATEGORIES:
            raise PolicyViolation("category is on the M1 hard-exclusion list (§4 M1 하드 배제)",
                                  category_id=category_id)

        now = self.ctx.clock.now()
        candidates, blocked = await self._collect(category_id, n, kw)
        async with self.ctx.uow() as repos:
            pool = await self._persist_and_filter(repos, candidates, blocked, category_id, workspace_id,
                                                  set(excluded), now)
        scored = await self._score_batch(pool, workspace_id=workspace_id, run_id=run_id, now=now)
        kept = self._dedupe(scored)
        chosen = [s for s in kept if s.score.pos >= min_pos][:n]
        if len(chosen) < n:
            log.warning("m1_selection_short", run_id=str(run_id), category_id=category_id, wanted=n,
                        got=len(chosen), min_pos=min_pos, candidates=len(pool), blocked_sources=blocked)
        selections = [ProductSelected(product_id=s.product.id, pos=s.score.pos, run_id=run_id, rank=i + 1)
                      for i, s in enumerate(chosen)]
        result: dict[str, Any] = {
            "selected": [sel.model_dump(mode="json") for sel in selections],
            "considered": len(pool), "scored": len(scored), "after_dedupe": len(kept),
            "blocked_sources": blocked, "model_version": self.model.model_version,
        }
        try:
            async with self.ctx.uow() as repos:
                again = await repos.activities.get(akey)
                if again is not None:
                    return self._replay(again)
                for s in scored:
                    await queries.add_score_once(repos, s.score)
                for sel in selections:
                    ev = Event.of(sel, workspace_id=workspace_id, run_id=run_id,
                                  idempotency_key=f"{run_id}:{sel.product_id}:selected")
                    ev.occurred_at = now
                    await self.ctx.emit(repos, ev)
                await repos.activities.put(akey, STAGE_SELECT, result, now)
        except IntegrityError:
            # a concurrent replay of the same run committed first — converge on its result
            async with self.ctx.uow() as repos:
                again = await repos.activities.get(akey)
            if again is None:
                raise
            return self._replay(again)
        log.info("m1_selected", run_id=str(run_id), category_id=category_id,
                 products=[sel.product_id for sel in selections])
        return selections

    @staticmethod
    def _replay(stored: dict[str, Any]) -> list[ProductSelected]:
        return [ProductSelected.model_validate(item) for item in stored.get("selected", [])]

    async def _collect(self, category_id: str, n: int, keyword: str | None) -> tuple[list[_Candidate], list[str]]:
        client = self.client
        blocked: list[str] = []
        out: list[_Candidate] = []
        try:
            best = await client.best_category(category_id, limit=min(100, max(20, 4 * n)))
            out.extend(_Candidate(self._to_product(item, category_id), item.rank, item.sold_out or item.discontinued)
                       for item in best)
        except (QuotaExhausted, CircuitOpen, UpstreamError) as exc:
            blocked.append("bestcategories")
            log.warning("m1_source_unavailable", source="bestcategories", error=exc.code)
        if keyword:
            try:
                found = await client.search(keyword, limit=SEARCH_LIMIT)
                for item in found:
                    item_cat = CATEGORY_IDS_BY_NAME.get(item.category_name)
                    if item_cat is not None and item_cat != category_id:
                        continue  # off-category hit for this command
                    out.append(_Candidate(self._to_product(item, category_id), None,
                                          item.sold_out or item.discontinued))
            except (QuotaExhausted, CircuitOpen, UpstreamError) as exc:
                blocked.append("search")
                log.warning("m1_source_unavailable", source="search", error=exc.code)
        return out, blocked

    @staticmethod
    def _to_product(item: CoupangProduct, category_id: str) -> Product:
        return Product(
            id=item.product_id, coupang_product_id=str(item.product_id), name=item.name, category_id=category_id,
            category_name=CATEGORY_NAMES.get(category_id, item.category_name), price=item.price,
            rating=item.rating, review_count=item.review_count, is_rocket=item.is_rocket,
            is_free_shipping=item.is_free_shipping, image_url=item.image_url,
            product_url=canonical_product_url(item.product_id),  # untagged; tracking links are per publication
            regulated=is_regulated(item.name, category_id), last_seen_at=item.fetched_at,
            price_checked_at=item.fetched_at)

    async def _persist_and_filter(self, repos: Repos, candidates: Sequence[_Candidate], blocked: list[str],
                                  category_id: str, workspace_id: uuid.UUID, exclude: set[int],
                                  now: datetime) -> list[Product]:
        pool: dict[int, Product] = {}
        unavailable: set[int] = set()
        for c in candidates:
            stored = await queries.upsert_product(repos, c.product, rank=c.rank)
            if c.unavailable:
                unavailable.add(stored.id)
            pool.setdefault(stored.id, stored)
        if blocked or not candidates:
            recent = await repos.products.by_category(category_id, since=now - DB_FALLBACK_WINDOW)
            added = [p for p in recent if p.id not in pool]
            for p in added:
                pool[p.id] = p
            log.info("m1_db_fallback", category_id=category_id, added=len(added), blocked_sources=blocked)
        exposed = await queries.exposed_product_ids(repos, workspace_id, now - RECENT_PUBLISH_WINDOW)
        regulated = [pid for pid, p in pool.items() if p.regulated]
        if regulated:
            log.info("m1_regulated_excluded", product_ids=regulated)
        return [p for pid, p in sorted(pool.items())
                if not p.regulated and pid not in unavailable and pid not in exposed and pid not in exclude]

    def _dedupe(self, scored: Sequence[ScoredProduct]) -> list[ScoredProduct]:
        kept: list[ScoredProduct] = []
        signatures: list[tuple[int, ...]] = []
        for s in sorted(scored, key=lambda x: (-x.score.pos, x.product.id)):
            sig = self._hasher.signature(s.product.name)
            dup_of = next((kept[i].product.id for i, other in enumerate(signatures)
                           if MinHasher.estimate(sig, other) >= DEDUPE_THRESHOLD), None)
            if dup_of is not None:
                log.info("m1_near_duplicate_dropped", product_id=s.product.id, kept_product_id=dup_of)
                continue
            kept.append(s)
            signatures.append(sig)
        return kept

    # ───────────────────────── scoring ─────────────────────────

    async def _score_batch(self, products: Sequence[Product], *, workspace_id: uuid.UUID | None,
                           run_id: uuid.UUID | None, now: datetime) -> list[ScoredProduct]:
        if not products:
            return []
        vdemo = await self.vdemo.score_many(products, workspace_id=workspace_id, run_id=run_id)
        signals = await self.evidence.signals(products, {pid: r.score for pid, r in vdemo.items()}, now)
        out: list[ScoredProduct] = []
        for p in products:
            features = self.model.features(p, signals[p.id], prior_for(p.category_id))
            out.append(ScoredProduct(p, ProductScore(product_id=p.id, scored_at=now, pos=self.model.pos(features),
                                                     features=features, model_version=self.model.model_version)))
        return out

    async def score(self, product: Product, *, workspace_id: uuid.UUID | None = None,
                    run_id: uuid.UUID | None = None) -> ProductScore:
        """Score one product with the current POS model and persist the ``product_score`` row."""
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            if await repos.products.get(product.id) is None:
                await queries.upsert_product(repos, product)
        scored = (await self._score_batch([product], workspace_id=workspace_id, run_id=run_id, now=now))[0]
        async with self.ctx.uow() as repos:
            await queries.add_score_once(repos, scored.score)
        return scored.score

    # ───────────────────────── C-03 price freshness ─────────────────────────

    async def refresh_price(self, product_id: int) -> tuple[int, datetime, bool]:
        """(price, checked_at, fresh) for gate rule C-03 (§8.1 "게시 직전 가격 재조회").

        Spends one search call only when the stored price is older than ``gate.price_fresh_minutes``;
        budget exhaustion, an open circuit or an API error never raise — the stored price comes back
        with ``fresh=False`` and the gate decides (FIX / remove the price phrase).
        """
        now = self.ctx.clock.now()
        window = timedelta(minutes=self.ctx.settings.gate.price_fresh_minutes)
        async with self.ctx.uow() as repos:
            product = await repos.products.get(product_id)
        if product is None:
            raise NotFound("product not found", product_id=product_id)
        checked_at = product.price_checked_at or product.last_seen_at
        if now - checked_at < window:
            return product.price, checked_at, True
        try:
            results = await self.client.search(_search_keyword(product.name), limit=SEARCH_LIMIT)
        except (QuotaExhausted, CircuitOpen, UpstreamError, ValidationFailed) as exc:
            log.info("m1_price_refresh_deferred", product_id=product_id, reason=exc.code)
            return product.price, checked_at, False
        match = next((r for r in results if r.product_id == product.id), None)
        if match is None or match.fetched_at <= checked_at or match.price <= 0:
            log.info("m1_price_refresh_no_newer_data", product_id=product_id, listed=match is not None)
            return product.price, checked_at, False
        async with self.ctx.uow() as repos:
            await queries.record_price(repos, product.id, match.price, match.fetched_at)
        return match.price, match.fetched_at, now - match.fetched_at < window


def _search_keyword(name: str) -> str:
    """Product title as a search keyword: brackets dropped, bounded length."""
    cleaned = " ".join(name.replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ").split())
    return cleaned[:50]
