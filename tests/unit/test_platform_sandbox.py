"""Sandbox clients (dry run) and the registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acvm.config import Settings
from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.enums import Platform
from acvm.core.errors import UpstreamError
from acvm.infra.platforms.base import PlatformWarning, PostRequest, RemoteStatus
from acvm.infra.platforms.facebook import FacebookReelsClient
from acvm.infra.platforms.kakao import ManualQueueClient
from acvm.infra.platforms.registry import build_platform_clients, close_platform_clients, get_platform_clients
from acvm.infra.platforms.sandbox import SandboxPlatformClient, simulated_metrics
from acvm.infra.platforms.threads import ThreadsClient
from acvm.infra.platforms.tiktok import TikTokClient


def _req(**kw: object) -> PostRequest:
    base: dict[str, object] = {"account_platform_user_id": "u", "access_token": "t", "text": "광고 문구\n본문",
                               "idempotency_key": "c1:instagram:a1"}
    base.update(kw)
    return PostRequest(**base)  # type: ignore[arg-type]


def test_insights_are_deterministic_and_grow_with_age() -> None:
    t0 = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    post = f"sbx-threads-{int(t0.timestamp())}-abcdef1234"
    early = simulated_metrics(Platform.THREADS, post, t0 + timedelta(hours=2))
    assert early == simulated_metrics(Platform.THREADS, post, t0 + timedelta(hours=2))
    late = simulated_metrics(Platform.THREADS, post, t0 + timedelta(hours=72))
    assert late["views"] > early["views"] > 0
    assert simulated_metrics(Platform.THREADS, post, t0)["views"] == 0
    other = simulated_metrics(Platform.THREADS, f"sbx-threads-{int(t0.timestamp())}-zzzz999999", t0 + timedelta(72))
    assert other["views"] != late["views"]
    assert set(late) >= {"views", "likes", "shares", "link_clicks", "replies"}
    assert simulated_metrics(Platform.THREADS, "not-a-sandbox-id", t0) == {}


def test_link_clicks_follow_platform_factor() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    post = f"sbx-x-{int(t0.timestamp())}-same"
    now = t0 + timedelta(days=7)
    views = [simulated_metrics(p, post, now)["views"] for p in (Platform.THREADS, Platform.INSTAGRAM)]
    assert views[0] == views[1] > 0  # same post id → same draw
    th, ig, fb = (simulated_metrics(p, post, now)["link_clicks"]
                  for p in (Platform.THREADS, Platform.INSTAGRAM, Platform.FACEBOOK))
    assert th == pytest.approx(views[0] * 0.008, abs=1)
    assert fb == pytest.approx(views[0] * 0.008 * 0.8, abs=1)
    assert ig == pytest.approx(views[0] * 0.008 * 0.25, abs=1)


async def test_instagram_two_phase_limit_and_expiry(clock: FrozenClock) -> None:
    sb = SandboxPlatformClient(Platform.INSTAGRAM, clock, api_limit_24h=100)
    r = _req(video_url="https://media/x.mp4")
    created = await sb.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS
    with pytest.raises(UpstreamError):  # not ready yet, like the real API
        await sb.publish(r, created.container_id)
    assert (await sb.status(r, created.container_id)).status is RemoteStatus.FINISHED
    done = await sb.publish(r, created.container_id)
    assert done.status is RemoteStatus.PUBLISHED and done.post_id.startswith("sbx-instagram-")
    sb.external_usage = 4
    limit = await sb.publishing_limit("u", "t")
    assert limit is not None and (limit.used, limit.limit) == (5, 100)
    assert await sb.hide_or_delete(done.post_id, "t") is False  # mirrors the real Instagram API
    late = await sb.create(_req(video_url="https://media/y.mp4", idempotency_key="c2:instagram:a1"))
    clock.advance(hours=25)
    assert (await sb.status(r, late.container_id)).status is RemoteStatus.EXPIRED


async def test_single_phase_platforms_and_failure_injection(clock: FrozenClock) -> None:
    yt = SandboxPlatformClient(Platform.YOUTUBE, clock, api_limit_24h=100)
    r = _req(video_path="renders/x.mp4", title="[광고] t")
    created = await yt.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS
    assert (await yt.publish(r, created.container_id)).status is RemoteStatus.PUBLISHED
    assert await yt.publishing_limit("u", "t") is None and await yt.hide_or_delete("p", "t") is True
    th = SandboxPlatformClient(Platform.THREADS, clock, api_limit_24h=250)
    assert (await th.create(_req(idempotency_key="k"))).status is RemoteStatus.FINISHED  # text is ready at once
    th.fail_next, th.fail_status = 1, 400
    with pytest.raises(UpstreamError) as info:
        await th.create(_req(idempotency_key="k2"))
    assert info.value.retryable is False
    th.warn_next = True
    with pytest.raises(PlatformWarning):
        await th.create(_req(idempotency_key="k3"))
    tt = SandboxPlatformClient(Platform.TIKTOK, clock, api_limit_24h=15)
    raw = (await tt.create(_req(video_url="https://media/x.mp4"))).raw
    assert raw["privacy_level"] == "SELF_ONLY" and raw["unaudited"] is True and raw["is_aigc"] is True
    with pytest.raises(ValueError):
        SandboxPlatformClient(Platform.KAKAO, clock, api_limit_24h=1)


async def test_registry_dry_run_and_real(ctx: AppContext) -> None:
    clients = get_platform_clients(ctx)
    assert clients is ctx.extras["platform_clients"] is get_platform_clients(ctx)
    assert isinstance(clients[Platform.KAKAO], ManualQueueClient)
    assert all(isinstance(clients[p], SandboxPlatformClient) for p in Platform if p is not Platform.KAKAO)
    live_settings = Settings(env="test", database_url="sqlite+aiosqlite:///:memory:")
    live_settings.platforms.dry_run = False
    live_ctx = AppContext(settings=live_settings, db=ctx.db, bus=ctx.bus, clock=ctx.clock, secrets=ctx.secrets,
                          objects=ctx.objects, quota=ctx.quota, perf=ctx.perf, embedder=ctx.embedder)
    real = build_platform_clients(live_ctx)
    assert isinstance(real[Platform.THREADS], ThreadsClient)
    assert isinstance(real[Platform.FACEBOOK], FacebookReelsClient)
    assert isinstance(real[Platform.TIKTOK], TikTokClient)
    assert isinstance(real[Platform.KAKAO], ManualQueueClient)
    await close_platform_clients(live_ctx)
    assert "platform_http" not in live_ctx.extras
