"""Repositories: the only code that translates between domain models and rows.

Each repository is bound to one ``AsyncSession`` (one unit of work); callers obtain them through
``Repos(session)``. Writes that must be idempotent use natural unique keys (publication
idempotency key, link publication key, activity key, cost-ledger key) and return the existing row
on conflict instead of raising — retried activities therefore converge instead of duplicating.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from acvm.core.enums import AccountStatus, PublicationState
from acvm.core.events import Event
from acvm.core.ids import uuid7
from acvm.db import tables as t
from acvm.domain.models import (
    AffiliateLink,
    ArmPosteriorState,
    Asset,
    AuditEntry,
    ChannelAccount,
    CommandRun,
    ComplianceCheck,
    Content,
    ContentDNA,
    PlaybookRule,
    Product,
    ProductScore,
    Publication,
    Workspace,
)
from acvm.policy.lexicon import is_valid_disclosure
from acvm.quant.similarity import cosine

M = TypeVar("M", bound=BaseModel)


def _dump(model: BaseModel, **overrides: Any) -> dict[str, Any]:
    data = model.model_dump(mode="python")
    data.update(overrides)
    return data


def _json(model: BaseModel | None) -> dict[str, Any] | None:
    return None if model is None else model.model_dump(mode="json")


def similarity_text(c: Content) -> str:
    """Canonical text for self-similarity (S-01) and diversity checks: hook + narration + body + caption
    with disclosure lines removed — the mandatory disclosure is identical everywhere and would otherwise
    dominate every comparison."""
    parts = [c.script.hook, c.script.narration, c.script.body, c.script.caption]
    lines = [ln for p in parts for ln in p.split("\n") if ln.strip() and not is_valid_disclosure(ln)]
    return " ".join(lines).strip()


class WorkspaceRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, ws: Workspace) -> Workspace:
        self.s.add(t.WorkspaceRow(**_dump(ws)))
        await self.s.flush()
        return ws

    async def get(self, ws_id: uuid.UUID) -> Workspace | None:
        row = await self.s.get(t.WorkspaceRow, ws_id)
        return Workspace.model_validate(row) if row else None

    async def first(self) -> Workspace | None:
        row = (await self.s.execute(select(t.WorkspaceRow).limit(1))).scalar_one_or_none()
        return Workspace.model_validate(row) if row else None


class AccountRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, acc: ChannelAccount) -> ChannelAccount:
        self.s.add(t.ChannelAccountRow(**_dump(acc, platform=acc.platform.value, status=acc.status.value)))
        await self.s.flush()
        return acc

    async def get(self, acc_id: uuid.UUID) -> ChannelAccount | None:
        row = await self.s.get(t.ChannelAccountRow, acc_id)
        return ChannelAccount.model_validate(row) if row else None

    async def list(self, workspace_id: uuid.UUID, *, platform: str | None = None,
                   niche: str | None = None) -> list[ChannelAccount]:
        q = select(t.ChannelAccountRow).where(t.ChannelAccountRow.workspace_id == workspace_id)
        if platform:
            q = q.where(t.ChannelAccountRow.platform == platform)
        if niche:
            q = q.where(t.ChannelAccountRow.niche == niche)
        rows = (await self.s.execute(q.order_by(t.ChannelAccountRow.created_at))).scalars().all()
        return [ChannelAccount.model_validate(r) for r in rows]

    async def set_status(self, acc_id: uuid.UUID, status: AccountStatus,
                         until: datetime | None = None) -> None:
        await self.s.execute(update(t.ChannelAccountRow).where(t.ChannelAccountRow.id == acc_id)
                             .values(status=status.value, cooldown_until=until))

    async def record_health(self, acc_id: uuid.UUID, kind: str, at: datetime, value: float = 1.0,
                            detail: dict[str, Any] | None = None) -> None:
        self.s.add(t.AccountHealthEventRow(account_id=acc_id, kind=kind, value=value, detail=detail or {},
                                           at=at))
        await self.s.flush()

    async def health_events(self, acc_id: uuid.UUID, since: datetime) -> builtins.list[t.AccountHealthEventRow]:
        q = (select(t.AccountHealthEventRow)
             .where(t.AccountHealthEventRow.account_id == acc_id, t.AccountHealthEventRow.at >= since)
             .order_by(t.AccountHealthEventRow.at))
        return list((await self.s.execute(q)).scalars().all())


class ProductRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    # fields an API refresh may legitimately lack (search API has no rating/reviews) — never overwrite with None
    _KEEP_IF_NONE = frozenset({"rating", "review_count", "price_checked_at", "category_name", "image_url"})

    async def upsert(self, p: Product, *, rank: int | None = None) -> Product:
        """Idempotent: re-upserting the same observation (same ``last_seen_at``) rewrites one snapshot."""
        row = await self.s.get(t.ProductRow, p.id)
        data = _dump(p)
        if row is None:
            self.s.add(t.ProductRow(**data))
        else:
            for k, v in data.items():
                if k == "verified_facts" and not v:
                    continue  # never wipe operator-verified facts with an empty API refresh
                if k in self._KEEP_IF_NONE and v in (None, ""):
                    continue
                setattr(row, k, v)
        await self._snapshot(p.id, p.last_seen_at, price=p.price, review_count=p.review_count, rating=p.rating,
                             rank=rank)
        await self.s.flush()
        return Product.model_validate(row) if row is not None else p

    async def _snapshot(self, product_id: int, at: datetime, *, price: int, review_count: int | None = None,
                        rating: float | None = None, rank: int | None = None) -> None:
        snap = await self.s.get(t.ProductSnapshotRow, (product_id, at))
        if snap is None:
            self.s.add(t.ProductSnapshotRow(product_id=product_id, at=at, price=price, review_count=review_count,
                                            rating=rating, rank=rank))
            return
        snap.price = price
        snap.review_count = review_count if review_count is not None else snap.review_count
        snap.rating = rating if rating is not None else snap.rating
        snap.rank = rank if rank is not None else snap.rank

    async def get(self, product_id: int) -> Product | None:
        row = await self.s.get(t.ProductRow, product_id)
        return Product.model_validate(row) if row else None

    async def by_category(self, category_id: str, since: datetime | None = None) -> list[Product]:
        q = select(t.ProductRow).where(t.ProductRow.category_id == category_id)
        if since:
            q = q.where(t.ProductRow.last_seen_at >= since)
        return [Product.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def snapshots(self, product_id: int, since: datetime) -> list[t.ProductSnapshotRow]:
        q = (select(t.ProductSnapshotRow)
             .where(t.ProductSnapshotRow.product_id == product_id, t.ProductSnapshotRow.at >= since)
             .order_by(t.ProductSnapshotRow.at))
        return list((await self.s.execute(q)).scalars().all())

    async def add_score(self, score: ProductScore) -> None:
        """Idempotent on (product_id, scored_at): a retried scoring activity overwrites, never duplicates."""
        row = await self.s.get(t.ProductScoreRow, (score.product_id, score.scored_at))
        features = score.features.model_dump(mode="json")
        if row is None:
            self.s.add(t.ProductScoreRow(product_id=score.product_id, scored_at=score.scored_at, pos=score.pos,
                                         features=features, model_version=score.model_version))
        else:
            row.pos, row.features, row.model_version = score.pos, features, score.model_version
        await self.s.flush()

    async def latest_score(self, product_id: int) -> ProductScore | None:
        q = (select(t.ProductScoreRow).where(t.ProductScoreRow.product_id == product_id)
             .order_by(t.ProductScoreRow.scored_at.desc()).limit(1))
        row = (await self.s.execute(q)).scalar_one_or_none()
        return ProductScore.model_validate(row) if row else None

    async def mark_price_checked(self, product_id: int, price: int, at: datetime) -> None:
        await self.s.execute(update(t.ProductRow).where(t.ProductRow.id == product_id)
                             .values(price=price, price_checked_at=at))
        await self._snapshot(product_id, at, price=price)
        await self.s.flush()


class DNARepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, dna: ContentDNA) -> ContentDNA:
        self.s.add(t.ContentDNARow(**_dump(dna, hook_type=dna.hook_type.value)))
        await self.s.flush()
        return dna

    async def get(self, dna_id: uuid.UUID) -> ContentDNA | None:
        row = await self.s.get(t.ContentDNARow, dna_id)
        return ContentDNA.model_validate(row) if row else None

    async def top(self, workspace_id: uuid.UUID | None, limit: int = 20,
                  hook_type: str | None = None) -> list[ContentDNA]:
        q = select(t.ContentDNARow)
        if workspace_id is not None:
            q = q.where((t.ContentDNARow.workspace_id == workspace_id) | (t.ContentDNARow.workspace_id.is_(None)))
        if hook_type:
            q = q.where(t.ContentDNARow.hook_type == hook_type)
        q = q.order_by(t.ContentDNARow.perf_percentile.desc()).limit(limit)
        return [ContentDNA.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def references_with_text(self, workspace_id: uuid.UUID | None, limit: int = 500) -> list[ContentDNA]:
        q = select(t.ContentDNARow).where(t.ContentDNARow.reference_text != "")
        if workspace_id is not None:
            q = q.where((t.ContentDNARow.workspace_id == workspace_id) | (t.ContentDNARow.workspace_id.is_(None)))
        return [ContentDNA.model_validate(r) for r in (await self.s.execute(q.limit(limit))).scalars().all()]

    async def nearest(self, embedding: Sequence[float], k: int = 5) -> list[tuple[ContentDNA, float]]:
        """Top-k by cosine similarity: pgvector ``<=>`` on PostgreSQL, exact scan elsewhere."""
        if self.s.get_bind().dialect.name == "postgresql":
            dist = t.ContentDNARow.embedding.cosine_distance(list(embedding))
            q = (select(t.ContentDNARow, dist).where(t.ContentDNARow.embedding.is_not(None))
                 .order_by(dist).limit(k))
            return [(ContentDNA.model_validate(r), 1.0 - float(d)) for r, d in (await self.s.execute(q)).all()]
        rows = (await self.s.execute(select(t.ContentDNARow).where(t.ContentDNARow.embedding.is_not(None)))).scalars()
        scored = [(ContentDNA.model_validate(r), cosine(embedding, r.embedding or [])) for r in rows]
        return sorted(scored, key=lambda x: -x[1])[:k]


class ContentRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    def _row_data(self, c: Content) -> dict[str, Any]:
        return {
            "id": c.id, "workspace_id": c.workspace_id, "product_id": c.product_id, "dna_id": c.dna_id,
            "kind": c.kind.value, "persona": c.persona, "hook_variant": c.hook_variant,
            "template_id": c.template_id, "script": c.script.model_dump(mode="json"),
            "claims": [cl.model_dump(mode="json") for cl in c.claims], "status": c.status.value,
            "similarity_max": c.similarity_max, "created_by_run": c.created_by_run,
            "target_account_id": c.target_account_id, "render": _json(c.render),
            "parent_content_id": c.parent_content_id, "created_at": c.created_at, "render_options": c.render_options,
            "text_for_similarity": similarity_text(c),
        }

    async def add(self, c: Content, embedding: Sequence[float] | None = None) -> Content:
        existing = await self.s.get(t.ContentRow, c.id)
        if existing is not None:
            return self._to_domain(existing)
        self.s.add(t.ContentRow(**self._row_data(c), embedding=list(embedding) if embedding else None))
        await self.s.flush()
        return c

    async def save(self, c: Content, embedding: Sequence[float] | None = None) -> Content:
        row = await self.s.get(t.ContentRow, c.id)
        if row is None:
            return await self.add(c, embedding)
        for k, v in self._row_data(c).items():
            setattr(row, k, v)
        if embedding is not None:
            row.embedding = list(embedding)
        await self.s.flush()
        return c

    @staticmethod
    def _to_domain(row: t.ContentRow) -> Content:
        return Content.model_validate({
            "id": row.id, "workspace_id": row.workspace_id, "product_id": row.product_id,
            "dna_id": row.dna_id, "kind": row.kind, "persona": row.persona,
            "hook_variant": row.hook_variant, "template_id": row.template_id, "script": row.script,
            "claims": row.claims, "status": row.status, "similarity_max": row.similarity_max,
            "created_by_run": row.created_by_run, "target_account_id": row.target_account_id,
            "render": row.render, "parent_content_id": row.parent_content_id, "created_at": row.created_at,
            "render_options": row.render_options or {},
        })

    async def get(self, content_id: uuid.UUID) -> Content | None:
        row = await self.s.get(t.ContentRow, content_id)
        return self._to_domain(row) if row else None

    async def by_run(self, run_id: uuid.UUID) -> list[Content]:
        q = select(t.ContentRow).where(t.ContentRow.created_by_run == run_id).order_by(t.ContentRow.created_at)
        return [self._to_domain(r) for r in (await self.s.execute(q)).scalars().all()]

    async def recent_texts(self, *, account_id: uuid.UUID | None, workspace_id: uuid.UUID, since: datetime,
                           exclude: uuid.UUID | None = None) -> list[tuple[uuid.UUID, str]]:
        """Texts of the account's recent content for S-01 / M4 diversity (30-day lookback)."""
        q = select(t.ContentRow.id, t.ContentRow.text_for_similarity).where(
            t.ContentRow.workspace_id == workspace_id, t.ContentRow.created_at >= since)
        if account_id is not None:
            q = q.where(t.ContentRow.target_account_id == account_id)
        if exclude is not None:
            q = q.where(t.ContentRow.id != exclude)
        return [(r[0], r[1]) for r in (await self.s.execute(q)).all()]

    async def count_by_template(self, template_id: str, workspace_id: uuid.UUID) -> int:
        q = select(func.count()).select_from(t.ContentRow).where(
            t.ContentRow.template_id == template_id, t.ContentRow.workspace_id == workspace_id,
            t.ContentRow.status.in_(("approved",)))
        return int((await self.s.execute(q)).scalar_one())

    async def link_asset(self, content_id: uuid.UUID, asset_id: uuid.UUID, role: str) -> None:
        exists = await self.s.get(t.ContentAssetRow, (content_id, asset_id, role))
        if exists is None:
            self.s.add(t.ContentAssetRow(content_id=content_id, asset_id=asset_id, role=role))
            await self.s.flush()

    async def asset_ids(self, content_id: uuid.UUID) -> list[uuid.UUID]:
        q = select(t.ContentAssetRow.asset_id).where(t.ContentAssetRow.content_id == content_id)
        return list((await self.s.execute(q)).scalars().all())


class AssetRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def register(self, a: Asset) -> Asset:
        q = select(t.AssetRow).where(t.AssetRow.sha256 == a.sha256,
                                     t.AssetRow.rights_source == a.rights_source.value)
        existing = (await self.s.execute(q)).scalar_one_or_none()
        if existing is not None:
            return Asset.model_validate(existing)
        try:
            async with self.s.begin_nested():  # concurrent producers may register the same media (e.g. a voice)
                self.s.add(t.AssetRow(**_dump(a, rights_source=a.rights_source.value)))
                await self.s.flush()
        except IntegrityError:
            existing = (await self.s.execute(q)).scalar_one_or_none()
            if existing is None:
                raise
            return Asset.model_validate(existing)
        return a

    async def get(self, asset_id: uuid.UUID) -> Asset | None:
        row = await self.s.get(t.AssetRow, asset_id)
        return Asset.model_validate(row) if row else None

    async def get_many(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Asset]:
        if not ids:
            return {}
        rows = (await self.s.execute(select(t.AssetRow).where(t.AssetRow.id.in_(list(ids))))).scalars()
        return {r.id: Asset.model_validate(r) for r in rows}

    async def for_product(self, product_id: int) -> list[Asset]:
        q = select(t.AssetRow).where(t.AssetRow.product_id == product_id)
        return [Asset.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def by_tag(self, tag: str, kind: str | None = None) -> list[Asset]:
        rows = (await self.s.execute(select(t.AssetRow))).scalars().all()
        out = [Asset.model_validate(r) for r in rows if tag in (r.tags or [])]
        return [a for a in out if kind is None or a.kind == kind]


class ComplianceRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, chk: ComplianceCheck) -> ComplianceCheck:
        self.s.add(t.ComplianceCheckRow(
            id=chk.id, content_id=chk.content_id, rule_set_version=chk.rule_set_version,
            verdict=chk.verdict.value, findings=[f.model_dump(mode="json") for f in chk.findings],
            iteration=chk.iteration, stage=chk.stage, reviewer=chk.reviewer, decided_at=chk.decided_at))
        await self.s.flush()
        return chk

    async def latest(self, content_id: uuid.UUID, stage: str | None = None) -> ComplianceCheck | None:
        q = select(t.ComplianceCheckRow).where(t.ComplianceCheckRow.content_id == content_id)
        if stage:
            q = q.where(t.ComplianceCheckRow.stage == stage)
        row = (await self.s.execute(q.order_by(t.ComplianceCheckRow.decided_at.desc()).limit(1))).scalar_one_or_none()
        return ComplianceCheck.model_validate(row) if row else None

    async def enqueue_review(self, workspace_id: uuid.UUID, content_id: uuid.UUID, check_id: uuid.UUID,
                             reasons: list[str], at: datetime, run_id: uuid.UUID | None = None) -> uuid.UUID:
        existing = (await self.s.execute(select(t.ReviewItemRow)
                                         .where(t.ReviewItemRow.content_id == content_id))).scalar_one_or_none()
        if existing is not None:
            existing.check_id, existing.reasons, existing.status = check_id, reasons, "pending"
            await self.s.flush()
            return existing.id
        rid = uuid7()
        self.s.add(t.ReviewItemRow(id=rid, workspace_id=workspace_id, content_id=content_id, check_id=check_id,
                                   run_id=run_id, reasons=reasons, status="pending", created_at=at))
        await self.s.flush()
        return rid

    async def pending_reviews(self, workspace_id: uuid.UUID) -> list[t.ReviewItemRow]:
        q = (select(t.ReviewItemRow).where(t.ReviewItemRow.workspace_id == workspace_id,
                                           t.ReviewItemRow.status == "pending")
             .order_by(t.ReviewItemRow.created_at))
        return list((await self.s.execute(q)).scalars().all())

    async def decide_review(self, item_id: uuid.UUID, approve: bool, by: str, at: datetime,
                            note: str = "") -> t.ReviewItemRow | None:
        row = await self.s.get(t.ReviewItemRow, item_id)
        if row is None or row.status != "pending":
            return row
        row.status = "approved" if approve else "rejected"
        row.decided_by, row.decided_at, row.note = by, at, note
        await self.s.flush()
        return row

    async def review_for_content(self, content_id: uuid.UUID) -> t.ReviewItemRow | None:
        q = select(t.ReviewItemRow).where(t.ReviewItemRow.content_id == content_id)
        return (await self.s.execute(q)).scalar_one_or_none()

    async def verdict_counts(self, since: datetime) -> dict[str, int]:
        q = (select(t.ComplianceCheckRow.verdict, func.count())
             .where(t.ComplianceCheckRow.decided_at >= since).group_by(t.ComplianceCheckRow.verdict))
        return {v: int(n) for v, n in (await self.s.execute(q)).all()}


class LinkRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def get_by_publication_key(self, key: str) -> AffiliateLink | None:
        q = select(t.AffiliateLinkRow).where(t.AffiliateLinkRow.publication_key == key)
        row = (await self.s.execute(q)).scalar_one_or_none()
        return AffiliateLink.model_validate(row) if row else None

    async def add(self, link: AffiliateLink) -> AffiliateLink:
        existing = await self.get_by_publication_key(link.publication_key)
        if existing is not None:
            return existing
        self.s.add(t.AffiliateLinkRow(**_dump(link, health=link.health.value)))
        await self.s.flush()
        return link

    async def get(self, link_id: uuid.UUID) -> AffiliateLink | None:
        row = await self.s.get(t.AffiliateLinkRow, link_id)
        return AffiliateLink.model_validate(row) if row else None

    async def by_sub_id(self, sub_id: str) -> AffiliateLink | None:
        row = (await self.s.execute(select(t.AffiliateLinkRow)
                                    .where(t.AffiliateLinkRow.sub_id == sub_id))).scalar_one_or_none()
        return AffiliateLink.model_validate(row) if row else None

    async def by_hub_code(self, code: str) -> AffiliateLink | None:
        row = (await self.s.execute(select(t.AffiliateLinkRow)
                                    .where(t.AffiliateLinkRow.hub_code == code))).scalar_one_or_none()
        return AffiliateLink.model_validate(row) if row else None

    async def set_health(self, link_id: uuid.UUID, health: str, at: datetime) -> None:
        await self.s.execute(update(t.AffiliateLinkRow).where(t.AffiliateLinkRow.id == link_id)
                             .values(health=health, checked_at=at))

    async def due_for_check(self, older_than: datetime, limit: int = 500) -> list[AffiliateLink]:
        q = (select(t.AffiliateLinkRow)
             .where((t.AffiliateLinkRow.checked_at.is_(None)) | (t.AffiliateLinkRow.checked_at < older_than))
             .limit(limit))
        return [AffiliateLink.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def for_product(self, product_id: int) -> list[AffiliateLink]:
        q = select(t.AffiliateLinkRow).where(t.AffiliateLinkRow.product_id == product_id)
        return [AffiliateLink.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]


class PublicationRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def get_by_key(self, key: str) -> Publication | None:
        row = (await self.s.execute(select(t.PublicationRow)
                                    .where(t.PublicationRow.idempotency_key == key))).scalar_one_or_none()
        return Publication.model_validate(row) if row else None

    async def add(self, pub: Publication) -> Publication:
        existing = await self.get_by_key(pub.idempotency_key)
        if existing is not None:
            return existing
        self.s.add(t.PublicationRow(**_dump(pub, platform=pub.platform.value, state=pub.state.value)))
        await self.s.flush()
        return pub

    async def get(self, pub_id: uuid.UUID) -> Publication | None:
        row = await self.s.get(t.PublicationRow, pub_id)
        return Publication.model_validate(row) if row else None

    async def transition(self, pub_id: uuid.UUID, to: PublicationState, at: datetime,
                         **fields: Any) -> Publication:
        row = await self.s.get(t.PublicationRow, pub_id)
        if row is None:
            raise KeyError(pub_id)
        frm = row.state
        row.state = to.value
        row.updated_at = at
        for k, v in fields.items():
            setattr(row, k, v)
        if frm != to.value:
            self.s.add(t.PublicationTransitionRow(publication_id=pub_id, from_state=frm, to_state=to.value,
                                                  detail={k: str(v) for k, v in fields.items() if k != "error"},
                                                  at=at))
        await self.s.flush()
        return Publication.model_validate(row)

    async def booked_times(self, account_id: uuid.UUID, start: datetime, end: datetime) -> list[datetime]:
        q = select(t.PublicationRow.scheduled_at).where(
            t.PublicationRow.channel_account_id == account_id,
            t.PublicationRow.scheduled_at >= start, t.PublicationRow.scheduled_at < end,
            t.PublicationRow.state.notin_(("FAILED", "CANCELLED", "EXPIRED")))
        return list((await self.s.execute(q)).scalars().all())

    async def due(self, now: datetime, limit: int = 100) -> list[Publication]:
        q = (select(t.PublicationRow)
             .where(t.PublicationRow.state == "QUEUED", t.PublicationRow.scheduled_at <= now)
             .order_by(t.PublicationRow.scheduled_at).limit(limit))
        return [Publication.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def by_content(self, content_id: uuid.UUID) -> list[Publication]:
        q = select(t.PublicationRow).where(t.PublicationRow.content_id == content_id)
        return [Publication.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def by_account(self, account_id: uuid.UUID, since: datetime) -> list[Publication]:
        q = select(t.PublicationRow).where(t.PublicationRow.channel_account_id == account_id,
                                           t.PublicationRow.updated_at >= since)
        return [Publication.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def by_link(self, link_id: uuid.UUID) -> Publication | None:
        row = (await self.s.execute(select(t.PublicationRow)
                                    .where(t.PublicationRow.link_id == link_id))).scalar_one_or_none()
        return Publication.model_validate(row) if row else None

    async def published_since(self, since: datetime, workspace_id: uuid.UUID | None = None) -> list[Publication]:
        q = select(t.PublicationRow).where(t.PublicationRow.state == "PUBLISHED",
                                           t.PublicationRow.published_at >= since)
        if workspace_id is not None:
            q = q.join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id).where(
                t.ContentRow.workspace_id == workspace_id)
        return [Publication.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def pending_for_account(self, account_id: uuid.UUID) -> list[Publication]:
        q = select(t.PublicationRow).where(t.PublicationRow.channel_account_id == account_id,
                                           t.PublicationRow.state == "QUEUED")
        return [Publication.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def state_counts(self, since: datetime) -> dict[str, int]:
        q = (select(t.PublicationRow.state, func.count())
             .where(t.PublicationRow.scheduled_at >= since).group_by(t.PublicationRow.state))
        return {s: int(n) for s, n in (await self.s.execute(q)).all()}


class RunRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, run: CommandRun) -> CommandRun:
        self.s.add(t.CommandRunRow(
            id=run.id, workspace_id=run.workspace_id, raw_command=run.raw_command, plan=_json(run.plan),
            status=run.status.value, approved_by=run.approved_by, cost_krw=run.cost_krw,
            est_cost_krw=run.plan.est_cost_krw if run.plan else 0.0, summary=run.summary,
            started_at=run.started_at, ended_at=run.ended_at))
        await self.s.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> CommandRun | None:
        row = await self.s.get(t.CommandRunRow, run_id)
        if row is None:
            return None
        return CommandRun.model_validate({
            "id": row.id, "workspace_id": row.workspace_id, "raw_command": row.raw_command, "plan": row.plan,
            "status": row.status, "approved_by": row.approved_by, "cost_krw": row.cost_krw,
            "started_at": row.started_at, "ended_at": row.ended_at, "summary": row.summary or {}})

    async def update(self, run_id: uuid.UUID, **fields: Any) -> None:
        if "status" in fields and hasattr(fields["status"], "value"):
            fields["status"] = fields["status"].value
        if "plan" in fields and isinstance(fields["plan"], BaseModel):
            fields["plan"] = fields["plan"].model_dump(mode="json")
        await self.s.execute(update(t.CommandRunRow).where(t.CommandRunRow.id == run_id).values(**fields))

    async def recent(self, workspace_id: uuid.UUID, limit: int = 50) -> list[t.CommandRunRow]:
        q = (select(t.CommandRunRow).where(t.CommandRunRow.workspace_id == workspace_id)
             .order_by(t.CommandRunRow.started_at.desc()).limit(limit))
        return list((await self.s.execute(q)).scalars().all())


class CostLedger:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def charge(self, *, workspace_id: uuid.UUID, run_id: uuid.UUID | None, stage: str, amount_krw: float,
                     key: str, at: datetime, content_id: uuid.UUID | None = None,
                     detail: dict[str, Any] | None = None) -> bool:
        """Idempotent charge; returns False when this key was already charged."""
        exists = (await self.s.execute(select(t.CostLedgerRow.id)
                                       .where(t.CostLedgerRow.idempotency_key == key))).scalar_one_or_none()
        if exists is not None:
            return False
        self.s.add(t.CostLedgerRow(workspace_id=workspace_id, run_id=run_id, content_id=content_id, stage=stage,
                                   amount_krw=amount_krw, idempotency_key=key, detail=detail or {}, at=at))
        await self.s.flush()
        return True

    async def run_total(self, run_id: uuid.UUID) -> float:
        q = select(func.coalesce(func.sum(t.CostLedgerRow.amount_krw), 0.0)).where(t.CostLedgerRow.run_id == run_id)
        return float((await self.s.execute(q)).scalar_one())

    async def workspace_total(self, workspace_id: uuid.UUID, since: datetime) -> float:
        q = select(func.coalesce(func.sum(t.CostLedgerRow.amount_krw), 0.0)).where(
            t.CostLedgerRow.workspace_id == workspace_id, t.CostLedgerRow.at >= since)
        return float((await self.s.execute(q)).scalar_one())

    async def content_total(self, content_id: uuid.UUID) -> float:
        q = select(func.coalesce(func.sum(t.CostLedgerRow.amount_krw), 0.0)).where(
            t.CostLedgerRow.content_id == content_id)
        return float((await self.s.execute(q)).scalar_one())


class ActivityCache:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def get(self, key: str) -> dict[str, Any] | None:
        row = await self.s.get(t.ActivityResultRow, key)
        return row.result if row else None

    async def put(self, key: str, stage: str, result: dict[str, Any], at: datetime) -> None:
        if await self.s.get(t.ActivityResultRow, key) is None:
            self.s.add(t.ActivityResultRow(key=key, stage=stage, result=result, created_at=at))
            await self.s.flush()


class Outbox:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, event: Event) -> None:
        self.s.add(t.OutboxRow(id=event.id, event=event.model_dump(mode="json"), created_at=event.occurred_at,
                               published_at=None))
        await self.s.flush()

    async def pending(self, limit: int = 200) -> list[Event]:
        q = select(t.OutboxRow).where(t.OutboxRow.published_at.is_(None)).order_by(t.OutboxRow.created_at).limit(limit)
        return [Event.model_validate(r.event) for r in (await self.s.execute(q)).scalars().all()]

    async def mark_published(self, ids: Sequence[uuid.UUID], at: datetime) -> None:
        if ids:
            await self.s.execute(update(t.OutboxRow).where(t.OutboxRow.id.in_(list(ids))).values(published_at=at))

    async def purge(self, before: datetime) -> int:
        res = await self.s.execute(delete(t.OutboxRow).where(t.OutboxRow.published_at < before))
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


class ArmRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def get(self, workspace_id: uuid.UUID, arm_key: str) -> ArmPosteriorState | None:
        row = await self.s.get(t.ArmPosteriorRow, (workspace_id, arm_key))
        return ArmPosteriorState.model_validate(row) if row else None

    async def put(self, workspace_id: uuid.UUID, state: ArmPosteriorState) -> None:
        row = await self.s.get(t.ArmPosteriorRow, (workspace_id, state.arm_key))
        data = state.model_dump()
        if row is None:
            self.s.add(t.ArmPosteriorRow(workspace_id=workspace_id, **data))
        else:
            for k, v in data.items():
                setattr(row, k, v)
        await self.s.flush()

    async def all(self, workspace_id: uuid.UUID) -> list[ArmPosteriorState]:
        q = select(t.ArmPosteriorRow).where(t.ArmPosteriorRow.workspace_id == workspace_id)
        return [ArmPosteriorState.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def record_observation(self, workspace_id: uuid.UUID, publication_id: uuid.UUID, arm_key: str,
                                 impressions: float, clicks: float, orders: float, commission_krw: float,
                                 at: datetime) -> t.ArmObservationRow | None:
        """Returns the *previous* observation for this publication (for delta updates), or None."""
        row = await self.s.get(t.ArmObservationRow, publication_id)
        prev = None
        if row is None:
            self.s.add(t.ArmObservationRow(publication_id=publication_id, workspace_id=workspace_id,
                                           arm_key=arm_key, impressions=impressions, clicks=clicks,
                                           orders=orders, commission_krw=commission_krw, observed_at=at))
        else:
            prev = t.ArmObservationRow(publication_id=row.publication_id, workspace_id=row.workspace_id,
                                       arm_key=row.arm_key, impressions=row.impressions, clicks=row.clicks,
                                       orders=row.orders, commission_krw=row.commission_krw,
                                       observed_at=row.observed_at)
            row.impressions, row.clicks, row.orders = impressions, clicks, orders
            row.commission_krw, row.observed_at = commission_krw, at
        await self.s.flush()
        return prev


class PlaybookRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def add(self, rule: PlaybookRule, embedding: Sequence[float] | None = None) -> PlaybookRule:
        self.s.add(t.PlaybookRuleRow(**_dump(rule), embedding=list(embedding) if embedding else None))
        await self.s.flush()
        return rule

    async def active(self, workspace_id: uuid.UUID | None, limit: int = 50) -> list[PlaybookRule]:
        q = select(t.PlaybookRuleRow).where(t.PlaybookRuleRow.active.is_(True))
        if workspace_id is not None:
            q = q.where((t.PlaybookRuleRow.workspace_id == workspace_id) | (t.PlaybookRuleRow.workspace_id.is_(None)))
        q = q.order_by(t.PlaybookRuleRow.confidence.desc()).limit(limit)
        return [PlaybookRule.model_validate(r) for r in (await self.s.execute(q)).scalars().all()]

    async def deactivate_scope(self, workspace_id: uuid.UUID | None, scope_key: str) -> None:
        rows = (await self.s.execute(select(t.PlaybookRuleRow).where(t.PlaybookRuleRow.active.is_(True)))).scalars()
        for r in rows:
            if (r.scope or {}).get("key") == scope_key and r.workspace_id == workspace_id:
                r.active = False
        await self.s.flush()


class AuditRepo:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def write(self, entry: AuditEntry) -> None:
        self.s.add(t.AuditLogRow(**_dump(entry)))
        await self.s.flush()

    async def recent(self, limit: int = 200, action: str | None = None) -> list[AuditEntry]:
        q = select(t.AuditLogRow)
        if action:
            q = q.where(t.AuditLogRow.action == action)
        rows = (await self.s.execute(q.order_by(t.AuditLogRow.at.desc()).limit(limit))).scalars().all()
        return [AuditEntry.model_validate(r) for r in rows]

    async def purge_older_than(self, cutoff: datetime) -> int:
        res = await self.s.execute(delete(t.AuditLogRow).where(t.AuditLogRow.at < cutoff))
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


class KVCache:
    def __init__(self, s: AsyncSession) -> None:
        self.s = s

    async def get(self, key: str, now: datetime) -> dict[str, Any] | None:
        row = await self.s.get(t.KVCacheRow, key)
        if row is None or row.expires_at <= now:
            return None
        return row.value

    async def get_for_update(self, key: str) -> t.KVCacheRow | None:
        """Row-locked read (SELECT … FOR UPDATE on PostgreSQL; SQLite serialises writers anyway) for
        read-modify-write state shared across workers, e.g. circuit breakers."""
        return await self.s.get(t.KVCacheRow, key, with_for_update=True)

    async def get_stale(self, key: str) -> dict[str, Any] | None:
        """Return the value even if expired — the 'operate from cache' fallback during a 403 (§9.5)."""
        row = await self.s.get(t.KVCacheRow, key)
        return row.value if row else None

    async def put(self, key: str, value: dict[str, Any], ttl: timedelta, now: datetime) -> None:
        row = await self.s.get(t.KVCacheRow, key)
        if row is None:
            self.s.add(t.KVCacheRow(key=key, value=value, expires_at=now + ttl))
        else:
            row.value, row.expires_at = value, now + ttl
        await self.s.flush()


class Repos:
    """Facade bundling every repository on one session (one transaction)."""

    def __init__(self, s: AsyncSession) -> None:
        self.session = s
        self.workspaces = WorkspaceRepo(s)
        self.accounts = AccountRepo(s)
        self.products = ProductRepo(s)
        self.dna = DNARepo(s)
        self.contents = ContentRepo(s)
        self.assets = AssetRepo(s)
        self.compliance = ComplianceRepo(s)
        self.links = LinkRepo(s)
        self.publications = PublicationRepo(s)
        self.runs = RunRepo(s)
        self.costs = CostLedger(s)
        self.activities = ActivityCache(s)
        self.outbox = Outbox(s)
        self.arms = ArmRepo(s)
        self.playbook = PlaybookRepo(s)
        self.audit = AuditRepo(s)
        self.kv = KVCache(s)

