"""Deterministic simulated platform clients — the default while ``settings.platforms.dry_run`` is True.

They implement the same ``PlatformClient`` contract as the real adapters and mirror each platform's real
behaviour so that M8/M9 run unchanged in dry-run mode — but they never perform network I/O:

* two-phase containers for Threads video and Instagram (IN_PROGRESS on create → FINISHED on the next status
  call, EXPIRED after 24 h, §4 M8); Threads text containers are ready at once;
* single-phase async processing for Facebook / YouTube / TikTok (PUBLISHED on the next status call);
* ``publishing_limit`` for Threads/Instagram reflects a rolling 24-h counter of sandbox publishes plus
  ``external_usage`` (posts made outside ACVM), against the §7 API limit;
* takedown support mirrors the real APIs (Threads/Facebook/YouTube can delete; Instagram/TikTok cannot);
* TikTok follows the audit gate (``SELF_ONLY`` + ``raw["unaudited"]`` until audited).

Insights are a pure function of ``(post_id, now)``: final views ~ LogNormal(median 600, σ 1.6) drawn from a
generator seeded by the post id (§2.2 heavy-tail model), approached as ``1 − e^(−age_h/18)`` with post age in
hours; link clicks ≈ views · 0.8 % · platform factor (Threads 1.0, Facebook 0.8, hub platforms 0.25, §7.2).

Failure injection for tests: ``fail_next`` (next N mutating calls raise ``UpstreamError`` with
``fail_status`` — 5xx/429 retryable, other 4xx not) and ``warn_next`` (next mutating call raises a
``PlatformWarning``).
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from acvm.core.clock import Clock
from acvm.core.enums import Platform
from acvm.core.errors import ValidationFailed
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus
from acvm.infra.platforms.tokens import PlatformPolicyWarning, TokenBundle, upstream_error
from acvm.quant.distributions import LogNormal

VIEWS_MEDIAN = 600.0
VIEWS_SIGMA = 1.6
GROWTH_TAU_HOURS = 18.0
BASE_CTR = 0.008  # §2.5
LINK_FACTOR = {Platform.THREADS: 1.0, Platform.FACEBOOK: 0.8, Platform.INSTAGRAM: 0.25,
               Platform.TIKTOK: 0.25, Platform.YOUTUBE: 0.25}
TWO_PHASE = frozenset({Platform.THREADS, Platform.INSTAGRAM})
REALTIME_LIMIT = frozenset({Platform.THREADS, Platform.INSTAGRAM})
DELETABLE = frozenset({Platform.THREADS, Platform.FACEBOOK, Platform.YOUTUBE})
VIDEO_ONLY = frozenset({Platform.INSTAGRAM, Platform.FACEBOOK, Platform.YOUTUBE, Platform.TIKTOK})
TEXT_LIMIT = {Platform.THREADS: 500, Platform.INSTAGRAM: 2200, Platform.TIKTOK: 2200,
              Platform.FACEBOOK: 5000, Platform.YOUTUBE: 5000}
CONTAINER_TTL = timedelta(hours=24)
META_TOKEN_TTL = timedelta(days=60)
SHORT_TOKEN_TTL = {Platform.YOUTUBE: timedelta(hours=1), Platform.TIKTOK: timedelta(hours=24)}


def _h(*parts: object, n: int = 10) -> str:
    return hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).hexdigest()[:n]


@dataclass
class _Container:
    created_at: datetime
    media_type: str
    status: RemoteStatus
    polls: int = 0
    post_id: str = ""


class SandboxPlatformClient:
    """Simulated official-API client for one platform (no network I/O)."""

    is_sandbox = True

    def __init__(self, platform: Platform, clock: Clock, *, api_limit_24h: int, audited: bool = False,
                 processing_polls: int = 0) -> None:
        if platform is Platform.KAKAO:
            raise ValueError("Kakao has no publishing API; use ManualQueueClient (§7)")
        self.platform: Platform = platform
        self.clock = clock
        self.api_limit_24h = api_limit_24h
        self.audited = audited
        self.processing_polls = processing_polls  # status calls that still report IN_PROGRESS
        self.fail_next = 0
        self.fail_status = 503
        self.warn_next = False
        self.external_usage = 0
        self.calls: list[str] = []
        self.containers: dict[str, _Container] = {}
        self.published: dict[str, datetime] = {}
        self.deleted: set[str] = set()

    # ── failure injection ──
    def _inject(self, op: str) -> None:
        self.calls.append(op)
        if self.warn_next:
            self.warn_next = False
            raise PlatformPolicyWarning("sandbox: action blocked as spam (simulated)", platform=self.platform,
                                        code="sandbox_warning", status=400)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise upstream_error(self.platform, f"sandbox: simulated HTTP {self.fail_status} on {op}",
                                 status=self.fail_status)

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c == op)

    # ── contract ──
    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        self.calls.append("publishing_limit")
        if self.platform not in REALTIME_LIMIT:
            return None
        cut = self.clock.now() - timedelta(hours=24)
        used = sum(1 for at in self.published.values() if at > cut) + self.external_usage
        return PublishingLimit(used=used, limit=self.api_limit_24h)

    async def create(self, req: PostRequest) -> PostResult:
        self._inject("create")
        limit = TEXT_LIMIT[self.platform]
        length = len(req.text.encode("utf-16-le")) // 2 if self.platform is Platform.TIKTOK else len(req.text)
        if length > limit:
            raise ValidationFailed(f"{self.platform.value} text exceeds {limit} characters")
        if self.platform in VIDEO_ONLY and not (req.video_url or req.video_path):
            raise ValidationFailed(f"{self.platform.value} needs a video")
        if self.platform is Platform.YOUTUBE and not req.title:
            raise ValidationFailed("YouTube needs a title")
        media_type = "VIDEO" if (req.video_url or req.video_path) else "TEXT"
        container_id = f"sbx-{self.platform.value}-c-{_h(req.idempotency_key or req.text, media_type)}"
        ready = self.platform is Platform.THREADS and media_type == "TEXT"
        status = RemoteStatus.FINISHED if ready else RemoteStatus.IN_PROGRESS
        existing = self.containers.get(container_id)
        if existing is None or existing.status in (RemoteStatus.ERROR, RemoteStatus.EXPIRED):
            self.containers[container_id] = _Container(self.clock.now(), media_type, status)
        raw: dict[str, object] = {"sandbox": True, "media_type": media_type}
        if self.platform is Platform.TIKTOK:
            raw.update(privacy_level="SELF_ONLY" if not self.audited else "PUBLIC_TO_EVERYONE",
                       unaudited=not self.audited, is_aigc=req.ai_generated,
                       brand_content_toggle=req.commercial_disclosure and self.audited)
        return PostResult(self.containers[container_id].status, container_id=container_id, raw=raw)

    def _container(self, container_id: str) -> _Container:
        c = self.containers.get(container_id)
        if c is None:  # created by an earlier process: processing has long finished
            c = self.containers[container_id] = _Container(self.clock.now(), "VIDEO", RemoteStatus.FINISHED)
        return c

    def _mark_published(self, container_id: str, c: _Container) -> PostResult:
        if not c.post_id:
            now = self.clock.now()
            c.post_id = f"sbx-{self.platform.value}-{int(now.timestamp())}-{_h(container_id)}"
            c.status = RemoteStatus.PUBLISHED
            self.published[c.post_id] = now
        raw: dict[str, object] = {"sandbox": True}
        if self.platform is Platform.TIKTOK:
            raw["unaudited"] = not self.audited
        return PostResult(RemoteStatus.PUBLISHED, post_id=c.post_id, container_id=container_id,
                          permalink=f"https://sandbox.invalid/{self.platform.value}/{c.post_id}", raw=raw)

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        self.calls.append("status")
        c = self._container(container_id)
        now = self.clock.now()
        if c.status is RemoteStatus.PUBLISHED:
            return PostResult(RemoteStatus.PUBLISHED, post_id=c.post_id, container_id=container_id)
        if self.platform in TWO_PHASE and now - c.created_at > CONTAINER_TTL:
            c.status = RemoteStatus.EXPIRED
            return PostResult(RemoteStatus.EXPIRED, container_id=container_id, raw={"sandbox": True})
        if c.status in (RemoteStatus.ERROR, RemoteStatus.EXPIRED):
            return PostResult(c.status, container_id=container_id)
        if c.polls < self.processing_polls:
            c.polls += 1
            return PostResult(RemoteStatus.IN_PROGRESS, container_id=container_id)
        if self.platform in TWO_PHASE:
            c.status = RemoteStatus.FINISHED
            return PostResult(RemoteStatus.FINISHED, container_id=container_id)
        return self._mark_published(container_id, c)

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        if self.platform not in TWO_PHASE:
            return await self.status(req, container_id)
        self._inject("publish")
        c = self._container(container_id)
        if c.status is RemoteStatus.PUBLISHED:
            return PostResult(RemoteStatus.PUBLISHED, post_id=c.post_id, container_id=container_id)
        if c.status is RemoteStatus.EXPIRED or self.clock.now() - c.created_at > CONTAINER_TTL:
            c.status = RemoteStatus.EXPIRED
            raise upstream_error(self.platform, "sandbox: container expired", status=400, retryable=False)
        if c.status is not RemoteStatus.FINISHED:
            raise upstream_error(self.platform, "sandbox: media not ready", status=400, retryable=True)
        result = self._mark_published(container_id, c)
        if self.platform is Platform.THREADS and req.link_in_first_comment and req.link_url:
            result.raw["first_comment_id"] = f"{result.post_id}-reply"
        return result

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        self.calls.append("hide_or_delete")
        if self.platform not in DELETABLE:
            return False  # mirrors the real API: Instagram / TikTok cannot delete via API
        self.deleted.add(post_id)
        return True

    async def insights(self, post_id: str, token: str) -> Insights:
        self.calls.append("insights")
        return Insights(simulated_metrics(self.platform, post_id, self.clock.now()), raw={"sandbox": True})

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        """Mirror refresh rules: Meta tokens cannot be refreshed once expired; Google/TikTok need a refresh token."""
        self.calls.append("refresh_token")
        if self.platform in (Platform.THREADS, Platform.INSTAGRAM, Platform.FACEBOOK):
            if bundle.expires_at is not None and bundle.expires_at <= now:
                raise upstream_error(self.platform, "sandbox: expired long-lived token cannot be refreshed",
                                     status=400, retryable=False)
            ttl = META_TOKEN_TTL
        else:
            if not bundle.refresh_token:
                raise ValidationFailed("sandbox: refresh_token required")
            ttl = SHORT_TOKEN_TTL[self.platform]
        token = f"sbx-{self.platform.value}-tok-{_h(bundle.access_token, now.isoformat(), n=16)}"
        return bundle.renewed(token, now=now, expires_in=ttl.total_seconds())


def simulated_metrics(platform: Platform, post_id: str, now: datetime) -> dict[str, float]:
    """Deterministic cumulative counters for a sandbox post (see module docstring)."""
    parts = post_id.split("-")
    if len(parts) < 4 or parts[0] != "sbx" or not parts[2].isdigit():
        return {}
    published_at = datetime.fromtimestamp(int(parts[2]), tz=UTC)
    age_h = max(0.0, (now - published_at).total_seconds() / 3600.0)
    rng = random.Random(int(hashlib.blake2b(post_id.encode(), digest_size=8).hexdigest(), 16))
    final = LogNormal.from_median(VIEWS_MEDIAN, VIEWS_SIGMA).quantile(min(max(rng.random(), 1e-9), 1 - 1e-9))
    views = math.floor(final * (1.0 - math.exp(-age_h / GROWTH_TAU_HOURS)))
    like_rate, share_rate, reply_rate = 0.03 + 0.03 * rng.random(), 0.004 + 0.006 * rng.random(), 0.006 * rng.random()
    metrics: dict[str, float] = {
        "views": float(views),
        "likes": float(math.floor(views * like_rate)),
        "shares": float(math.floor(views * share_rate)),
        "link_clicks": float(math.floor(views * BASE_CTR * LINK_FACTOR.get(platform, 0.25))),
    }
    replies = float(math.floor(views * reply_rate))
    metrics["replies" if platform is Platform.THREADS else "comments"] = replies
    if platform in (Platform.INSTAGRAM, Platform.FACEBOOK):
        metrics["reach"] = float(math.floor(views * 0.85))
    return metrics
