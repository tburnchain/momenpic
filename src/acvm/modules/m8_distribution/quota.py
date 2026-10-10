"""Quota manager for distribution (§4 M8 "쿼터 관리자", §7, §7.1 #1–#2, §8.1 S-02).

Two independent limits must both allow a post:

1. **Local double enforcement (§7.1 #1)** — a rolling 24-hour window per ``account × platform`` in the shared
   ``QuotaStore`` (Redis Lua in production, atomic across workers). The limit is the *effective daily cap*
   ``min(account.daily_cap, operating_cap(api_limit_24h, design cap, max_api_fraction))`` — never more than 30 %
   of the platform API limit. The publication idempotency key is the window token, so a retried publish never
   consumes a second slot.
2. **Real-time platform check (§7.1 #2)** — Threads and Instagram expose their usage counter; it is read before
   every post and the post is deferred when ``used ≥ ⌊limit · max_api_fraction⌋`` (someone else — a human, a
   second app — is consuming the platform budget, which our local counter cannot see).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from acvm.config import QuotaPolicy
from acvm.core.enums import Platform
from acvm.domain.models import ChannelAccount, Publication
from acvm.infra.platforms.base import PlatformClient
from acvm.ports import QuotaDecision, QuotaStore
from acvm.quant.ratelimit import operating_cap

WINDOW_SECONDS = 86_400
REALTIME_LIMIT_PLATFORMS = frozenset({Platform.THREADS, Platform.INSTAGRAM})
PLATFORM_DEFER_SECONDS = 3600.0  # platform window is rolling and opaque: re-check in an hour


def effective_daily_cap(account: ChannelAccount, policy: QuotaPolicy) -> int:
    """§7: account cap, clipped to the design cap and to ``max_api_fraction`` of the API limit."""
    p = account.platform.value
    design = policy.daily_cap.get(p, 0)
    api = policy.api_limit_24h.get(p, 0)
    return max(0, min(account.daily_cap, operating_cap(api, design, policy.max_api_fraction)))


def window_key(account: ChannelAccount) -> str:
    return f"{account.id}:{account.platform.value}"


@dataclass(frozen=True, slots=True)
class PlatformQuotaCheck:
    allowed: bool
    used: int | None = None
    limit: int | None = None
    threshold: int | None = None
    retry_after_seconds: float = 0.0


class QuotaGate:
    """Local rolling-window acquisition + platform-reported usage check."""

    def __init__(self, store: QuotaStore, policy: QuotaPolicy) -> None:
        self.store = store
        self.policy = policy

    async def acquire(self, account: ChannelAccount, publication: Publication, now: datetime) -> QuotaDecision:
        cap = effective_daily_cap(account, self.policy)
        if cap <= 0:
            return QuotaDecision(False, 0, 0, float(WINDOW_SECONDS))
        return await self.store.acquire_window(window_key(account), cap, WINDOW_SECONDS, now,
                                               token=publication.idempotency_key)

    async def release(self, account: ChannelAccount, publication: Publication) -> None:
        """Give the slot back when the post never reached the platform (deferral, refusal, failure)."""
        await self.store.release_window(window_key(account), publication.idempotency_key)

    async def usage(self, account: ChannelAccount, now: datetime) -> int:
        return await self.store.window_usage(window_key(account), WINDOW_SECONDS, now)

    async def platform_check(self, client: PlatformClient, account: ChannelAccount, token: str) -> PlatformQuotaCheck:
        if account.platform not in REALTIME_LIMIT_PLATFORMS:
            return PlatformQuotaCheck(True)
        reported = await client.publishing_limit(account.platform_user_id, token)
        if reported is None:
            return PlatformQuotaCheck(True)
        threshold = math.floor(reported.limit * self.policy.max_api_fraction)
        allowed = reported.limit > 0 and reported.used < threshold
        return PlatformQuotaCheck(allowed, reported.used, reported.limit, threshold,
                                  0.0 if allowed else PLATFORM_DEFER_SECONDS)
