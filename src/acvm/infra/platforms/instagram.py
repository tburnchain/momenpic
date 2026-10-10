"""Instagram Content Publishing API client (Reels, professional accounts) — §4 M8, §7.

§7 matrix: 100 posts per rolling 24 h (older docs: 50), 400 containers, containers expire after 24 h;
ACVM caps Reels at 5/day and checks ``content_publishing_limit`` before every post (§7.1 #2). Caption links
are not clickable, so captions point to the profile link hub (§4 M3).

Two-phase flow: ``POST /{ig-user-id}/media`` (``media_type=REELS``, public ``video_url`` — the presigned
object URL — ``caption`` ≤ 2,200, ``share_to_feed=true``, ``cover_url`` or ``thumb_offset``) →
``GET /{container-id}?fields=status_code`` until FINISHED → ``POST /{ig-user-id}/media_publish``.

Limitation (documented, never faked): the Instagram Graph API offers no endpoint to delete or hide a
published media object, so :meth:`hide_or_delete` returns ``False`` and the saga records a manual takedown.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

from acvm.config import PlatformConfig
from acvm.core.enums import Platform
from acvm.core.errors import UpstreamError, ValidationFailed
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus
from acvm.infra.platforms.facebook import (
    classify_graph_error,
    exchange_long_lived_token,
    parse_graph_insights,
    parse_publishing_limit,
)
from acvm.infra.platforms.tokens import ApiTransport, TokenBundle, require_id

_STATUS = {
    "IN_PROGRESS": RemoteStatus.IN_PROGRESS,
    "FINISHED": RemoteStatus.FINISHED,
    "PUBLISHED": RemoteStatus.PUBLISHED,
    "ERROR": RemoteStatus.ERROR,
    "EXPIRED": RemoteStatus.EXPIRED,
}
INSIGHT_METRICS = "views,reach,likes,comments,shares,saved"
_HASHTAG = re.compile(r"(?<![\w&])#\w+")


class InstagramReelsClient:
    """Official Instagram Graph API client for Reels."""

    platform = Platform.INSTAGRAM
    CAPTION_LIMIT = 2200
    HASHTAG_LIMIT = 30
    CONTAINER_TTL = timedelta(hours=24)

    def __init__(self, cfg: PlatformConfig, *, http: httpx.AsyncClient | None = None,
                 timeout: float | httpx.Timeout = 30.0, thumb_offset_ms: int = 1000) -> None:
        self.graph = f"{cfg.graph_base.rstrip('/')}/{cfg.graph_version}"
        self.thumb_offset_ms = thumb_offset_ms
        self._t = ApiTransport(Platform.INSTAGRAM, classify_graph_error, http=http, timeout=timeout)

    async def aclose(self) -> None:
        await self._t.aclose()

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        body = await self._t.json("GET", f"{self.graph}/{user_id}/content_publishing_limit",
                                  params={"fields": "quota_usage,config", "access_token": token})
        return parse_publishing_limit(body)

    async def create(self, req: PostRequest) -> PostResult:
        if not req.video_url:
            raise ValidationFailed("Instagram Reels pull media from a public HTTPS video_url")
        if len(req.text) > self.CAPTION_LIMIT:
            raise ValidationFailed(f"Instagram caption exceeds {self.CAPTION_LIMIT} chars ({len(req.text)})")
        if len(_HASHTAG.findall(req.text)) > self.HASHTAG_LIMIT:
            raise ValidationFailed(f"Instagram allows at most {self.HASHTAG_LIMIT} hashtags")
        data: dict[str, str] = {
            "media_type": "REELS", "video_url": req.video_url, "caption": req.text,
            "share_to_feed": "true", "access_token": req.access_token,
        }
        if req.thumbnail_url:
            data["cover_url"] = req.thumbnail_url
        else:
            data["thumb_offset"] = str(self.thumb_offset_ms)
        body = await self._t.json("POST", f"{self.graph}/{req.account_platform_user_id}/media", data=data)
        return PostResult(RemoteStatus.IN_PROGRESS, container_id=require_id(self.platform, body),
                          raw={"media_type": "REELS"})

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("GET", f"{self.graph}/{container_id}",
                                  params={"fields": "status_code,status", "access_token": req.access_token})
        code = str(body.get("status_code", "")).upper()
        return PostResult(_STATUS.get(code, RemoteStatus.IN_PROGRESS), container_id=container_id,
                          raw={"status_code": code, "status": body.get("status")})

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("POST", f"{self.graph}/{req.account_platform_user_id}/media_publish",
                                  data={"creation_id": container_id, "access_token": req.access_token})
        media_id = require_id(self.platform, body)
        return PostResult(RemoteStatus.PUBLISHED, post_id=media_id, container_id=container_id,
                          permalink=await self._permalink(media_id, req.access_token))

    async def _permalink(self, media_id: str, token: str) -> str:
        try:
            body = await self._t.json("GET", f"{self.graph}/{media_id}",
                                      params={"fields": "permalink", "access_token": token})
        except UpstreamError:
            return ""
        return str(body.get("permalink") or "")

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """Unsupported by the official API (see module docstring) — never reported as success."""
        return False

    async def insights(self, post_id: str, token: str) -> Insights:
        body = await self._t.json("GET", f"{self.graph}/{post_id}/insights",
                                  params={"metric": INSIGHT_METRICS, "access_token": token})
        return Insights(parse_graph_insights(body), raw=body)

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        """Instagram via Facebook Login uses Meta long-lived tokens (``fb_exchange_token``)."""
        return await exchange_long_lived_token(self._t, self.graph, bundle, now)
