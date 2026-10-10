"""Platform API client contract (shared by M8 publishing and M9 insights).

Only *official* APIs are implemented (§8.2: no browser macros, no unofficial APIs). Kakao has no
official short-form publishing API, so its client only supports the manual one-click queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from acvm.core.enums import Platform


class RemoteStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"  # container/upload ready to publish
    PUBLISHED = "published"
    ERROR = "error"
    EXPIRED = "expired"  # Instagram container > 24 h


@dataclass(slots=True)
class PostRequest:
    """Everything an adapter needs; produced by M8 from Content + Publication + AffiliateLink."""

    account_platform_user_id: str
    access_token: str
    text: str  # caption/body, disclosure already first (D-01)
    video_url: str | None = None  # public HTTPS (presigned R2) for pull-based platforms
    video_path: str | None = None  # local path for push uploads (YouTube resumable, FB rupload)
    thumbnail_url: str | None = None
    link_url: str | None = None  # Threads link attachment / first-comment link
    link_in_first_comment: bool = False
    title: str = ""  # YouTube title
    tags: list[str] = field(default_factory=list)
    ai_generated: bool = True  # A-01: YouTube containsSyntheticMedia / TikTok is_aigc
    commercial_disclosure: bool = True  # TikTok brand_content_toggle, IG/FB branded flags where available
    privacy: str = "public"
    idempotency_key: str = ""


@dataclass(slots=True)
class PostResult:
    status: RemoteStatus
    post_id: str = ""
    container_id: str = ""
    permalink: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PublishingLimit:
    used: int
    limit: int
    window_seconds: int = 86_400


@dataclass(slots=True)
class Insights:
    """Cumulative counters as of ``fetched_at`` (metric names match perf_event.metric)."""

    metrics: dict[str, float]
    raw: dict[str, Any] = field(default_factory=dict)


class PlatformWarning(Exception):
    """Platform returned a policy/spam warning or restriction signal (§7.1 #3 → immediate cooldown)."""


class PlatformClient(Protocol):
    platform: Platform

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        """Real-time quota usage where the platform exposes it (Threads/Instagram), else None."""
        ...

    async def create(self, req: PostRequest) -> PostResult:
        """Create the post or upload container. May return IN_PROGRESS (poll with ``status``)."""
        ...

    async def status(self, req: PostRequest, container_id: str) -> PostResult: ...

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        """Second phase for two-phase platforms; single-phase clients return the create result."""
        ...

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """Saga compensation (§5.2): take the post down or make it private."""
        ...

    async def insights(self, post_id: str, token: str) -> Insights: ...
