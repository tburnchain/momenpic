"""M3-local read helpers over ``repos.session`` + ``acvm.db.tables`` (joins the shared repositories lack)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select

from acvm.core.enums import LinkHealth, PublicationState
from acvm.db import tables as t
from acvm.db.repos import Repos
from acvm.domain.models import AffiliateLink, Product

# Publications whose link belongs on the account's hub page (queued or live; not failed/cancelled/taken down).
HUB_STATES: tuple[str, ...] = tuple(s.value for s in (
    PublicationState.QUEUED, PublicationState.UPLOADING, PublicationState.PROCESSING, PublicationState.PUBLISHED,
    PublicationState.MANUAL_PENDING,
))
UNHEALTHY: tuple[str, ...] = (LinkHealth.SOLD_OUT.value, LinkHealth.DISCONTINUED.value)


@dataclass(frozen=True, slots=True)
class HubEntry:
    link: AffiliateLink
    product: Product
    scheduled_at: datetime


async def hub_entries(repos: Repos, account_id: uuid.UUID) -> list[HubEntry]:
    """One entry per product for the account's queued/published links, newest publication first;
    links whose product is sold out or discontinued are left off the page."""
    q = (select(t.AffiliateLinkRow, t.ProductRow, t.PublicationRow.scheduled_at)
         .join(t.PublicationRow, t.PublicationRow.link_id == t.AffiliateLinkRow.id)
         .join(t.ProductRow, t.ProductRow.id == t.AffiliateLinkRow.product_id)
         .where(t.PublicationRow.channel_account_id == account_id,
                t.PublicationRow.state.in_(HUB_STATES),
                t.AffiliateLinkRow.health.notin_(UNHEALTHY))
         .order_by(t.PublicationRow.scheduled_at.desc(), t.AffiliateLinkRow.id))
    seen: set[int] = set()
    out: list[HubEntry] = []
    for link_row, product_row, scheduled_at in (await repos.session.execute(q)).all():
        if product_row.id in seen:
            continue
        seen.add(product_row.id)
        out.append(HubEntry(AffiliateLink.model_validate(link_row), Product.model_validate(product_row),
                            scheduled_at))
    return out


async def best_replacement(repos: Repos, category_id: str, exclude: Collection[int]) -> int | None:
    """§9.5 #4 대체 상품: same category, not regulated, no sold-out/discontinued link, highest latest POS."""
    latest = (select(t.ProductScoreRow.product_id, func.max(t.ProductScoreRow.scored_at).label("at"))
              .group_by(t.ProductScoreRow.product_id).subquery())
    unhealthy = select(t.AffiliateLinkRow.product_id).where(t.AffiliateLinkRow.health.in_(UNHEALTHY))
    q = (select(t.ProductRow.id)
         .join(latest, latest.c.product_id == t.ProductRow.id)
         .join(t.ProductScoreRow, and_(t.ProductScoreRow.product_id == latest.c.product_id,
                                       t.ProductScoreRow.scored_at == latest.c.at))
         .where(t.ProductRow.category_id == category_id,
                t.ProductRow.regulated.is_(False),
                t.ProductRow.id.notin_(unhealthy))
         .order_by(t.ProductScoreRow.pos.desc(), t.ProductRow.id))
    if exclude:
        q = q.where(t.ProductRow.id.notin_(list(exclude)))
    pid = (await repos.session.execute(q.limit(1))).scalar_one_or_none()
    return int(pid) if pid is not None else None


async def workspace_for_link(repos: Repos, link_id: uuid.UUID) -> uuid.UUID | None:
    q = (select(t.ChannelAccountRow.workspace_id)
         .join(t.PublicationRow, t.PublicationRow.channel_account_id == t.ChannelAccountRow.id)
         .where(t.PublicationRow.link_id == link_id))
    return (await repos.session.execute(q)).scalar_one_or_none()
