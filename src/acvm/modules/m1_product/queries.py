"""M1-local persistence helpers (``repos.session`` + ``acvm.db.tables``).

They cover gaps in the shared repositories without editing them (requested upstream in the M1 report):

* snapshot-safe product upsert — ``ProductRepo.upsert`` / ``mark_price_checked`` insert a
  ``product_snapshot`` row keyed (product_id, at) unconditionally, which collides when the same
  observation is recorded twice (cache hit, replay under a frozen clock); they also do not record the
  best-seller ``rank`` that M_trend uses, and an API refresh would wipe operator-entered ratings;
* idempotent ``product_score`` insert (same (product_id, scored_at) twice);
* the workspace's recently published products (14-day exclusion) and category subIds (r̂_cat).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import select

from acvm.core.enums import LinkHealth, PublicationState
from acvm.db import tables as t
from acvm.db.repos import Repos
from acvm.domain.models import Product, ProductScore

# Publication states that mean "this product is (being) shown by the workspace".
EXPOSED_STATES: tuple[str, ...] = tuple(s.value for s in (
    PublicationState.QUEUED, PublicationState.UPLOADING, PublicationState.PROCESSING, PublicationState.PUBLISHED,
    PublicationState.MANUAL_PENDING, PublicationState.TAKEN_DOWN,
))
UNHEALTHY: tuple[str, ...] = (LinkHealth.SOLD_OUT.value, LinkHealth.DISCONTINUED.value)


async def upsert_product(repos: Repos, product: Product, *, rank: int | None = None) -> Product:
    """Insert/refresh a product and its (product_id, last_seen_at) snapshot exactly once.

    Operator-verified fields survive an API refresh: a ``None`` rating/review count from the Partners
    API never overwrites a stored value, and empty ``verified_facts`` never wipe stored ones.
    """
    existing = await repos.products.get(product.id)
    if existing is not None and existing.last_seen_at > product.last_seen_at:
        # An older observation (e.g. a stale cache served during an open circuit) never moves state back.
        snap = await repos.session.get(t.ProductSnapshotRow, (product.id, product.last_seen_at))
        if snap is not None and rank is not None and snap.rank is None:
            snap.rank = rank
            await repos.session.flush()
        return existing
    if existing is not None:
        keep: dict[str, object] = {}
        if product.rating is None and existing.rating is not None:
            keep["rating"] = existing.rating
        if product.review_count is None and existing.review_count is not None:
            keep["review_count"] = existing.review_count
        if not product.verified_facts and existing.verified_facts:
            keep["verified_facts"] = existing.verified_facts
        if existing.price_checked_at and (product.price_checked_at is None
                                          or existing.price_checked_at > product.price_checked_at):
            keep["price_checked_at"] = existing.price_checked_at
            keep["price"] = existing.price
        if keep:
            product = product.model_copy(update=keep)
    snap = await repos.session.get(t.ProductSnapshotRow, (product.id, product.last_seen_at))
    if snap is None:
        await repos.products.upsert(product)
        snap = await repos.session.get(t.ProductSnapshotRow, (product.id, product.last_seen_at))
    else:
        row = await repos.session.get(t.ProductRow, product.id)
        if row is not None:
            for key, value in product.model_dump(mode="python").items():
                setattr(row, key, value)
        snap.price, snap.review_count, snap.rating = product.price, product.review_count, product.rating
    if snap is not None and rank is not None:
        snap.rank = rank
    await repos.session.flush()
    return product


async def record_price(repos: Repos, product_id: int, price: int, at: datetime) -> None:
    """``mark_price_checked`` that tolerates an existing snapshot at the same instant."""
    snap = await repos.session.get(t.ProductSnapshotRow, (product_id, at))
    if snap is None:
        await repos.products.mark_price_checked(product_id, price, at)
        return
    row = await repos.session.get(t.ProductRow, product_id)
    if row is not None:
        row.price, row.price_checked_at = price, at
    snap.price = price
    await repos.session.flush()


async def add_score_once(repos: Repos, score: ProductScore) -> None:
    if await repos.session.get(t.ProductScoreRow, (score.product_id, score.scored_at)) is None:
        await repos.products.add_score(score)


async def exposed_product_ids(repos: Repos, workspace_id: uuid.UUID, since: datetime) -> set[int]:
    """Products the workspace published (or has queued) since ``since`` — §4 M1 14-day exclusion."""
    q = (select(t.ContentRow.product_id)
         .join(t.PublicationRow, t.PublicationRow.content_id == t.ContentRow.id)
         .where(t.ContentRow.workspace_id == workspace_id,
                t.PublicationRow.state.in_(EXPOSED_STATES),
                (t.PublicationRow.published_at >= since) | (t.PublicationRow.scheduled_at >= since))
         .distinct())
    return {int(pid) for pid in (await repos.session.execute(q)).scalars().all()}


async def links_by_product(repos: Repos, product_ids: Collection[int]) -> dict[int, list[tuple[str, str]]]:
    """product_id → [(sub_id, health)] for every affiliate link ever created for it."""
    if not product_ids:
        return {}
    q = (select(t.AffiliateLinkRow.product_id, t.AffiliateLinkRow.sub_id, t.AffiliateLinkRow.health)
         .where(t.AffiliateLinkRow.product_id.in_(list(product_ids))))
    out: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for pid, sub_id, health in (await repos.session.execute(q)).all():
        out[int(pid)].append((str(sub_id), str(health)))
    return dict(out)


async def category_sub_ids(repos: Repos, category_ids: Sequence[str]) -> dict[str, list[str]]:
    """category_id → subIds of every link for a product in that category (r̂_cat evidence)."""
    if not category_ids:
        return {}
    q = (select(t.ProductRow.category_id, t.AffiliateLinkRow.sub_id)
         .join(t.AffiliateLinkRow, t.AffiliateLinkRow.product_id == t.ProductRow.id)
         .where(t.ProductRow.category_id.in_(list(category_ids))))
    out: dict[str, list[str]] = defaultdict(list)
    for cat, sub_id in (await repos.session.execute(q)).all():
        out[str(cat)].append(str(sub_id))
    return dict(out)
