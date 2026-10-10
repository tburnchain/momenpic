"""Kakao channels: manual one-click queue (§4 M8, §7 matrix, §8.2).

No official Kakao short-form publishing API has been confirmed (§7: "공식 숏폼 게시 API 확인 안 됨"), and
browser macros / unofficial endpoints are on the never-do list (§8.2). This client therefore never posts
anything: ``create`` only reserves a manual-queue entry (``manual:{idempotency_key}``) that the console shows
with the caption and a signed media URL to copy; an operator posts by hand and records the permalink
(``DistributionService.complete_manual``). Status stays IN_PROGRESS until that happens, takedown is manual and
insights are unavailable.
"""

from __future__ import annotations

from acvm.core.enums import Platform
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus

MANUAL_PREFIX = "manual:"


class ManualQueueClient:
    """Placeholder client that enqueues for a human — it performs no network I/O at all."""

    platform = Platform.KAKAO
    manual = True

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        return None

    async def create(self, req: PostRequest) -> PostResult:
        return PostResult(RemoteStatus.IN_PROGRESS, container_id=f"{MANUAL_PREFIX}{req.idempotency_key}",
                          raw={"manual": True})

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        return PostResult(RemoteStatus.IN_PROGRESS, container_id=container_id, raw={"manual": True})

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        """Nothing is ever posted automatically; completion is recorded by an operator."""
        return await self.status(req, container_id)

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """Manual channel: the operator must take the post down by hand."""
        return False

    async def insights(self, post_id: str, token: str) -> Insights:
        return Insights({}, raw={"manual": True, "reason": "no official insights API"})
