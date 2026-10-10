"""Platform client registry: one ``PlatformClient`` per platform, chosen from settings.

* ``settings.platforms.dry_run`` (default True until gates G0/G1): deterministic sandbox clients — no network.
* otherwise: the official-API clients sharing one pooled ``httpx.AsyncClient``.
* Kakao always gets the manual one-click queue (no official publishing API, §7; no macros, §8.2).

Callers keep the dict in ``ctx.extras["platform_clients"]`` (``get_platform_clients`` does it for them) so
M8 publishing and M9 insights share sandbox state and HTTP connection pools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from acvm.core.enums import Platform
from acvm.infra.platforms.base import PlatformClient
from acvm.infra.platforms.facebook import FacebookReelsClient
from acvm.infra.platforms.instagram import InstagramReelsClient
from acvm.infra.platforms.kakao import ManualQueueClient
from acvm.infra.platforms.sandbox import SandboxPlatformClient
from acvm.infra.platforms.threads import ThreadsClient
from acvm.infra.platforms.tiktok import TikTokClient
from acvm.infra.platforms.youtube import YouTubeShortsClient

if TYPE_CHECKING:
    from acvm.context import AppContext

CLIENTS_KEY = "platform_clients"
HTTP_KEY = "platform_http"
API_PLATFORMS = (Platform.THREADS, Platform.INSTAGRAM, Platform.FACEBOOK, Platform.YOUTUBE, Platform.TIKTOK)


def build_platform_clients(ctx: AppContext) -> dict[Platform, PlatformClient]:
    """Build a fresh client per platform (sandbox when dry-run, official APIs otherwise)."""
    cfg = ctx.settings.platforms
    clients: dict[Platform, PlatformClient] = {Platform.KAKAO: ManualQueueClient()}
    if cfg.dry_run:
        for p in API_PLATFORMS:
            clients[p] = SandboxPlatformClient(p, ctx.clock, api_limit_24h=ctx.settings.quota.api_limit_24h[p.value],
                                               audited=cfg.tiktok_audited)
        return clients
    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                             limits=httpx.Limits(max_connections=50, max_keepalive_connections=10))
    ctx.extras[HTTP_KEY] = http
    clients[Platform.THREADS] = ThreadsClient(cfg, http=http)
    clients[Platform.INSTAGRAM] = InstagramReelsClient(cfg, http=http)
    clients[Platform.FACEBOOK] = FacebookReelsClient(cfg, http=http, timeout=httpx.Timeout(60.0, connect=10.0))
    clients[Platform.YOUTUBE] = YouTubeShortsClient(cfg, http=http)
    clients[Platform.TIKTOK] = TikTokClient(cfg, http=http)
    return clients


def get_platform_clients(ctx: AppContext) -> dict[Platform, PlatformClient]:
    """Process-wide clients cached in ``ctx.extras["platform_clients"]``."""
    clients: dict[Platform, PlatformClient] | None = ctx.extras.get(CLIENTS_KEY)
    if clients is None:
        clients = ctx.extras[CLIENTS_KEY] = build_platform_clients(ctx)
    return clients


async def close_platform_clients(ctx: AppContext) -> None:
    """Release the shared HTTP pool (call on shutdown)."""
    http = ctx.extras.pop(HTTP_KEY, None)
    ctx.extras.pop(CLIENTS_KEY, None)
    if isinstance(http, httpx.AsyncClient):
        await http.aclose()
