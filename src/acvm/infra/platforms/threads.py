"""Threads API client (graph.threads.net) — the V1 text vending machine's main channel (§1, §7).

§7 matrix: 250 posts / 1,000 replies per rolling 24 h, 500-character text, clickable body links. ACVM's
operating cap is 20 posts/day and ``threads_publishing_limit`` is queried before every post (§7.1 #2).

Two-phase publishing:

* ``POST /{user-id}/threads`` creates a container — ``media_type=TEXT`` (optionally ``link_attachment``) or
  ``media_type=VIDEO`` with a public ``video_url`` (presigned, §4 M8);
* ``GET /{container-id}?fields=status,error_message`` → IN_PROGRESS / FINISHED / ERROR / EXPIRED / PUBLISHED;
* ``POST /{user-id}/threads_publish?creation_id=…`` publishes it.

Link placement (§4 M3): the link goes in the body (with ``link_attachment`` for a preview card) or, when
``link_in_first_comment`` is set, into a reply container (``reply_to_id``) published right after the post.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

import httpx

from acvm.config import PlatformConfig
from acvm.core.enums import Platform
from acvm.core.errors import UpstreamError, ValidationFailed
from acvm.infra.platforms.base import (
    Insights,
    PlatformWarning,
    PostRequest,
    PostResult,
    PublishingLimit,
    RemoteStatus,
)
from acvm.infra.platforms.facebook import classify_graph_error, parse_graph_insights, parse_publishing_limit
from acvm.infra.platforms.tokens import ApiTransport, TokenBundle, require_id

_STATUS = {
    "IN_PROGRESS": RemoteStatus.IN_PROGRESS,
    "FINISHED": RemoteStatus.FINISHED,
    "PUBLISHED": RemoteStatus.PUBLISHED,
    "ERROR": RemoteStatus.ERROR,
    "EXPIRED": RemoteStatus.EXPIRED,
}
INSIGHT_METRICS = "views,likes,replies,reposts,quotes,shares"


class ThreadsClient:
    """Official Threads API client."""

    platform = Platform.THREADS
    TEXT_LIMIT = 500

    def __init__(self, cfg: PlatformConfig, *, http: httpx.AsyncClient | None = None,
                 timeout: float | httpx.Timeout = 30.0) -> None:
        self.base = cfg.threads_base.rstrip("/")
        parts = urlsplit(self.base)
        self.root = f"{parts.scheme}://{parts.netloc}"  # token refresh is unversioned
        self._t = ApiTransport(Platform.THREADS, classify_graph_error, http=http, timeout=timeout)

    async def aclose(self) -> None:
        await self._t.aclose()

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        body = await self._t.json("GET", f"{self.base}/{user_id}/threads_publishing_limit",
                                  params={"fields": "quota_usage,config", "access_token": token})
        return parse_publishing_limit(body)

    async def create(self, req: PostRequest) -> PostResult:
        if len(req.text) > self.TEXT_LIMIT:
            raise ValidationFailed(f"Threads text exceeds {self.TEXT_LIMIT} characters ({len(req.text)})")
        data: dict[str, str] = {"text": req.text, "access_token": req.access_token}
        if req.video_url:
            data.update(media_type="VIDEO", video_url=req.video_url)
        else:
            data["media_type"] = "TEXT"
            if req.link_url and not req.link_in_first_comment:
                data["link_attachment"] = req.link_url
        body = await self._t.json("POST", f"{self.base}/{req.account_platform_user_id}/threads", data=data)
        container_id = require_id(self.platform, body)
        # text containers are ready immediately; video containers need processing (poll ``status``)
        status = RemoteStatus.FINISHED if data["media_type"] == "TEXT" else RemoteStatus.IN_PROGRESS
        return PostResult(status, container_id=container_id, raw={"media_type": data["media_type"]})

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("GET", f"{self.base}/{container_id}",
                                  params={"fields": "status,error_message", "access_token": req.access_token})
        state = str(body.get("status", "")).upper()
        mapped = _STATUS.get(state, RemoteStatus.IN_PROGRESS)
        raw = {"status": state, "error_message": body.get("error_message")}
        return PostResult(mapped, container_id=container_id, raw=raw)

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("POST", f"{self.base}/{req.account_platform_user_id}/threads_publish",
                                  data={"creation_id": container_id, "access_token": req.access_token})
        media_id = require_id(self.platform, body)
        raw: dict[str, object] = {}
        if req.link_in_first_comment and req.link_url:
            # the post is already live: a failed reply must not turn it into a failure (recorded instead)
            try:
                raw["first_comment_id"] = await self._reply(req, media_id, req.link_url)
            except PlatformWarning as exc:
                raw["platform_warning"] = str(exc)
            except UpstreamError as exc:
                raw["first_comment_error"] = str(exc)
        permalink = await self._permalink(media_id, req.access_token)
        return PostResult(RemoteStatus.PUBLISHED, post_id=media_id, container_id=container_id,
                          permalink=permalink, raw=raw)

    async def _reply(self, req: PostRequest, media_id: str, text: str) -> str:
        body = await self._t.json("POST", f"{self.base}/{req.account_platform_user_id}/threads", data={
            "media_type": "TEXT", "text": text, "reply_to_id": media_id, "access_token": req.access_token})
        reply_container = require_id(self.platform, body)
        published = await self._t.json("POST", f"{self.base}/{req.account_platform_user_id}/threads_publish",
                                       data={"creation_id": reply_container, "access_token": req.access_token})
        return require_id(self.platform, published)

    async def _permalink(self, media_id: str, token: str) -> str:
        try:
            body = await self._t.json("GET", f"{self.base}/{media_id}",
                                      params={"fields": "permalink", "access_token": token})
        except UpstreamError:
            return ""  # cosmetic; never fail a live post over it
        return str(body.get("permalink") or "")

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """``DELETE /{threads-media-id}`` (Threads supports deleting own posts)."""
        body = await self._t.json("DELETE", f"{self.base}/{post_id}", params={"access_token": token})
        return bool(body.get("success", False))

    async def insights(self, post_id: str, token: str) -> Insights:
        body = await self._t.json("GET", f"{self.base}/{post_id}/insights",
                                  params={"metric": INSIGHT_METRICS, "access_token": token})
        return Insights(parse_graph_insights(body), raw=body)

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        """``GET /refresh_access_token?grant_type=th_refresh_token`` (long-lived tokens, 60 days)."""
        body = await self._t.json("GET", f"{self.root}/refresh_access_token",
                                  params={"grant_type": "th_refresh_token", "access_token": bundle.access_token})
        token = require_id(self.platform, body, "access_token")
        return bundle.renewed(token, now=now, expires_in=body.get("expires_in"), token_type=body.get("token_type"))
