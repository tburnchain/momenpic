"""M10 optimizer: exact delta updates, hierarchical priors, amplification, retirement, allocation, G3, playbook."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from acvm.context import AppContext
from acvm.core.enums import ContentKind, HookType, Platform, PublicationState
from acvm.core.events import Event, PerfUpdated
from acvm.core.ids import publication_key, short_code, sub_id_for
from acvm.db import tables as t
from acvm.domain.models import (
    AffiliateLink,
    ArmKey,
    ArmPosteriorState,
    ChannelAccount,
    Content,
    Product,
    Publication,
    Script,
)
from acvm.modules.m9_analytics import learning_view
from acvm.modules.m10_optimizer import Observation, Optimizer, apply_delta
from acvm.modules.m10_optimizer.posterior import (
    centred_prior,
    economics_prior,
    pooled_posterior,
    state_from_prior,
    to_posterior,
)
from acvm.ports import AffiliateReportRow, PerfRow

CAT = "1013"


@pytest.fixture
async def product(ctx: AppContext, product_factory) -> Product:  # type: ignore[no-untyped-def]
    p = product_factory(category=CAT)
    async with ctx.uow() as r:
        await r.products.upsert(p)
    return p


async def seed_pub(ctx: AppContext, account: ChannelAccount, product: Product, published_at: datetime, *,
                   hook: HookType = HookType.QUESTION, views: float | None = None,
                   link_clicks: float | None = None) -> tuple[Content, AffiliateLink, Publication]:
    content = Content(workspace_id=account.workspace_id, product_id=product.id, kind=ContentKind.SHORT,
                      persona="30대 여성 직장인", template_id="problem-solution-v3", created_at=published_at,
                      script=Script(hooks=["h"], hook_type=hook, persona="30대 여성 직장인",
                                    template_id="problem-solution-v3"))
    key = publication_key(content.id, account.platform.value, account.id)
    link = AffiliateLink(product_id=product.id, sub_id=sub_id_for(key), deeplink="https://link.coupang.com/re/x",
                         short_url="https://link.coupang.com/a/x", hub_code=short_code(key), signature="sig",
                         publication_key=key, created_at=published_at)
    pub = Publication(content_id=content.id, channel_account_id=account.id, platform=account.platform,
                      link_id=link.id, platform_post_id=f"post-{content.id}", state=PublicationState.PUBLISHED,
                      scheduled_at=published_at, published_at=published_at, idempotency_key=key)
    async with ctx.uow() as r:
        await r.contents.add(content)
        await r.links.add(link)
        await r.publications.add(pub)
    rows = []
    if views is not None:
        rows.append(PerfRow(ctx.clock.now(), content.id, pub.id, account.platform.value, "views", views))
    if link_clicks is not None:
        rows.append(PerfRow(ctx.clock.now(), content.id, pub.id, account.platform.value, "link_clicks", link_clicks))
    if rows:
        await ctx.perf.insert_perf(rows)
    return content, link, pub


def perf_event(content_id: Any) -> Event:
    return Event.of(PerfUpdated(content_id=content_id, epc=0.0, percentile=0.5))


def make_state(ctx: AppContext, key: str, obs: Observation, trials: int, *, retired: bool = False) -> ArmPosteriorState:
    now = ctx.clock.now()
    prior = economics_prior(ctx.settings.economics, ctx.settings.optimizer.prior_strength)
    st = apply_delta(state_from_prior(key, prior, now), obs, None, now)
    return st.model_copy(update={"trials": trials, "retired": retired})


def key_of(hook: str = "question", template: str = "problem-solution-v3", persona: str = "30대 여성 직장인",
           platform: str = "threads", slot: str = "evening", category: str = CAT) -> str:
    return ArmKey(category=category, hook_type=hook, template=template, persona=persona, platform=platform,
                  slot=slot).key()


async def test_delta_update_is_idempotent_and_exact(ctx: AppContext, workspace, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()  # 09:30 KST
    content, link, pub = await seed_pub(ctx, accounts[Platform.THREADS], product, now - timedelta(days=1),
                                        views=1000, link_clicks=10)
    await ctx.perf.insert_reports([AffiliateReportRow(date(2026, 10, 7), link.sub_id, 12, 2, 60_000, 1_800, 0)])
    opt = Optimizer(ctx)
    key = key_of(slot="morning")
    prior = centred_prior(economics_prior(ctx.settings.economics, 20.0), 20.0)

    out = await opt.on_perf(perf_event(content.id))
    assert [u.arm_key for u in out.arm_updates] == [key]
    async with ctx.uow() as r:
        s1 = await r.arms.get(workspace.id, key)
    assert s1 is not None and s1.trials == 1
    assert s1.ctr_alpha == pytest.approx(prior.ctr.alpha + 12)  # report clicks preferred over link clicks
    assert s1.ctr_beta == pytest.approx(prior.ctr.beta + 988)
    assert s1.order_shape == pytest.approx(prior.orders_per_click.shape + 2)
    assert s1.order_rate == pytest.approx(prior.orders_per_click.rate + 12)
    assert s1.comm_rate == pytest.approx(prior.commission_rate_param.rate + 1_800)
    assert out.arm_updates[0].posterior["trials"] == 1.0

    again = await opt.on_perf(perf_event(content.id))  # redelivery: nothing moves
    async with ctx.uow() as r:
        s2 = await r.arms.get(workspace.id, key)
    assert again.arm_updates == [] and s2 == s1

    await ctx.perf.insert_perf([PerfRow(now + timedelta(hours=1), content.id, pub.id, "threads", "views", 2000),
                                PerfRow(now + timedelta(hours=1), content.id, pub.id, "threads", "link_clicks", 25)])
    await ctx.perf.insert_reports([AffiliateReportRow(date(2026, 10, 8), link.sub_id, 5, 1, 30_000, 1_000, 0)])
    await opt.on_perf(perf_event(content.id))
    async with ctx.uow() as r:
        s3 = await r.arms.get(workspace.id, key)
    assert s3 is not None and s3.trials == 1 and s3.impressions == 2000
    assert s3.ctr_alpha == pytest.approx(prior.ctr.alpha + 17)  # exactly prior + latest observation
    assert s3.ctr_beta == pytest.approx(prior.ctr.beta + 1983)
    assert s3.order_shape == pytest.approx(prior.orders_per_click.shape + 3)
    assert s3.order_rate == pytest.approx(prior.orders_per_click.rate + 17)
    assert s3.comm_shape == pytest.approx(prior.commission_rate_param.shape + 3)
    assert s3.comm_rate == pytest.approx(prior.commission_rate_param.rate + 2_800)
    async with ctx.uow() as r:
        arm_events = [e for e in await r.outbox.pending() if e.type == "arm.updated"]
    assert len(arm_events) == 2 and len({e.idempotency_key for e in arm_events}) == 2


async def test_new_arm_prior_is_centred_on_category_platform_parent(ctx: AppContext, workspace, accounts,  # type: ignore[no-untyped-def]
                                                                     product: Product) -> None:
    sibling = make_state(ctx, key_of(hook="shock", slot="night"), Observation(100_000, 2_000, 100, 120_000), 40)
    other_cat = make_state(ctx, key_of(hook="shock", category="9999"), Observation(100_000, 50, 0, 0), 40)
    async with ctx.uow() as r:
        await r.arms.put(workspace.id, sibling)
        await r.arms.put(workspace.id, other_cat)
    now = ctx.clock.now()
    content, _, _ = await seed_pub(ctx, accounts[Platform.THREADS], product, now - timedelta(days=1), views=1)
    await Optimizer(ctx).on_perf(perf_event(content.id))
    async with ctx.uow() as r:
        st = await r.arms.get(workspace.id, key_of(slot="morning"))
    econ = economics_prior(ctx.settings.economics, 20.0)
    expected = centred_prior(pooled_posterior(econ, [sibling]), 20.0)  # other categories don't leak in
    assert st is not None
    assert st.ctr_alpha == pytest.approx(expected.ctr.alpha)  # views=1, clicks=0 ⇒ α unchanged
    assert st.order_shape == pytest.approx(expected.orders_per_click.shape)
    assert to_posterior(st).ctr.mean > econ.ctr.mean  # pulled toward the 2 % sibling CTR


async def test_amplification_top_2pct_within_48h_once(ctx: AppContext, workspace, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()
    th = accounts[Platform.THREADS]
    for i in range(30):
        await seed_pub(ctx, th, product, now - timedelta(days=3 + i % 20), views=100 + i)
    opt = Optimizer(ctx)
    old_hot, _, _ = await seed_pub(ctx, th, product, now - timedelta(hours=60), views=20_000)
    assert (await opt.on_perf(perf_event(old_hot.id))).amplify is None  # top 2 % but outside the 48 h window
    warm, _, _ = await seed_pub(ctx, th, product, now - timedelta(hours=5), views=135)
    assert (await opt.on_perf(perf_event(warm.id))).amplify is None  # inside the window, not top 2 %
    hot, _, _ = await seed_pub(ctx, th, product, now - timedelta(hours=10), views=30_000)
    out = await opt.on_perf(perf_event(hot.id))
    assert out.amplify is not None and out.amplify.content_id == hot.id and out.amplify.variants == 5
    assert out.amplify.cross_platforms == [p for p in Platform if p is not Platform.THREADS]
    assert (await opt.on_perf(perf_event(hot.id))).amplify is None  # once per content
    async with ctx.uow() as r:
        amp = [e for e in await r.outbox.pending() if e.type == "amplify.requested"]
    assert len(amp) == 1 and amp[0].idempotency_key == f"amplify:{hot.id}"


async def test_retirement_bottom_decile_of_mature_arms(ctx: AppContext, workspace) -> None:  # type: ignore[no-untyped-def]
    opt = Optimizer(ctx)
    personas = [f"p{i}" for i in range(12)]
    async with ctx.uow() as r:
        for i, persona in enumerate(personas[:5]):
            await r.arms.put(workspace.id, make_state(ctx, key_of(persona=persona),
                                                      Observation(30_000, 300, 9, 9 * (300 + 100 * i)), 30))
    assert await opt.apply_retirement(workspace.id) == []  # < 10 mature arms: no decile yet
    async with ctx.uow() as r:
        for i, persona in enumerate(personas[5:], start=5):
            await r.arms.put(workspace.id, make_state(ctx, key_of(persona=persona),
                                                      Observation(30_000, 300, 9, 9 * (300 + 100 * i)), 30 + i))
        await r.arms.put(workspace.id, make_state(ctx, key_of(persona="young"), Observation(1_000, 1, 0, 0), 3))
    retired = await opt.apply_retirement(workspace.id)
    assert sorted(retired) == sorted([key_of(persona="p0"), key_of(persona="p1")])  # idx ⌊0.1·11⌋ = 1
    assert await opt.apply_retirement(workspace.id) == []  # sticky, no re-emission
    async with ctx.uow() as r:
        young = await r.arms.get(workspace.id, key_of(persona="young"))
    assert young is not None and not young.retired  # immature arms are never retired

    # a retired arm is never allocated, however good its posterior looks
    star = make_state(ctx, key_of(persona="star"), Observation(10_000, 1_000, 500, 5_000_000), 50, retired=True)
    async with ctx.uow() as r:
        await r.arms.put(workspace.id, star)
    recs = await opt.recommend(workspace.id, CAT, 30, [Platform.THREADS], seed_key="retire-check")
    assert star.arm_key not in {rec.arm_key for rec in recs}


async def test_recommend_exploration_budget_and_determinism(ctx: AppContext, workspace) -> None:  # type: ignore[no-untyped-def]
    opt = Optimizer(ctx)
    champion = make_state(ctx, key_of(hook="price_twist", template="price-twist-v1"),
                          Observation(400_000, 8_000, 400, 480_000), 60)  # RPI ≈ 1.2 KRW/view
    async with ctx.uow() as r:
        await r.arms.put(workspace.id, champion)
        for hook in ("question", "shock", "empathy", "before_after"):  # mature, average arms (RPI ≈ 0.22)
            await r.arms.put(workspace.id, make_state(ctx, key_of(hook=hook, template="listicle-v1"),
                                                      Observation(400_000, 3_200, 96, 86_400), 60))
    plats = [Platform.THREADS, Platform.INSTAGRAM]
    recs = await opt.recommend(workspace.id, CAT, 10, plats, seed_key="run-1")
    assert [r.mode for r in recs].count("explore") == 2 and [r.mode for r in recs].count("exploit") == 8
    assert recs == await opt.recommend(workspace.id, CAT, 10, plats, seed_key="run-1")
    assert recs != await opt.recommend(workspace.id, CAT, 10, plats, seed_key="run-2")
    explore = [r for r in recs if r.mode == "explore"]
    assert all(r.arm_key != champion.arm_key for r in explore)  # explore slots only go to young arms
    picks: Counter[str] = Counter()  # exploit concentrates on the proven winner among ~320 candidates
    for i in range(10):
        picks.update(r.arm_key for r in await opt.recommend(workspace.id, CAT, 10, plats, seed_key=f"agg-{i}")
                     if r.mode == "exploit")
    top, top_n = picks.most_common(1)[0]
    assert top == champion.arm_key and top_n >= 25  # ≥ 25 of 80 exploit slots
    assert {r.platform for r in recs} <= set(plats)
    assert all(r.hook_type in {h.value for h in HookType} for r in recs)
    assert await opt.recommend(workspace.id, CAT, 0, plats, seed_key="x") == []

    lv = await learning_view(ctx, workspace.id)  # §9.3 탐색/활용 비율 from recorded allocations
    assert lv["allocation"]["explore"] >= 2 and lv["allocation"]["explore_share"] is not None
    assert lv["arms"][0]["arm_key"] == champion.arm_key


async def test_platform_admission_g3(ctx: AppContext, workspace, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()
    for i in range(30):
        await seed_pub(ctx, accounts[Platform.THREADS], product, now - timedelta(days=1 + i % 25), views=1500 + 10 * i)
        await seed_pub(ctx, accounts[Platform.INSTAGRAM], product, now - timedelta(days=1 + i % 25), views=80 + i)
    adm = await Optimizer(ctx).platform_admission(workspace.id)
    assert adm["threads"]["admitted"] is True and adm["threads"]["ratio"] > 1
    assert adm["threads"]["breakeven_views"] == pytest.approx(694.4444, rel=1e-4)
    assert adm["instagram"]["admitted"] is False and adm["instagram"]["ratio"] < 1
    assert adm["youtube"]["admitted"] is False and adm["youtube"]["n"] == 0
    assert set(adm) == {p.value for p in Platform}


class _Rephraser:
    def __init__(self, keep: bool) -> None:
        self.keep = keep

    async def text(self, *, tier: str, system: str, user: str, **_: Any) -> str:
        assert tier == "small" and "untrusted" in system
        return "가격반전 훅이 잘 통합니다 +999% (확률 0.50)" if not self.keep else "REPHRASED"


async def _playbook_arms(ctx: AppContext, workspace_id: Any, price_orders: int) -> None:
    async with ctx.uow() as r:
        for hook in HookType:
            for template in ("problem-solution-v3", "before-after-v1", "listicle-v1"):
                orders = price_orders if hook is HookType.PRICE_TWIST else 6
                await r.arms.put(workspace_id, make_state(
                    ctx, key_of(hook=hook.value, template=template), Observation(20_000, 200, orders, orders * 900), 5))


async def test_refresh_playbook_writes_numeric_korean_rules(ctx: AppContext, workspace, product: Product) -> None:  # type: ignore[no-untyped-def]
    opt = Optimizer(ctx)
    await _playbook_arms(ctx, workspace.id, price_orders=12)
    rules = await opt.refresh_playbook(workspace.id)
    ctx_rule = next(r for r in rules if r.scope["category"] == CAT)
    assert ctx_rule.text.startswith("주방용품(1013) · Threads에서 '가격반전(price_twist)' 훅은 평균 대비 수익/노출 +")
    assert ctx_rule.text.endswith(f"(확률 {ctx_rule.confidence:.2f}, 표본 15건)")
    assert f"{ctx_rule.lift * 100:+.0f}%" in ctx_rule.text and ctx_rule.lift > 0.2 and ctx_rule.confidence >= 0.9
    assert any(r.text.startswith("전체 채널 기준 '가격반전(price_twist)' 훅은") for r in rules)
    assert all(r.scope["value"] == "price_twist" for r in rules)  # no rule for the average hooks
    async with ctx.uow() as r:
        rows = (await r.session.execute(select(t.PlaybookRuleRow))).scalars().all()
    assert len(rows) == len(rules) and all(row.embedding is not None and row.active for row in rows)

    again = await opt.refresh_playbook(workspace.id)  # unchanged data ⇒ same rules, no new rows
    assert [r.id for r in again] == [r.id for r in rules]
    async with ctx.uow() as r:
        assert (await r.session.execute(select(func.count()).select_from(t.PlaybookRuleRow))).scalar_one() == len(rules)

    ctx.llm = _Rephraser(keep=False)  # type: ignore[assignment]
    await _playbook_arms(ctx, workspace.id, price_orders=20)  # stronger effect ⇒ superseding rules
    newer = await opt.refresh_playbook(workspace.id)
    assert {r.scope["key"] for r in newer} == {r.scope["key"] for r in rules}
    assert all("+999%" not in r.text for r in newer)  # rephrasing that changed numbers was rejected
    async with ctx.uow() as r:
        active = await r.playbook.active(workspace.id)
        total = (await r.session.execute(select(func.count()).select_from(t.PlaybookRuleRow))).scalar_one()
    assert {a.id for a in active} == {n.id for n in newer} and total == 2 * len(rules)


async def test_epc_prior(ctx: AppContext, workspace) -> None:  # type: ignore[no-untyped-def]
    opt = Optimizer(ctx)
    assert await opt.epc_prior(workspace.id, CAT) == pytest.approx(0.03 * 900)
    async with ctx.uow() as r:
        await r.arms.put(workspace.id, make_state(ctx, key_of(), Observation(100_000, 10_000, 1_000, 1_500_000), 50))
    assert await opt.epc_prior(workspace.id, CAT) > 0.03 * 900
    assert await opt.epc_prior(workspace.id, "other") == pytest.approx(0.03 * 900)


async def test_perf_updated_flows_from_m9_through_bus_to_m10_and_m2(ctx: AppContext, workspace, accounts,  # type: ignore[no-untyped-def]
                                                                    product: Product) -> None:
    from acvm.modules.m2_dna import ContentDNAService, own_dna_id
    from acvm.modules.m9_analytics import AnalyticsService

    now = ctx.clock.now()
    th = accounts[Platform.THREADS]
    for i in range(30):
        await seed_pub(ctx, th, product, now - timedelta(days=2 + i % 20), views=100 + i, link_clicks=1)
    hot, link, _ = await seed_pub(ctx, th, product, now - timedelta(hours=20), views=50_000, link_clicks=400)
    await ctx.perf.insert_reports([AffiliateReportRow(date(2026, 10, 7), link.sub_id, 350, 9, 270_000, 8_100, 0)])
    Optimizer(ctx).subscribe()
    ContentDNAService(ctx).subscribe()

    events = await AnalyticsService(ctx).attribute()
    assert len(events) == 31
    await ctx.relay_outbox()  # perf.updated → M10 (arms, amplify) and M2 (tail DNA)
    async with ctx.uow() as r:
        arms = await r.arms.all(workspace.id)
        dna = await r.dna.get(own_dna_id(hot.id))
    in_horizon = sum(1 for i in range(30) if 2 + i % 20 <= 7) + 1  # 7-day reward horizon (+ the hot post)
    assert sum(a.trials for a in arms) == in_horizon == 13
    assert dna is not None and dna.perf_percentile > 0.98
    await ctx.relay_outbox()  # arm.updated / amplify.requested have no subscribers here; publish is harmless
    assert len(ctx.bus.of_type("amplify.requested")) == 1  # type: ignore[attr-defined]
