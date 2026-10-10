"""M3 link hub (D-04), click redirect/pause (§4 M9 input, §8.2) and the 6-hourly health checker (§9.5 #4)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

import pytest

from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.enums import ContentKind, LinkHealth, Platform, PublicationState
from acvm.core.errors import NotFound, QuotaExhausted
from acvm.core.events import Event, InvalidClickDetected, LinkHealthChanged
from acvm.domain.models import (
    AffiliateLink,
    ChannelAccount,
    Content,
    Product,
    ProductFeatures,
    ProductScore,
    Publication,
    Script,
    Workspace,
)
from acvm.infra.coupang_sandbox import SandboxCoupangClient, coupang_client_for
from acvm.modules.m3_link import LinkService, hub_record_key, pause_key
from acvm.security.linksign import hash_ip


async def _publish(ctx: AppContext, svc: LinkService, workspace: Workspace, account: ChannelAccount,
                   product: Product, *, state: PublicationState = PublicationState.QUEUED,
                   scheduled_offset: timedelta = timedelta(0)) -> AffiliateLink:
    now = ctx.clock.now()
    script = Script(hooks=["h"], hook_type="question", persona="30대 여성", template_id="problem-solution-v3")
    content = Content(workspace_id=workspace.id, product_id=product.id, kind=ContentKind.SHORT, persona="30대 여성",
                      template_id="problem-solution-v3", script=script, created_at=now)
    async with ctx.uow() as repos:
        if await repos.products.get(product.id) is None:
            await repos.products.upsert(product)
        await repos.contents.add(content)
    link = await svc.create_link(product=product, content_id=content.id, platform=account.platform,
                                 account_id=account.id)
    async with ctx.uow() as repos:
        await repos.publications.add(Publication(
            content_id=content.id, channel_account_id=account.id, platform=account.platform, link_id=link.id,
            state=state, scheduled_at=now + scheduled_offset, idempotency_key=link.publication_key))
    return link


class _BodyProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_body = False
        self.first_tag: tuple[str, dict[str, str | None]] | None = None
        self.first_text: str | None = None
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.in_body = True
            return
        if self.in_body and self.first_tag is None:
            self.first_tag = (tag, dict(attrs))
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href") or "")

    def handle_data(self, data: str) -> None:
        if self.in_body and self.first_text is None and data.strip():
            self.first_text = data.strip()


# ───────────────────────── hub page ─────────────────────────


async def test_hub_page_puts_the_disclosure_first_and_is_stored_for_the_gate(
        ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount], product_factory) -> None:  # type: ignore[no-untyped-def]
    svc = LinkService(ctx)
    ig = accounts[Platform.INSTAGRAM]
    clip = product_factory(7101300001)
    hostile = product_factory(7101300002, name="밀폐용기 <script>alert(1)</script>", price=29_900)
    sold_out = product_factory(7101300015, name="계량컵 세트")
    l1 = await _publish(ctx, svc, workspace, ig, clip, scheduled_offset=timedelta(hours=1))
    l2 = await _publish(ctx, svc, workspace, ig, hostile, state=PublicationState.PUBLISHED)
    l3 = await _publish(ctx, svc, workspace, ig, sold_out)
    l4 = await _publish(ctx, svc, workspace, ig, product_factory(7101300003, name="취소된 상품"),
                        state=PublicationState.CANCELLED)
    await _publish(ctx, svc, workspace, accounts[Platform.TIKTOK], product_factory(7101300004, name="다른 계정"))
    async with ctx.uow() as repos:
        await repos.links.set_health(l3.id, LinkHealth.SOLD_OUT.value, ctx.clock.now())

    html = await svc.build_hub_page(ig.id)

    probe = _BodyProbe()
    probe.feed(html)
    disclosure = ctx.settings.gate.standard_disclosure
    assert probe.first_tag == ("div", {"id": "acvm-disclosure", "role": "note"})
    assert probe.first_text == disclosure  # the very first visible text (D-04)
    assert "position:sticky;top:0" in html
    base = ctx.settings.link_hub_base_url
    assert probe.hrefs == [f"{base}/r/{l1.hub_code}", f"{base}/r/{l2.hub_code}"]  # newest first
    assert l3.hub_code not in html and l4.hub_code not in html  # sold out / cancelled stay off the page
    assert "다른 계정" not in html
    assert "12,900원" in html and "29,900원" in html and "2026-10-08 기준" in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html  # marketplace text is escaped

    async with ctx.uow() as repos:
        record = await repos.kv.get(hub_record_key(ig.id), ctx.clock.now())
    assert record is not None
    assert record["sha256"] == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert record["uri"].endswith("hub/kitchen_pick_instagram.html")
    assert record["built_at"] == ctx.clock.now().isoformat() and record["link_count"] == 2
    assert await ctx.objects.get_bytes(record["uri"]) == html.encode("utf-8")
    with pytest.raises(NotFound):
        await svc.build_hub_page(uuid.uuid4())


async def test_empty_hub_still_carries_the_disclosure(ctx: AppContext,
                                                      accounts: dict[Platform, ChannelAccount]) -> None:
    html = await LinkService(ctx).build_hub_page(accounts[Platform.TIKTOK].id)
    probe = _BodyProbe()
    probe.feed(html)
    assert probe.first_text == ctx.settings.gate.standard_disclosure
    assert "지금 소개 중인 상품이 없습니다" in html


# ───────────────────────── click redirect & pause ─────────────────────────


async def test_click_is_logged_with_hashed_ip_and_paused_links_redirect_untagged(
        ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount], clock: FrozenClock,
        product_factory) -> None:  # type: ignore[no-untyped-def]
    svc = LinkService(ctx)
    svc.register(ctx.bus)
    link = await _publish(ctx, svc, workspace, accounts[Platform.INSTAGRAM], product_factory(7101300001))

    target = await svc.resolve_click(link.hub_code.upper(), "203.0.113.7", "Mozilla/5.0 (iPhone)",
                                     "https://www.instagram.com/\x00x")
    assert target == link.short_url
    clicks = ctx.perf.clicks  # type: ignore[attr-defined]
    assert len(clicks) == 1
    row = clicks[0]
    assert row.ip_hash == hash_ip("203.0.113.7", b"test-salt") and row.ua_hash == hash_ip("Mozilla/5.0 (iPhone)",
                                                                                           b"test-salt")
    assert "203.0.113.7" not in repr(row) and row.sub_id == link.sub_id and row.referer == "https://www.instagram.com/x"

    # M9 flags the link → outbox → bus → M3 pauses it for 24 h
    async with ctx.uow() as repos:
        await ctx.emit(repos, Event.of(InvalidClickDetected(link_id=None, sub_id=link.sub_id,
                                                            reason="same_ip_burst", score=0.97),
                                       idempotency_key=f"{link.sub_id}:invalid:1"))
    assert await ctx.relay_outbox() >= 1
    async with ctx.uow() as repos:
        assert await repos.kv.get(pause_key(link.id), clock.now()) is not None
        assert len(await repos.audit.recent(action="link.paused")) == 1

    paused_target = await svc.resolve_click(link.hub_code, "203.0.113.7", "Mozilla/5.0 (iPhone)")
    assert paused_target == "https://www.coupang.com/vp/products/7101300001"  # no affiliate tag
    assert len(clicks) == 2  # still logged

    clock.advance(hours=24, seconds=1)
    assert await svc.resolve_click(link.hub_code, "198.51.100.1", "UA") == link.short_url


async def test_unknown_or_malformed_codes_are_not_found(ctx: AppContext) -> None:
    svc = LinkService(ctx)
    with pytest.raises(NotFound):
        await svc.resolve_click("abcdefgh", "1.1.1.1", "UA")
    with pytest.raises(NotFound):
        await svc.resolve_click("../../etc/passwd", "1.1.1.1", "UA")


async def test_tampered_row_never_redirects_through_the_affiliate_link(
        ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount], product_factory) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import update

    from acvm.db import tables as t

    svc = LinkService(ctx)
    link = await _publish(ctx, svc, workspace, accounts[Platform.THREADS], product_factory(7101300001))
    async with ctx.uow() as repos:
        await repos.session.execute(update(t.AffiliateLinkRow).where(t.AffiliateLinkRow.id == link.id)
                                    .values(short_url="https://link.coupang.com/a/attacker"))
    assert await svc.resolve_click(link.hub_code, "1.1.1.1", "UA") == "https://www.coupang.com/vp/products/7101300001"


# ───────────────────────── health sweep ─────────────────────────


def _score(pid: int, pos: float, at: Any) -> ProductScore:
    f = ProductFeatures(log_reviews=0.5, rating=0.5, rocket=1, price_affinity=1, r_cat=1, v_demo=0.5, m_trend=0.5,
                        epc_bayes=0.5, p_risk=0.06)
    return ProductScore(product_id=pid, scored_at=at, pos=pos, features=f, model_version="pos-v1")


async def test_health_sweep_flags_sold_out_and_suggests_best_same_category_replacement(
        ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount], clock: FrozenClock,
        product_factory) -> None:  # type: ignore[no-untyped-def]
    svc = LinkService(ctx)
    threads = accounts[Platform.THREADS]
    sold_out = product_factory(7101300015, name="다용도 계량컵 스푼 세트 8종")
    healthy = product_factory(7101300001)
    candidates = {
        7101300002: (product_factory(7101300002, name="진공 밀폐용기 6종"), 0.80),
        7101300009: (product_factory(7101300009, name="냉장고 정리 트레이"), 0.90),
        7101300099: (product_factory(7101300099, name="레드 와인 750ml", regulated=True), 0.97),  # regulated
        7101400001: (product_factory(7101400001, name="압축 리빙박스", category="1014"), 0.99),  # other category
    }
    l_bad = await _publish(ctx, svc, workspace, threads, sold_out)
    l_ok = await _publish(ctx, svc, workspace, threads, healthy)
    async with ctx.uow() as repos:
        for pid, (product, pos) in candidates.items():
            await repos.products.upsert(product)
            await repos.products.add_score(_score(pid, pos, clock.now() - timedelta(days=1)))
        await repos.products.add_score(_score(7101300009, 0.70, clock.now() - timedelta(days=2)))  # older score
        await repos.products.add_score(_score(7101300001, 0.95, clock.now()))  # has a link but it is healthy

    assert await svc.health_sweep() == []  # nothing is due within 6 h of creation

    clock.advance(hours=7)
    changes = await svc.health_sweep()
    assert changes == [LinkHealthChanged(link_id=l_bad.id, health="sold_out", replacement_product_id=7101300001)]
    async with ctx.uow() as repos:
        bad, ok = await repos.links.get(l_bad.id), await repos.links.get(l_ok.id)
        events = [e for e in await repos.outbox.pending(500) if e.type == LinkHealthChanged.TYPE]
    assert bad is not None and bad.health is LinkHealth.SOLD_OUT and bad.checked_at == clock.now()
    assert ok is not None and ok.health is LinkHealth.OK and ok.checked_at == clock.now()
    assert len(events) == 1 and events[0].workspace_id == workspace.id

    assert await svc.health_sweep() == []  # just checked

    clock.advance(hours=7)
    sandbox = coupang_client_for(ctx)
    assert isinstance(sandbox, SandboxCoupangClient)
    sandbox.set_availability(7101300015, LinkHealth.OK)
    sandbox.set_availability(7101300001, LinkHealth.DISCONTINUED)
    changes = await svc.health_sweep()
    assert sorted(changes, key=lambda c: c.health) == sorted([
        LinkHealthChanged(link_id=l_bad.id, health="ok", replacement_product_id=None),
        LinkHealthChanged(link_id=l_ok.id, health="discontinued", replacement_product_id=7101300009),
    ], key=lambda c: c.health)


class _BlockedClient:
    async def availability(self, product_id: int, name: str) -> LinkHealth:
        raise QuotaExhausted("search budget exhausted", retry_after_seconds=450.0)


async def test_health_sweep_stops_on_budget_and_leaves_links_due(
        ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount], clock: FrozenClock,
        product_factory) -> None:  # type: ignore[no-untyped-def]
    link = await _publish(ctx, LinkService(ctx), workspace, accounts[Platform.THREADS], product_factory(7101300015))
    clock.advance(hours=7)
    blocked = LinkService(ctx, client=_BlockedClient())  # type: ignore[arg-type]
    assert await blocked.health_sweep() == []
    async with ctx.uow() as repos:
        stored = await repos.links.get(link.id)
    assert stored is not None and stored.health is LinkHealth.OK
    assert stored.checked_at == clock.now() - timedelta(hours=7)  # still due for the next sweep
