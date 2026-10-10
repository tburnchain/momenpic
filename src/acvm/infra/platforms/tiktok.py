"""TikTok Content Posting API client (Direct Post, ``PULL_FROM_URL``) — §7, A-01.

§7 matrix: unaudited apps may only post privately (``SELF_ONLY``) and to a handful of users; public posting is
gated on TikTok's audit, which is the release gate of this adapter. Therefore, while
``settings.platforms.tiktok_audited`` is False every post is forced to ``SELF_ONLY`` and the result carries
``raw["unaudited"] = True`` so the console can show that nothing is publicly visible.

Flow: ``POST /v2/post/publish/creator_info/query/`` (privacy options, interaction settings, max duration) →
``POST /v2/post/publish/video/init/`` with ``post_info`` (caption, ``privacy_level``, interaction flags,
``brand_content_toggle`` for the commercial disclosure, ``is_aigc`` for the AI label) and
``source_info{source: PULL_FROM_URL, video_url}`` (presigned URL on a verified domain) →
``POST /v2/post/publish/status/fetch/`` until ``PUBLISH_COMPLETE``.

Documented limitations (never faked):

* TikTok rejects branded content with "only me" visibility, so while posts are forced to ``SELF_ONLY`` the
  ``brand_content_toggle`` is sent as False and ``raw["brand_content_deferred"]`` is set; the text disclosure
  is still the first caption line (D-01).
* The Content Posting API has no delete/hide endpoint → :meth:`hide_or_delete` returns False.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from acvm.config import PlatformConfig
from acvm.core.enums import Platform
from acvm.core.errors import ValidationFailed
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus
from acvm.infra.platforms.tokens import (
    ApiTransport,
    PlatformPolicyWarning,
    TokenBundle,
    as_dict,
    require_id,
    upstream_error,
)

RETRYABLE_CODES = frozenset({"rate_limit_exceeded", "internal_error", "spam_risk_too_many_posts",
                             "spam_risk_too_many_pending_share", "reached_active_user_cap"})
WARNING_CODES = frozenset({"spam_risk_user_banned_from_posting", "spam_risk_text", "spam_risk",
                           "user_banned", "account_banned"})
WARNING_FAIL_REASONS = frozenset({"spam_risk_user_banned_from_posting", "spam_risk_text", "spam_risk",
                                  "user_banned_from_posting"})
_PRIVACY = {"public": "PUBLIC_TO_EVERYONE", "friends": "MUTUAL_FOLLOW_FRIENDS",
            "followers": "FOLLOWER_OF_CREATOR", "private": "SELF_ONLY", "self_only": "SELF_ONLY"}
VIDEO_FIELDS = "id,view_count,like_count,comment_count,share_count"


def utf16_len(text: str) -> int:
    """TikTok counts caption length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def classify_tiktok_error(platform: Platform, status: int, body: Any) -> Exception | None:
    """TikTok wraps results in ``{"data":…, "error":{"code":"ok"|…, "message", "log_id"}}`` (even on 200)."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):  # OAuth endpoint: {"error":"invalid_grant","error_description":…}
        desc = str(body.get("error_description", "")) if isinstance(body, dict) else ""
        return upstream_error(platform, f"{err}: {desc}".strip(": "), status=status if status >= 400 else 400,
                              code=err)
    code = str(err.get("code", "")) if isinstance(err, dict) else ""
    if code in ("", "ok") and status < 400:
        return None
    message = str(err.get("message", "")) if isinstance(err, dict) else ""
    detail = f"{message} ({code or status})"
    if code in WARNING_CODES or (code.startswith("spam_risk") and code not in RETRYABLE_CODES):
        return PlatformPolicyWarning(detail, platform=platform, code=code, status=status)
    if code in RETRYABLE_CODES:
        return upstream_error(platform, detail, status=status if status >= 400 else 429, retryable=True, code=code)
    return upstream_error(platform, detail, status=status if status >= 400 else 400, code=code or None)


class TikTokClient:
    """Official TikTok Content Posting API client."""

    platform = Platform.TIKTOK
    CAPTION_LIMIT = 2200

    def __init__(self, cfg: PlatformConfig, *, http: httpx.AsyncClient | None = None,
                 timeout: float | httpx.Timeout = 30.0, audited: bool | None = None,
                 cover_timestamp_ms: int = 1000) -> None:
        self.base = cfg.tiktok_base.rstrip("/")
        self.audited = cfg.tiktok_audited if audited is None else audited
        self.cover_timestamp_ms = cover_timestamp_ms
        self._t = ApiTransport(Platform.TIKTOK, classify_tiktok_error, http=http, timeout=timeout)

    async def aclose(self) -> None:
        await self._t.aclose()

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        """TikTok exposes no usage counter; the per-user daily cap surfaces as ``spam_risk_too_many_posts``."""
        return None

    async def creator_info(self, token: str) -> dict[str, Any]:
        body = await self._t.json("POST", f"{self.base}/v2/post/publish/creator_info/query/",
                                  headers=self._auth(token), json_body={})
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _privacy(self, requested: str, options: list[str]) -> str:
        level = "SELF_ONLY" if not self.audited else _PRIVACY.get(requested.lower(), requested.upper())
        if options and level not in options:
            raise ValidationFailed(f"TikTok privacy level {level} not offered for this creator",
                                   options=options)
        return level

    async def create(self, req: PostRequest) -> PostResult:
        if not req.video_url:
            raise ValidationFailed("TikTok PULL_FROM_URL needs a public HTTPS video_url")
        if utf16_len(req.text) > self.CAPTION_LIMIT:
            raise ValidationFailed(f"TikTok caption exceeds {self.CAPTION_LIMIT} UTF-16 units")
        info = await self.creator_info(req.access_token)
        privacy = self._privacy(req.privacy, [str(o) for o in info.get("privacy_level_options") or []])
        branded = req.commercial_disclosure and privacy != "SELF_ONLY"
        post_info = {
            "title": req.text,
            "privacy_level": privacy,
            "disable_duet": bool(info.get("duet_disabled", False)),
            "disable_stitch": bool(info.get("stitch_disabled", False)),
            "disable_comment": bool(info.get("comment_disabled", False)),
            "video_cover_timestamp_ms": self.cover_timestamp_ms,
            "brand_content_toggle": branded,
            "brand_organic_toggle": False,
            "is_aigc": req.ai_generated,
        }
        body = await self._t.json("POST", f"{self.base}/v2/post/publish/video/init/",
                                  headers=self._auth(req.access_token), json_body={
                                      "post_info": post_info,
                                      "source_info": {"source": "PULL_FROM_URL", "video_url": req.video_url}})
        data = as_dict(body.get("data"))
        publish_id = require_id(self.platform, data, "publish_id")
        raw: dict[str, Any] = {"privacy_level": privacy, "unaudited": not self.audited,
                               "is_aigc": req.ai_generated, "brand_content_toggle": branded,
                               "max_video_post_duration_sec": info.get("max_video_post_duration_sec")}
        if req.commercial_disclosure and not branded:
            raw["brand_content_deferred"] = True
        return PostResult(RemoteStatus.IN_PROGRESS, container_id=publish_id, raw=raw)

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("POST", f"{self.base}/v2/post/publish/status/fetch/",
                                  headers=self._auth(req.access_token), json_body={"publish_id": container_id})
        data = as_dict(body.get("data"))
        state = str(data.get("status", ""))
        raw = {"status": state, "fail_reason": data.get("fail_reason"), "unaudited": not self.audited}
        if state == "PUBLISH_COMPLETE":
            # TikTok's field name is spelled "publicaly"; private (SELF_ONLY) posts expose no public id
            ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
            post_id = str(ids[0]) if ids else ""
            return PostResult(RemoteStatus.PUBLISHED, post_id=post_id, container_id=container_id, raw=raw)
        if state == "FAILED":
            reason = str(data.get("fail_reason", ""))
            if reason in WARNING_FAIL_REASONS or reason.startswith("spam_risk"):
                raise PlatformPolicyWarning(f"publish failed: {reason}", platform=self.platform, code=reason)
            return PostResult(RemoteStatus.ERROR, container_id=container_id, raw=raw)
        return PostResult(RemoteStatus.IN_PROGRESS, container_id=container_id, raw=raw)

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        """Single-phase (Direct Post): report the publish status."""
        return await self.status(req, container_id)

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """No official delete/hide endpoint — never reported as success."""
        return False

    async def insights(self, post_id: str, token: str) -> Insights:
        if not post_id or post_id.startswith("v_pub"):
            return Insights({}, raw={"reason": "no public video id (private post)"})
        body = await self._t.json("POST", f"{self.base}/v2/video/query/", params={"fields": VIDEO_FIELDS},
                                  headers=self._auth(token), json_body={"filters": {"video_ids": [post_id]}})
        data = as_dict(body.get("data"))
        videos = data.get("videos") or []
        video = videos[0] if videos and isinstance(videos[0], dict) else {}
        metrics = {dst: float(video[src]) for src, dst in (("view_count", "views"), ("like_count", "likes"),
                                                           ("comment_count", "comments"),
                                                           ("share_count", "shares")) if src in video}
        return Insights(metrics, raw=body)

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        """``POST /v2/oauth/token/`` with ``grant_type=refresh_token`` (access 24 h, refresh 365 days)."""
        if not (bundle.refresh_token and bundle.client_id and bundle.client_secret):
            raise ValidationFailed("TikTok refresh needs refresh_token, client_id (client_key) and client_secret")
        body = await self._t.json("POST", f"{self.base}/v2/oauth/token/", data={
            "client_key": bundle.client_id, "client_secret": bundle.client_secret,
            "grant_type": "refresh_token", "refresh_token": bundle.refresh_token})
        token = require_id(self.platform, body, "access_token")
        extra: dict[str, Any] = {}
        if body.get("refresh_expires_in"):
            extra["refresh_expires_in"] = body["refresh_expires_in"]
        return bundle.renewed(token, now=now, expires_in=body.get("expires_in"),
                              refresh_token=body.get("refresh_token"), token_type=body.get("token_type"), **extra)
