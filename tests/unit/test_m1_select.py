"""M1 ProductIntelligence.select / score / refresh_price against the deterministic sandbox (§4 M1, §5.2)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.enums import ContentKind, Platform, PublicationState
from acvm.core.errors import NotFound, PolicyViolation, ValidationFailed
from acvm.core.events import ProductSelected
from acvm.core.ids import publication_key, sub_id_for
from acvm.domain.models import AffiliateLink, ChannelAccount, Content, Product, Publication, Script, Workspace
from acvm.infra.coupang_sandbox import SandboxCoupangClient, coupang_client_for
from acvm.modules.m1_product import ProductIntelligence


def _sandbox(ctx: AppContext) -> SandboxCoupangClient:
    client = coupang_client_for(ctx)
    assert isinstance(client, SandboxCoupangClient)
    return client


async def _selected_events(ctx: AppContext, run_id: uuid.UUID) -> list[ProductSelected]:
    async with ctx.uow() as repos:
        events = await repos.outbox.pending(1_000)
    out = [e.parsed() for e in events if e.type == ProductSelected.TYPE and e.run_id == run_id]
    return [e for e in out if isinstance(e, ProductSelected)]


async def test_select_returns_top_n_above_bar_and_emits_events(ctx: AppContext, workspace: Workspace) -> None:
    run = uuid.uuid4()
    m1 = ProductIntelligence(ctx)
    picks = await m1.select(run_id=run, workspace_id=workspace.id, category_id="1013", n=5)
    assert len(picks) == 5
    assert [p.rank for p in picks] == [1, 2, 3, 4, 5]
    assert [p.pos for p in picks] == sorted((p.pos for p in picks), reverse=True)
    assert all(p.pos >= 0.7 and p.run_id == run for p in picks)
    ids = {p.product_id for p in picks}
    assert 7101300015 not in ids and 7101300016 not in ids  # sold out / discontinued never selected
    assert 7101300017 not in ids  # 389 000 KRW oven: outside the impulse band, low POS

    events = await _selected_events(ctx, run)
    assert sorted(e.product_id for e in events) == sorted(ids)
    async with ctx.uow() as repos:
        raw = [e for e in await repos.outbox.pending(1_000) if e.type == ProductSelected.TYPE]
        score = await repos.products.latest_score(picks[0].product_id)
        product = await repos.products.get(picks[0].product_id)
    assert {e.idempotency_key for e in raw} == {f"{run}:{pid}:selected" for pid in ids}
    assert all(e.workspace_id == workspace.id and e.occurred_at == ctx.clock.now() for e in raw)
    assert score is not None and score.model_version == "pos-v1" and score.pos == picks[0].pos
    assert product is not None and product.product_url.startswith("https://www.coupang.com/vp/products/")


async def test_select_is_idempotent_per_run(ctx: AppContext, workspace: Workspace) -> None:
    run = uuid.uuid4()
    m1 = ProductIntelligence(ctx)
    first = await m1.select(run_id=run, workspace_id=workspace.id, category_id="1018", n=3, keyword="거치대")
    calls = dict(_sandbox(ctx).transport_calls)
    again = await m1.select(run_id=run, workspace_id=workspace.id, category_id="1018", n=3, keyword="거치대")
    assert again == first
    assert dict(_sandbox(ctx).transport_calls) == calls  # replay costs no API budget
    assert len(await _selected_events(ctx, run)) == 3  # events emitted exactly once
    other = await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                                  category_id="1018", n=3, keyword="거치대")
    assert [p.product_id for p in other] == [p.product_id for p in first]  # same inputs → same choice


async def test_regulated_goods_are_hard_excluded(ctx: AppContext, workspace: Workspace) -> None:
    picks = await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                                  category_id="1014", n=20, min_pos=0.0)
    assert picks and 7101400013 not in {p.product_id for p in picks}  # KF94 마스크 (의약외품)
    async with ctx.uow() as repos:
        mask = await repos.products.get(7101400013)
    assert mask is not None and mask.regulated
    with pytest.raises(PolicyViolation):
        await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                              category_id="1024", n=5)


async def test_near_duplicate_titles_keep_the_higher_pos(ctx: AppContext, workspace: Workspace) -> None:
    picks = await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                                  category_id="1013", n=20, min_pos=0.0, keyword="밀봉 클립")
    ids = [p.product_id for p in picks]
    assert 7101300001 in ids and 7101300013 not in ids  # "과자밀봉 클립 20개입 대용량세트" re-listing dropped
    async with ctx.uow() as repos:
        original = await repos.products.latest_score(7101300001)
        relisting = await repos.products.latest_score(7101300013)
    assert original is not None and relisting is not None and original.pos >= relisting.pos


async def test_bar_is_never_lowered_silently(ctx: AppContext, workspace: Workspace) -> None:
    picks = await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                                  category_id="1013", n=5, min_pos=0.99)
    assert picks == []
    with pytest.raises(ValidationFailed):
        await ProductIntelligence(ctx).select(run_id=uuid.uuid4(), workspace_id=workspace.id,
                                              category_id="9999", n=5)


async def test_recently_published_and_explicit_exclusions(ctx: AppContext, workspace: Workspace,
                                                          accounts: dict[Platform, ChannelAccount],
                                                          clock: FrozenClock) -> None:
    m1 = ProductIntelligence(ctx)
    baseline = await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1013", n=3)
    top, second = baseline[0].product_id, baseline[1].product_id
    acc = accounts[Platform.THREADS]
    content = Content(workspace_id=workspace.id, product_id=top, kind=ContentKind.TEXT, persona="30대 여성",
                      template_id="text-v1", created_at=clock.now() - timedelta(days=3),
                      script=Script(hooks=["h"], hook_type="question", persona="30대 여성", template_id="text-v1"))
    key = publication_key(content.id, "threads", acc.id)
    async with ctx.uow() as repos:
        await repos.contents.add(content)
        link = await repos.links.add(AffiliateLink(
            product_id=top, sub_id=sub_id_for(key), deeplink="https://link.coupang.com/re/x",
            short_url="https://link.coupang.com/a/x", hub_code="abcd2345", signature="s", publication_key=key,
            created_at=clock.now()))
        await repos.publications.add(Publication(
            content_id=content.id, channel_account_id=acc.id, platform=Platform.THREADS, link_id=link.id,
            state=PublicationState.PUBLISHED, scheduled_at=clock.now() - timedelta(days=3),
            published_at=clock.now() - timedelta(days=3), idempotency_key=key))
    picks = await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1013", n=3,
                            exclude_product_ids=[second])
    ids = {p.product_id for p in picks}
    assert top not in ids and second not in ids and len(ids) == 3


async def test_budget_exhaustion_falls_back_to_recent_db_products(ctx: AppContext, workspace: Workspace,
                                                                  clock: FrozenClock) -> None:
    m1 = ProductIntelligence(ctx)
    warm = await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1018", n=3)
    clock.advance(hours=7)  # API cache expired
    while (await ctx.quota.take_token("coupang:catalog", 8, 8 / 3600, clock.now())).allowed:
        pass  # another workflow spent the catalogue budget
    calls = _sandbox(ctx).transport_calls["bestcategories"]
    picks = await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1018", n=3)
    assert _sandbox(ctx).transport_calls["bestcategories"] == calls  # nothing reached Coupang
    assert [p.product_id for p in picks] == [p.product_id for p in warm]  # DB products seen < 7 days ago
    clock.advance(days=8)
    while (await ctx.quota.take_token("coupang:catalog", 8, 8 / 3600, clock.now())).allowed:
        pass
    assert await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1018", n=3) == []


async def test_score_persists_and_works_for_unseen_products(ctx: AppContext, product_factory) -> None:  # type: ignore[no-untyped-def]
    product: Product = product_factory(9001, name="원터치 접이식 밀폐 용기")
    score = await ProductIntelligence(ctx).score(product)
    assert 0.0 <= score.pos <= 1.0 and score.features.imputed == ["m_trend"]
    async with ctx.uow() as repos:
        stored = await repos.products.latest_score(9001)
    assert stored == score


async def test_refresh_price_fresh_stale_and_budget_paths(ctx: AppContext, workspace: Workspace,
                                                          clock: FrozenClock) -> None:
    m1 = ProductIntelligence(ctx)
    await m1.select(run_id=uuid.uuid4(), workspace_id=workspace.id, category_id="1013", n=3)
    calls = _sandbox(ctx).transport_calls["search"]
    price, checked_at, fresh = await m1.refresh_price(7101300001)
    assert (price, checked_at, fresh) == (8_900, clock.now(), True)
    assert _sandbox(ctx).transport_calls["search"] == calls  # fresh: no API call

    clock.advance(hours=7)
    price, checked_at, fresh = await m1.refresh_price(7101300001)
    assert (price, checked_at, fresh) == (8_900, clock.now(), True)  # re-checked through search
    assert _sandbox(ctx).transport_calls["search"] == calls + 1

    clock.advance(hours=7)
    while (await ctx.quota.take_token("coupang:search", 8, 8 / 3600, clock.now())).allowed:
        pass
    price, checked_at, fresh = await m1.refresh_price(7101300001)  # budget gone: stale, never raises
    assert (price, checked_at, fresh) == (8_900, clock.now() - timedelta(hours=7), False)
    with pytest.raises(NotFound):
        await m1.refresh_price(42)
