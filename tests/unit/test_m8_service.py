"""DistributionService flows against the sandbox platform clients and the in-memory test context."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise

import pytest

from acvm.context import AppContext
from acvm.core.clock import KST, FrozenClock
from acvm.core.enums import AccountStatus, ContentKind, ContentStatus, Platform
from acvm.core.enums import PublicationState as S
from acvm.core.errors import PolicyViolation, UpstreamError, ValidationFailed
from acvm.core.ids import publication_key, short_code, sub_id_for, uuid7
from acvm.domain.models import (
    AffiliateLink,
    ChannelAccount,
    Content,
    Product,
    Publication,
    PublishTarget,
    RenderMeta,
    Script,
    Workspace,
)
from acvm.infra.platforms.base import PlatformClient
from acvm.infra.platforms.registry import build_platform_clients
from acvm.infra.platforms.sandbox import SandboxPlatformClient
from acvm.modules.m8_distribution import DistributionService, effective_daily_cap
from acvm.modules.m8_distribution.captions import DEFAULT_PLACEMENT
from acvm.modules.m8_distribution.quota import window_key
from acvm.security.linksign import sign_link

SIGNING_KEY = b"test-link-signing-key"


# ───────────────────────────── helpers ─────────────────────────────


class World:
    def __init__(self, ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount],
                 product: Product) -> None:
        self.ctx, self.workspace, self.accounts, self.product = ctx, workspace, accounts, product
        self.clients: dict[Platform, PlatformClient] = build_platform_clients(ctx)
        self.svc = DistributionService(ctx, self.clients)

    def sandbox(self, platform: Platform) -> SandboxPlatformClient:
        client = self.clients[platform]
        assert isinstance(client, SandboxPlatformClient)
        return client

    async def content(self, *, kind: ContentKind = ContentKind.SHORT,
                      status: ContentStatus = ContentStatus.APPROVED) -> Content:
        cid = uuid7()
        render = None
        if kind is ContentKind.SHORT:
            uri = await self.ctx.objects.put_bytes(b"\x00\x00\x00\x18ftypmp42", f"renders/{cid}.mp4", "video/mp4")
            render = RenderMeta(media_uri=uri, duration_sec=29.0, width=1080, height=1920, fps=30)
        script = Script(hooks=["과자 봉지, 아직도 고무줄로 묶어요?"], hook_type="question",
                        body="집게 하나로 끝. 20개입이라 넉넉해요." if kind is ContentKind.TEXT else "",
                        caption="집게 하나로 끝. 20개입이라 넉넉해요." if kind is ContentKind.SHORT else "",
                        hashtags=["주방꿀템", "살림"], persona="30대 여성", template_id="t1", title="밀봉 클립 써보기")
        c = Content(id=cid, workspace_id=self.workspace.id, product_id=self.product.id, kind=kind, persona="30대 여성",
                    template_id="t1", script=script, status=status, render=render, created_at=self.ctx.clock.now())
        async with self.ctx.uow() as r:
            await r.contents.add(c)
        return c

    async def link(self, content: Content, account: ChannelAccount, *, signature: str | None = None) -> AffiliateLink:
        key = publication_key(content.id, account.platform.value, account.id)
        sub = sub_id_for(key)
        deeplink = f"https://link.coupang.com/re/AFFSDP?lptag=AF0000000&subid={sub}&pageKey={self.product.id}"
        short = f"https://link.coupang.com/a/{short_code(key)}"
        sig = signature or sign_link(SIGNING_KEY, sub, deeplink, short, self.ctx.settings.coupang.partner_id)
        link = AffiliateLink(product_id=self.product.id, sub_id=sub, deeplink=deeplink, short_url=short,
                             hub_code=short_code(key + ":hub"), signature=sig, publication_key=key,
                             created_at=self.ctx.clock.now())
        async with self.ctx.uow() as r:
            return await r.links.add(link)

    @staticmethod
    def target(account: ChannelAccount, placement: str | None = None) -> PublishTarget:
        return PublishTarget(platform=account.platform, account_id=account.id, account_handle=account.handle,
                             link_placement=placement or DEFAULT_PLACEMENT[account.platform],  # type: ignore[arg-type]
                             format="video")

    async def planned(self, platform: Platform, *, kind: ContentKind = ContentKind.SHORT,
                      status: ContentStatus = ContentStatus.APPROVED, not_before: datetime | None = None,
                      placement: str | None = None) -> Publication:
        acc = self.accounts[platform]
        c = await self.content(kind=kind, status=status)
        link = await self.link(c, acc)
        [pub] = await self.svc.plan(c, [self.target(acc, placement)], {platform: link},
                                    not_before=not_before or self.ctx.clock.now())
        return pub

    async def get(self, pub: Publication) -> Publication:
        async with self.ctx.uow() as r:
            got = await r.publications.get(pub.id)
        assert got is not None
        return got

    async def account(self, platform: Platform) -> ChannelAccount:
        async with self.ctx.uow() as r:
            acc = await r.accounts.get(self.accounts[platform].id)
        assert acc is not None
        return acc

    async def events(self, event_type: str) -> list[dict[str, object]]:
        await self.ctx.relay_outbox()
        return [e.payload for e in self.ctx.bus.of_type(event_type)]  # type: ignore[attr-defined]


@pytest.fixture
async def world(ctx: AppContext, workspace: Workspace, accounts: dict[Platform, ChannelAccount],
                product_factory) -> World:  # type: ignore[no-untyped-def]
    product = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(product)
    return World(ctx, workspace, accounts, product)


def _kst(dt: datetime) -> datetime:
    return dt.astimezone(KST)


# ───────────────────────────── plan & scheduling ─────────────────────────────


async def test_plan_creates_idempotent_queued_publications(world: World) -> None:
    th, ig = world.accounts[Platform.THREADS], world.accounts[Platform.INSTAGRAM]
    c = await world.content()
    links = {Platform.THREADS: await world.link(c, th), Platform.INSTAGRAM: await world.link(c, ig)}
    targets = [world.target(th), world.target(ig)]
    now = world.ctx.clock.now()
    pubs = await world.svc.plan(c, targets, links, not_before=now)
    assert [p.state for p in pubs] == [S.QUEUED, S.QUEUED]
    assert {p.idempotency_key for p in pubs} == {publication_key(c.id, "threads", th.id),
                                                 publication_key(c.id, "instagram", ig.id)}
    disclosure = world.ctx.settings.gate.standard_disclosure
    assert all(p.caption.startswith(disclosure) for p in pubs)
    assert pubs[0].caption != pubs[1].caption
    assert all(p.scheduled_at >= now for p in pubs)
    assert abs((pubs[0].scheduled_at - pubs[1].scheduled_at).total_seconds()) >= 20 * 60  # §7.1 #4
    again = await world.svc.plan(c, targets, links, not_before=now + timedelta(hours=5))
    assert [p.id for p in again] == [p.id for p in pubs]
    assert [p.scheduled_at for p in again] == [p.scheduled_at for p in pubs]
    queued = [e for e in await world.events("publication.state_changed") if e["state"] == "QUEUED"]
    assert len(queued) == 2


async def test_plan_requires_the_publications_own_link(world: World) -> None:
    th = world.accounts[Platform.THREADS]
    c, other = await world.content(kind=ContentKind.TEXT), await world.content(kind=ContentKind.TEXT)
    wrong = await world.link(other, th)
    with pytest.raises(ValidationFailed):
        await world.svc.plan(c, [world.target(th)], {Platform.THREADS: wrong}, not_before=world.ctx.clock.now())
    with pytest.raises(ValidationFailed):
        await world.svc.plan(c, [world.target(th)], {}, not_before=world.ctx.clock.now())
    ig = world.accounts[Platform.INSTAGRAM]  # text content cannot go to a video-only platform
    with pytest.raises(ValidationFailed):
        await world.svc.plan(c, [world.target(ig)], {Platform.INSTAGRAM: await world.link(c, ig)},
                             not_before=world.ctx.clock.now())


async def test_plan_never_targets_two_accounts_of_one_platform(world: World) -> None:
    th = world.accounts[Platform.THREADS]
    second = ChannelAccount(workspace_id=world.workspace.id, platform=Platform.THREADS, handle="kitchen_pick_2",
                            token_ref="tokens/threads/k2", daily_cap=20, niche="kitchen",
                            created_at=world.ctx.clock.now() - timedelta(days=40))
    async with world.ctx.uow() as r:
        await r.accounts.add(second)
    c = await world.content(kind=ContentKind.TEXT)
    with pytest.raises(PolicyViolation):
        await world.svc.plan(c, [world.target(th), world.target(second)], {Platform.THREADS: await world.link(c, th)},
                             not_before=world.ctx.clock.now())
    await world.svc.plan(c, [world.target(th)], {Platform.THREADS: await world.link(c, th)},
                         not_before=world.ctx.clock.now())
    with pytest.raises(PolicyViolation):
        await world.svc.plan(c, [world.target(second)], {Platform.THREADS: await world.link(c, second)},
                             not_before=world.ctx.clock.now())


async def test_schedule_respects_cap_window_gap_and_rolls_over_days(world: World) -> None:
    ig = world.accounts[Platform.INSTAGRAM]
    cap = effective_daily_cap(ig, world.ctx.settings.quota)
    assert cap == 5
    pubs = [await world.planned(Platform.INSTAGRAM) for _ in range(cap + 3)]
    times = sorted(p.scheduled_at for p in pubs)
    per_day = Counter(_kst(t).date() for t in times)
    assert max(per_day.values()) <= cap and len(per_day) == 2
    assert per_day[_kst(world.ctx.clock.now()).date()] == cap
    for t in times:
        local = _kst(t)
        assert 9 <= local.hour < 23
        assert 3 <= local.minute <= 57  # top-of-hour band avoided
    gaps = [(b - a).total_seconds() for a, b in pairwise(times)]
    assert min(gaps) >= 20 * 60
    assert min(gaps) >= 60 * 60  # 5 posts spread across a 14-hour window, not clumped
    late = world.ctx.clock.now().astimezone(KST).replace(hour=22, minute=55)
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT, not_before=late)
    assert _kst(pub.scheduled_at).date() > late.date() and _kst(pub.scheduled_at).hour >= 9


def test_effective_cap_never_exceeds_api_fraction(world: World) -> None:
    policy = world.ctx.settings.quota
    tiktok = world.accounts[Platform.TIKTOK]
    assert effective_daily_cap(tiktok, policy) == min(3, int(15 * 0.30))
    small = world.accounts[Platform.THREADS].model_copy(update={"daily_cap": 2})
    assert effective_daily_cap(small, policy) == 2


# ───────────────────────────── publish ─────────────────────────────


async def test_publish_refuses_content_that_is_not_approved(world: World) -> None:
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT, status=ContentStatus.GATED)
    with pytest.raises(PolicyViolation):
        await world.svc.publish(pub.id)
    assert (await world.get(pub)).state is S.QUEUED
    assert world.sandbox(Platform.THREADS).count("create") == 0


async def test_threads_text_publish_is_idempotent(world: World) -> None:
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    done = await world.svc.publish(pub.id)
    assert done.state is S.PUBLISHED and done.platform_post_id.startswith("sbx-threads-")
    assert done.published_at == world.ctx.clock.now()
    again = await world.svc.publish(pub.id)
    assert again.state is S.PUBLISHED and again.platform_post_id == done.platform_post_id
    assert world.sandbox(Platform.THREADS).count("create") == 1
    states = [e["state"] for e in await world.events("publication.state_changed")
              if e["publication_id"] == str(pub.id)]
    assert states == ["QUEUED", "UPLOADING", "PROCESSING", "PUBLISHED"]
    async with world.ctx.uow() as r:
        kinds = [e.kind for e in await r.accounts.health_events(pub.channel_account_id,
                                                                world.ctx.clock.now() - timedelta(days=1))]
    assert kinds == ["attempt_ok"]


async def test_instagram_two_phase_publish_then_poll(world: World) -> None:
    pub = await world.planned(Platform.INSTAGRAM)
    processing = await world.svc.publish(pub.id)
    assert processing.state is S.PROCESSING and processing.platform_container_id
    done = await world.svc.poll(pub.id)
    assert done.state is S.PUBLISHED and done.platform_post_id.startswith("sbx-instagram-")
    assert (await world.svc.publish(pub.id)).state is S.PUBLISHED


async def test_instagram_container_expires_after_24h(world: World, clock: FrozenClock) -> None:
    ig = world.accounts[Platform.INSTAGRAM]
    pub = await world.planned(Platform.INSTAGRAM)
    assert (await world.svc.publish(pub.id)).state is S.PROCESSING
    assert await world.ctx.quota.window_usage(window_key(ig), 86_400, clock.now()) == 1
    clock.advance(hours=25)
    expired = await world.svc.poll(pub.id)
    assert expired.state is S.EXPIRED and expired.error and expired.error["code"] == "container_expired"
    assert await world.ctx.quota.window_usage(window_key(ig), 86_400, clock.now()) == 0
    assert world.sandbox(Platform.INSTAGRAM).count("publish") == 0


async def test_youtube_and_tiktok_single_phase_flow(world: World) -> None:
    yt = await world.planned(Platform.YOUTUBE)
    assert (await world.svc.publish(yt.id)).state is S.PROCESSING
    assert (await world.svc.poll(yt.id)).state is S.PUBLISHED
    tt = await world.planned(Platform.TIKTOK)
    assert (await world.svc.publish(tt.id)).state is S.PROCESSING
    assert (await world.svc.poll(tt.id)).state is S.PUBLISHED
    async with world.ctx.uow() as r:
        submitted = await r.audit.recent(action="publication_submitted")
    tiktok = [a for a in submitted if a.payload["platform"] == "tiktok"]
    assert tiktok and tiktok[0].payload["remote"]["unaudited"] is True  # §7 audit gate surfaced
    assert tiktok[0].payload["remote"]["privacy_level"] == "SELF_ONLY"


async def test_local_quota_defers_the_publication_beyond_the_cap(world: World) -> None:
    pubs = [await world.planned(Platform.INSTAGRAM) for _ in range(6)]
    results = [await world.svc.publish(p.id) for p in pubs]
    assert [r.state for r in results[:5]] == [S.PROCESSING] * 5
    deferred = results[5]
    assert deferred.state is S.QUEUED and deferred.error and deferred.error["deferred"] == "quota_local"
    assert deferred.scheduled_at > world.ctx.clock.now()
    assert 9 <= _kst(deferred.scheduled_at).hour < 23
    assert world.sandbox(Platform.INSTAGRAM).count("create") == 5


async def test_platform_reported_usage_defers_and_releases_local_slot(world: World) -> None:
    th = world.accounts[Platform.THREADS]
    world.sandbox(Platform.THREADS).external_usage = 75  # ⌊250 · 0.30⌋
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    out = await world.svc.publish(pub.id)
    assert out.state is S.QUEUED and out.error and out.error["deferred"] == "quota_platform"
    assert out.error["used"] == 75 and out.error["threshold"] == 75
    assert await world.ctx.quota.window_usage(window_key(th), 86_400, world.ctx.clock.now()) == 0
    assert world.sandbox(Platform.THREADS).count("create") == 0
    world.sandbox(Platform.THREADS).external_usage = 74
    assert (await world.svc.publish(pub.id)).state is S.PUBLISHED


async def test_warning_cools_down_account_and_cancels_its_schedule(world: World, clock: FrozenClock) -> None:
    pubs = [await world.planned(Platform.THREADS, kind=ContentKind.TEXT) for _ in range(3)]
    other = await world.planned(Platform.FACEBOOK)
    world.sandbox(Platform.THREADS).warn_next = True
    isolated = await world.svc.publish(pubs[0].id)
    assert isolated.state is S.FAILED and isolated.error and isolated.error["isolated"] is True
    assert [(await world.get(p)).state for p in pubs[1:]] == [S.CANCELLED, S.CANCELLED]
    assert (await world.get(other)).state is S.QUEUED  # other accounts untouched
    acc = await world.account(Platform.THREADS)
    assert acc.status is AccountStatus.COOLDOWN and acc.cooldown_until == clock.now() + timedelta(hours=48)
    changes = await world.events("account.status_changed")
    assert changes and changes[-1]["status"] == "cooldown" and changes[-1]["account_id"] == str(acc.id)
    async with world.ctx.uow() as r:
        kinds = {e.kind for e in await r.accounts.health_events(acc.id, clock.now() - timedelta(days=1))}
    assert "warning" in kinds


async def test_cooldown_reschedules_then_lifts(world: World, clock: FrozenClock) -> None:
    th = world.accounts[Platform.THREADS]
    until = clock.now() + timedelta(hours=2)
    async with world.ctx.uow() as r:
        await r.accounts.set_status(th.id, AccountStatus.COOLDOWN, until)
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    assert pub.scheduled_at >= until  # planning already respects the cooldown
    out = await world.svc.publish(pub.id)
    assert out.state is S.QUEUED and out.scheduled_at >= until and out.error["deferred"] == "account_cooldown"
    assert world.sandbox(Platform.THREADS).count("create") == 0
    assert await world.svc.evaluate_account(th.id) is AccountStatus.COOLDOWN
    clock.advance(hours=3)
    assert await world.svc.evaluate_account(th.id) is AccountStatus.ACTIVE
    assert (await world.events("account.status_changed"))[-1]["status"] == "active"
    assert (await world.svc.publish(pub.id)).state is S.PUBLISHED


async def test_retryable_errors_propagate_and_4xx_fail(world: World) -> None:
    sb = world.sandbox(Platform.THREADS)
    first = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    sb.fail_next, sb.fail_status = 1, 503
    with pytest.raises(UpstreamError) as info:
        await world.svc.publish(first.id)
    assert info.value.retryable
    assert (await world.get(first)).state is S.UPLOADING
    assert (await world.svc.publish(first.id)).state is S.PUBLISHED  # Temporal retry converges
    second = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    sb.fail_next, sb.fail_status = 1, 400
    failed = await world.svc.publish(second.id)
    assert failed.state is S.FAILED and failed.error and failed.error["status"] == 400
    assert failed.error["retryable"] is False


async def test_circuit_breaker_opens_on_4xx_burst_and_preserves_queue(world: World) -> None:
    svc = DistributionService(world.ctx, world.clients, breaker_threshold=2)
    sb = world.sandbox(Platform.FACEBOOK)
    async with world.ctx.uow() as r:  # healthy history: two failures must open the adapter, not cool the account
        for i in range(20):
            await r.accounts.record_health(world.accounts[Platform.FACEBOOK].id, "attempt_ok",
                                           world.ctx.clock.now() - timedelta(hours=i + 1))
    pubs = [await world.planned(Platform.FACEBOOK) for _ in range(3)]
    sb.fail_next, sb.fail_status = 2, 400
    assert [(await svc.publish(p.id)).state for p in pubs[:2]] == [S.FAILED, S.FAILED]
    held = await svc.publish(pubs[2].id)
    assert held.state is S.QUEUED and held.error and held.error["deferred"] == "circuit_open"
    assert sb.count("create") == 2


async def test_token_refresh_failure_puts_account_in_cooldown(world: World, clock: FrozenClock) -> None:
    th = world.accounts[Platform.THREADS]
    await world.ctx.secrets.put(th.token_ref, json.dumps({
        "access_token": "expired", "expires_at": (clock.now() - timedelta(days=1)).isoformat()}))
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    out = await world.svc.publish(pub.id)
    assert out.state is S.QUEUED and out.error and out.error["deferred"] == "token_refresh_failed"
    acc = await world.account(Platform.THREADS)
    assert acc.status is AccountStatus.COOLDOWN
    assert (await world.events("account.status_changed"))[-1]["status"] == "cooldown"
    async with world.ctx.uow() as r:
        assert await r.audit.recent(action="token_refresh_failed")


async def test_link_integrity_violation_never_publishes(world: World) -> None:
    th = world.accounts[Platform.THREADS]
    c = await world.content(kind=ContentKind.TEXT)
    link = await world.link(c, th, signature="0" * 64)
    [pub] = await world.svc.plan(c, [world.target(th)], {Platform.THREADS: link}, not_before=world.ctx.clock.now())
    out = await world.svc.publish(pub.id)
    assert out.state is S.FAILED and out.error and out.error["code"] == "integrity_violation"
    assert world.sandbox(Platform.THREADS).count("create") == 0


# ───────────────────────────── saga, manual queue, due ─────────────────────────────


async def test_take_down_deletes_or_flags_manual(world: World) -> None:
    th = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    await world.svc.publish(th.id)
    down = await world.svc.take_down(th.id, "link health failed")
    assert down.state is S.TAKEN_DOWN
    assert (await world.svc.take_down(th.id, "again")).state is S.TAKEN_DOWN
    ig = await world.planned(Platform.INSTAGRAM)
    await world.svc.publish(ig.id)
    await world.svc.poll(ig.id)
    kept = await world.svc.take_down(ig.id, "link health failed")
    assert kept.state is S.PUBLISHED and kept.error and kept.error["manual_takedown_required"] is True
    async with world.ctx.uow() as r:
        assert await r.audit.recent(action="manual_takedown_required")
    queued = await world.planned(Platform.FACEBOOK)
    assert (await world.svc.take_down(queued.id, "saga")).state is S.CANCELLED


async def test_kakao_manual_queue_flow(world: World) -> None:
    pub = await world.planned(Platform.KAKAO, kind=ContentKind.SHORT)
    pending = await world.svc.publish(pub.id)
    assert pending.state is S.MANUAL_PENDING and pending.platform_container_id == f"manual:{pub.idempotency_key}"
    [item] = await world.svc.manual_queue(world.workspace.id)
    assert item["publication_id"] == str(pub.id)
    assert str(item["caption"]).startswith(world.ctx.settings.gate.standard_disclosure)
    assert item["link_url"] and str(item["link_url"]) in str(item["caption"])
    assert item["media_url"] and "sig=" in str(item["media_url"])
    with pytest.raises(ValidationFailed):
        await world.svc.complete_manual(pub.id, "not-a-url", by="operator@tburn")
    done = await world.svc.complete_manual(pub.id, "https://pf.kakao.com/_x/123", by="operator@tburn")
    assert done.state is S.PUBLISHED and done.platform_post_id == "https://pf.kakao.com/_x/123"
    assert await world.svc.manual_queue(world.workspace.id) == []
    assert (await world.svc.complete_manual(pub.id, "https://pf.kakao.com/_x/123", by="x")).state is S.PUBLISHED


async def test_due_lists_queued_items_whose_time_has_come(world: World, clock: FrozenClock) -> None:
    pub = await world.planned(Platform.THREADS, kind=ContentKind.TEXT)
    clock.set(pub.scheduled_at - timedelta(seconds=1))
    assert pub.id not in [p.id for p in await world.svc.due()]
    clock.set(pub.scheduled_at + timedelta(seconds=1))
    assert [p.id for p in await world.svc.due()] == [pub.id]
