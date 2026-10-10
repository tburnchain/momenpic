"""YouTube Data API v3 client for Shorts (§7: ``videos.insert``, AI-synthetic disclosure field, A-01).

Upload is the documented two-step resumable protocol:

1. ``POST {upload_base}/videos?uploadType=resumable&part=snippet,status`` with the JSON metadata —
   ``snippet{title ≤ 100, description ≤ 5000, tags, categoryId "26"}`` and
   ``status{privacyStatus, selfDeclaredMadeForKids: false, containsSyntheticMedia: true}`` — returns the
   session URI in ``Location``;
2. ``PUT`` the bytes of ``req.video_path`` to that URI.

YouTube then processes the upload; ``status`` polls ``videos.list(part=status)`` until ``processed``.
A rejection for policy reasons (terms of use, inappropriate, copyright, suspended uploader …) is a platform
warning (§7.1 #3). Shorts description links are not clickable (§4 M3), so the description carries the hub
notice and hub URL composed by M8.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from acvm.config import PlatformConfig
from acvm.core.enums import Platform
from acvm.core.errors import UpstreamError, ValidationFailed
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus
from acvm.infra.platforms.facebook import WARNING_PATTERN
from acvm.infra.platforms.tokens import (
    ApiTransport,
    PlatformPolicyWarning,
    TokenBundle,
    require_id,
    upstream_error,
)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CATEGORY_HOWTO_STYLE = "26"  # "Howto & Style" — product-use shorts
RETRYABLE_REASONS = frozenset({"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded", "uploadLimitExceeded",
                           "dailyLimitExceeded", "backendError"})
POLICY_REJECTIONS = frozenset({"copyright", "inappropriate", "legal", "termsOfUse", "trademark",
                               "uploaderAccountClosed", "uploaderAccountSuspended"})
_PRIVACY = {"public": "public", "unlisted": "unlisted", "private": "private", "self_only": "private"}


def classify_google_error(platform: Platform, status: int, body: Any) -> Exception | None:
    """Google JSON errors (``{"error":{"errors":[{"reason":…}]}}``) and OAuth errors (``{"error":"invalid_grant"}``)."""
    if status < 400:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):  # OAuth token endpoint
        desc = str(body.get("error_description", "")) if isinstance(body, dict) else ""
        return upstream_error(platform, f"{err}: {desc}".strip(": "), status=status, code=err)
    if not isinstance(err, dict):
        return upstream_error(platform, f"HTTP {status}", status=status)
    reasons = [str(e.get("reason", "")) for e in err.get("errors") or [] if isinstance(e, dict)]
    message = str(err.get("message", ""))
    reason = reasons[0] if reasons else None
    if any(r in RETRYABLE_REASONS for r in reasons):
        return upstream_error(platform, f"{message} ({reason})", status=status, retryable=True, code=reason)
    if any(r in POLICY_REJECTIONS for r in reasons) or WARNING_PATTERN.search(message):
        return PlatformPolicyWarning(f"{message} ({reason})", platform=platform, code=reason, status=status)
    return upstream_error(platform, f"{message} ({reason})", status=status, code=reason)


def fit_tags(tags: list[str], limit: int = 500) -> list[str]:
    """YouTube caps the combined tag length at 500 characters (multi-word tags count their quotes)."""
    out: list[str] = []
    used = 0
    for raw in tags:
        tag = raw.lstrip("#").strip().replace("<", "").replace(">", "")
        if not tag:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(tag)
        used += cost
    return out


class YouTubeShortsClient:
    """Official YouTube Data API v3 client (resumable upload)."""

    platform = Platform.YOUTUBE
    TITLE_LIMIT = 100
    DESCRIPTION_LIMIT = 5000

    def __init__(self, cfg: PlatformConfig, *, http: httpx.AsyncClient | None = None,
                 timeout: float | httpx.Timeout = 30.0, upload_timeout: float = 600.0,
                 token_url: str = GOOGLE_TOKEN_URL) -> None:
        self.upload_base = cfg.youtube_upload_base.rstrip("/")
        self.base = cfg.youtube_base.rstrip("/")
        self.token_url = token_url
        self.upload_timeout = httpx.Timeout(upload_timeout, connect=15.0)
        self._t = ApiTransport(Platform.YOUTUBE, classify_google_error, http=http, timeout=timeout)

    async def aclose(self) -> None:
        await self._t.aclose()

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        """Upload quota lives in the Cloud Console quota page (§7) — not queryable per channel."""
        return None

    async def create(self, req: PostRequest) -> PostResult:
        if not req.video_path:
            raise ValidationFailed("YouTube resumable upload needs a local video_path")
        title = req.title.strip()
        if not title or len(title) > self.TITLE_LIMIT or any(c in title for c in "<>"):
            raise ValidationFailed(f"YouTube title must be 1–{self.TITLE_LIMIT} chars without '<' or '>'")
        if len(req.text) > self.DESCRIPTION_LIMIT or any(c in req.text for c in "<>"):
            raise ValidationFailed(f"YouTube description must be ≤ {self.DESCRIPTION_LIMIT} chars without '<' '>'")
        path = Path(req.video_path)
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
        except OSError as exc:
            raise ValidationFailed(f"video file unreadable: {type(exc).__name__}") from None
        metadata = {
            "snippet": {"title": title, "description": req.text, "tags": fit_tags(req.tags),
                        "categoryId": CATEGORY_HOWTO_STYLE},
            "status": {"privacyStatus": _PRIVACY.get(req.privacy.lower(), "private"),
                       "selfDeclaredMadeForKids": False, "containsSyntheticMedia": req.ai_generated},
        }
        init = await self._t.request(
            "POST", f"{self.upload_base}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
            json_body=metadata, headers={**self._auth(req.access_token), "X-Upload-Content-Type": "video/mp4",
                                         "X-Upload-Content-Length": str(size)})
        session_uri = init.headers.get("Location")
        if not session_uri:
            raise upstream_error(self.platform, "resumable session URI missing", status=502, retryable=True)
        payload = await asyncio.to_thread(path.read_bytes)
        put = await self._t.request("PUT", session_uri, content=payload, http_timeout=self.upload_timeout,
                                    headers={**self._auth(req.access_token), "Content-Type": "video/mp4"})
        video = put.json() if put.content else {}
        video_id = require_id(self.platform, video if isinstance(video, dict) else {})
        upload_status = (video.get("status") or {}).get("uploadStatus", "uploaded")
        return PostResult(RemoteStatus.IN_PROGRESS, post_id=video_id, container_id=video_id,
                          permalink=f"https://www.youtube.com/shorts/{video_id}",
                          raw={"uploadStatus": upload_status})

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("GET", f"{self.base}/videos", params={"part": "status", "id": container_id},
                                  headers=self._auth(req.access_token))
        items = body.get("items") or []
        if not items:
            return PostResult(RemoteStatus.ERROR, post_id=container_id, container_id=container_id,
                              raw={"reason": "video not found"})
        st = items[0].get("status") or {}
        upload_status = str(st.get("uploadStatus", ""))
        raw = {"uploadStatus": upload_status, "failureReason": st.get("failureReason"),
               "rejectionReason": st.get("rejectionReason")}
        if upload_status == "processed":
            return PostResult(RemoteStatus.PUBLISHED, post_id=container_id, container_id=container_id,
                              permalink=f"https://www.youtube.com/shorts/{container_id}", raw=raw)
        if upload_status == "rejected" and st.get("rejectionReason") in POLICY_REJECTIONS:
            raise PlatformPolicyWarning(f"video rejected: {st.get('rejectionReason')}", platform=self.platform,
                                        code=str(st.get("rejectionReason")))
        if upload_status in ("failed", "rejected", "deleted"):
            return PostResult(RemoteStatus.ERROR, post_id=container_id, container_id=container_id, raw=raw)
        return PostResult(RemoteStatus.IN_PROGRESS, post_id=container_id, container_id=container_id, raw=raw)

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        """Single-phase: the upload is the post; report processing progress."""
        return await self.status(req, container_id)

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """``videos.delete``; an already-deleted video counts as taken down."""
        try:
            await self._t.request("DELETE", f"{self.base}/videos", params={"id": post_id},
                                  headers=self._auth(token))
        except UpstreamError as exc:
            if exc.status == 404:
                return True
            raise
        return True

    async def insights(self, post_id: str, token: str) -> Insights:
        body = await self._t.json("GET", f"{self.base}/videos", params={"part": "statistics", "id": post_id},
                                  headers=self._auth(token))
        items = body.get("items") or []
        stats = items[0].get("statistics", {}) if items else {}
        metrics: dict[str, float] = {}
        for src, dst in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments")):
            if src in stats:
                metrics[dst] = float(stats[src])
        return Insights(metrics, raw=body)

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        """OAuth 2.0 ``refresh_token`` grant at ``oauth2.googleapis.com/token``."""
        if not (bundle.refresh_token and bundle.client_id and bundle.client_secret):
            raise ValidationFailed("Google refresh needs refresh_token, client_id and client_secret")
        body = await self._t.json("POST", self.token_url, data={
            "grant_type": "refresh_token", "refresh_token": bundle.refresh_token,
            "client_id": bundle.client_id, "client_secret": bundle.client_secret})
        token = require_id(self.platform, body, "access_token")
        return bundle.renewed(token, now=now, expires_in=body.get("expires_in"),
                              refresh_token=body.get("refresh_token"), token_type=body.get("token_type"))
