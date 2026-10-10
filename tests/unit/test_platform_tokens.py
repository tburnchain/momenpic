"""Token material parsing, refresh policy (§10) and secret write-back."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.enums import Platform
from acvm.domain.models import ChannelAccount
from acvm.infra.platforms.facebook import FacebookReelsClient
from acvm.infra.platforms.sandbox import SandboxPlatformClient
from acvm.infra.platforms.threads import ThreadsClient
from acvm.infra.platforms.tokens import TokenBundle, TokenManager, TokenRefreshFailed, needs_refresh
from acvm.infra.platforms.youtube import GOOGLE_TOKEN_URL, YouTubeShortsClient


def test_bundle_parse_dump_and_repr_never_leaks() -> None:
    raw = TokenBundle.parse("  plain-token  ")
    assert raw.raw and raw.access_token == "plain-token" and raw.expires_at is None
    data = {"access_token": "AT-1", "refresh_token": "RT-1", "expires_at": "2026-10-10T00:00:00+00:00",
            "client_id": "cid", "client_secret": "cs", "token_type": "bearer", "page_id": "p1"}
    b = TokenBundle.parse(json.dumps(data))
    assert b.expires_at is not None and b.expires_at.isoformat() == "2026-10-10T00:00:00+00:00"
    assert b.extra == {"page_id": "p1"}
    assert json.loads(b.dumps()) == data
    shown = repr(b) + str(b)
    assert "AT-1" not in shown and "RT-1" not in shown and "cs" not in shown.replace("refreshable", "")
    with pytest.raises(ValueError):
        TokenBundle.parse('{"refresh_token": "x"}')


def test_refresh_windows(clock: FrozenClock) -> None:
    now = clock.now()
    meta = TokenBundle("t", expires_at=now + timedelta(days=6, hours=23))
    assert needs_refresh(Platform.THREADS, meta, now)
    assert not needs_refresh(Platform.THREADS, TokenBundle("t", expires_at=now + timedelta(days=8)), now)
    assert not needs_refresh(Platform.YOUTUBE, TokenBundle("t", expires_at=now + timedelta(minutes=30)), now)
    assert needs_refresh(Platform.YOUTUBE, TokenBundle("t", expires_at=now + timedelta(minutes=2)), now)
    assert not needs_refresh(Platform.FACEBOOK, TokenBundle("t"), now)  # page tokens without expiry


@respx.mock
async def test_threads_refresh_seven_days_before_expiry_writes_back(
        ctx: AppContext, accounts: dict[Platform, ChannelAccount], clock: FrozenClock) -> None:
    acc = accounts[Platform.THREADS]
    await ctx.secrets.put(acc.token_ref, json.dumps({
        "access_token": "old-long-lived", "expires_at": (clock.now() + timedelta(days=3)).isoformat()}))
    route = respx.get("https://graph.threads.net/refresh_access_token").mock(return_value=httpx.Response(
        200, json={"access_token": "new-long-lived", "token_type": "bearer", "expires_in": 5_184_000}))
    mgr = TokenManager(ctx, {Platform.THREADS: ThreadsClient(ctx.settings.platforms)})
    assert await mgr.access_token(acc) == "new-long-lived"
    params = route.calls.last.request.url.params
    assert params["grant_type"] == "th_refresh_token" and params["access_token"] == "old-long-lived"
    stored = json.loads(await ctx.secrets.get(acc.token_ref))
    assert stored["access_token"] == "new-long-lived"
    assert stored["expires_at"] == (clock.now() + timedelta(seconds=5_184_000)).isoformat()
    # fresh token: no second refresh
    assert await mgr.access_token(acc) == "new-long-lived" and route.call_count == 1


@respx.mock
async def test_meta_exchange_and_google_refresh(ctx: AppContext, accounts: dict[Platform, ChannelAccount],
                                                clock: FrozenClock) -> None:
    fb, yt = accounts[Platform.FACEBOOK], accounts[Platform.YOUTUBE]
    await ctx.secrets.put(fb.token_ref, json.dumps({
        "access_token": "fb-old", "client_id": "app", "client_secret": "app-secret",
        "expires_at": (clock.now() + timedelta(days=1)).isoformat()}))
    await ctx.secrets.put(yt.token_ref, json.dumps({
        "access_token": "ya29-old", "refresh_token": "1//rt", "client_id": "gid", "client_secret": "gsec",
        "expires_at": (clock.now() - timedelta(minutes=1)).isoformat()}))
    cfg = ctx.settings.platforms
    exchange = respx.get(f"{cfg.graph_base}/{cfg.graph_version}/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "fb-new", "expires_in": 5_183_999}))
    google = respx.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "ya29-new", "expires_in": 3599, "token_type": "Bearer"}))
    mgr = TokenManager(ctx, {Platform.FACEBOOK: FacebookReelsClient(cfg), Platform.YOUTUBE: YouTubeShortsClient(cfg)})
    assert await mgr.access_token(fb) == "fb-new"
    p = exchange.calls.last.request.url.params
    assert (p["grant_type"], p["client_id"], p["fb_exchange_token"]) == ("fb_exchange_token", "app", "fb-old")
    assert await mgr.access_token(yt) == "ya29-new"
    sent = dict(parse_qsl(google.calls.last.request.content.decode()))
    assert sent == {"grant_type": "refresh_token", "refresh_token": "1//rt", "client_id": "gid",
                    "client_secret": "gsec"}
    stored = json.loads(await ctx.secrets.get(yt.token_ref))
    assert stored["refresh_token"] == "1//rt" and stored["access_token"] == "ya29-new"


@respx.mock
async def test_refresh_failures(ctx: AppContext, accounts: dict[Platform, ChannelAccount],
                                clock: FrozenClock) -> None:
    th = accounts[Platform.THREADS]
    cfg = ctx.settings.platforms
    route = respx.get("https://graph.threads.net/refresh_access_token")
    mgr = TokenManager(ctx, {Platform.THREADS: ThreadsClient(cfg)})
    # still valid + transient failure → keep using the current token
    await ctx.secrets.put(th.token_ref, json.dumps({
        "access_token": "still-valid", "expires_at": (clock.now() + timedelta(days=2)).isoformat()}))
    route.mock(return_value=httpx.Response(503, json={"error": {"message": "down", "code": 2}}))
    assert await mgr.access_token(th) == "still-valid"
    # rejected refresh → typed failure (service turns it into cooldown + alert)
    route.mock(return_value=httpx.Response(400, json={"error": {"message": "Session has expired", "code": 190}}))
    with pytest.raises(TokenRefreshFailed) as info:
        await mgr.access_token(th)
    assert info.value.reason == "rejected" and "still-valid" not in str(info.value)
    # missing secret on a real client → failure; on a sandbox client → placeholder
    missing = accounts[Platform.INSTAGRAM]
    with pytest.raises(TokenRefreshFailed) as miss:
        await TokenManager(ctx, {}).access_token(missing)
    assert miss.value.reason == "missing"
    sandbox = SandboxPlatformClient(Platform.INSTAGRAM, clock, api_limit_24h=100)
    token = await TokenManager(ctx, {Platform.INSTAGRAM: sandbox}).access_token(missing)
    assert token.startswith("sandbox-token:")


async def test_expired_token_without_refresh_capability(ctx: AppContext, accounts: dict[Platform, ChannelAccount],
                                                        clock: FrozenClock) -> None:
    yt = accounts[Platform.YOUTUBE]
    await ctx.secrets.put(yt.token_ref, json.dumps({
        "access_token": "expired", "expires_at": (clock.now() - timedelta(hours=1)).isoformat()}))
    sandbox = SandboxPlatformClient(Platform.YOUTUBE, clock, api_limit_24h=100)
    with pytest.raises(TokenRefreshFailed) as info:  # Google needs a refresh_token
        await TokenManager(ctx, {Platform.YOUTUBE: sandbox}).access_token(yt)
    assert info.value.reason == "unrefreshable"
