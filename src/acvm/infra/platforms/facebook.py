"""Facebook Page Reels client (Graph API) + helpers shared by the Meta Graph family (Threads, Instagram).

§7 matrix: Facebook has no personal-profile publishing API — ACVM posts Reels to a *Page* it operates. The
caption link is clickable (§4 M3: "Facebook 릴스 캡션 링크, 대가성 문구 첫 줄").

Publishing is the documented three-step Reels flow:

1. ``POST /{page-id}/video_reels upload_phase=start`` → ``video_id``
2. ``POST {rupload}/video-upload/{version}/{video-id}`` with ``Authorization: OAuth …`` and ``file_url``
   (the 2-hour presigned object URL, §4 M8) — or the raw bytes when only a local file is available
3. ``POST /{page-id}/video_reels upload_phase=finish video_state=PUBLISHED description=…``

Publishing then completes asynchronously; ``status`` polls ``GET /{video-id}?fields=status``.

Meta Graph error semantics (also used by threads.py and instagram.py): throttling codes (4, 17, 32, 613,
80001+, subcode 2207042) are retryable; code 368 and Instagram subcodes 2207050/2207051 (restricted /
"we restrict certain activity") — or a spam/abuse message — are policy warnings (§7.1 #3).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from acvm.config import PlatformConfig
from acvm.core.enums import Platform
from acvm.core.errors import UpstreamError, ValidationFailed
from acvm.infra.platforms.base import Insights, PostRequest, PostResult, PublishingLimit, RemoteStatus
from acvm.infra.platforms.tokens import (
    ApiTransport,
    PlatformPolicyWarning,
    TokenBundle,
    as_dict,
    require_id,
    upstream_error,
)

# ───────────────────────────── Meta Graph family helpers ─────────────────────────────

GRAPH_WARNING_CODES = frozenset({368})  # "temporarily blocked for policies violations"
GRAPH_WARNING_SUBCODES = frozenset({2207050, 2207051})  # IG account restricted / action blocked as spam
GRAPH_THROTTLE_CODES = frozenset({4, 17, 32, 613, *range(80001, 80015)})
GRAPH_THROTTLE_SUBCODES = frozenset({2207042})  # IG: content publishing limit reached
GRAPH_TRANSIENT_CODES = frozenset({1, 2})
GRAPH_AUTH_CODES = frozenset({10, 102, 190, *range(200, 300)})  # permission/token problems are not sanctions
WARNING_PATTERN = re.compile(
    r"spam|abusive|restricted|temporarily blocked|action (?:was )?blocked|violat|community (?:standards|guidelines)"
    r"|suspended|checkpoint", re.IGNORECASE)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_graph_error(platform: Platform, status: int, body: Any) -> Exception | None:
    """Map a Graph API response (graph.facebook.com / graph.threads.net) to a typed failure or None."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        if status >= 400:
            text = err if isinstance(err, str) else (body if isinstance(body, str) else "")
            return upstream_error(platform, str(text)[:300] or f"HTTP {status}", status=status)
        return None
    code, subcode = _int(err.get("code")), _int(err.get("error_subcode"))
    message = str(err.get("error_user_msg") or err.get("message") or "")
    detail = f"{message} (code={code}, subcode={subcode})"
    if (code in GRAPH_WARNING_CODES or subcode in GRAPH_WARNING_SUBCODES
            or (message and code not in GRAPH_AUTH_CODES and WARNING_PATTERN.search(message))):
        return PlatformPolicyWarning(detail, platform=platform, code=subcode or code, status=status)
    if code in GRAPH_THROTTLE_CODES or subcode in GRAPH_THROTTLE_SUBCODES:
        return upstream_error(platform, detail, status=status if status >= 500 else 429, retryable=True,
                              code=code, subcode=subcode)
    if err.get("is_transient") or code in GRAPH_TRANSIENT_CODES:
        return upstream_error(platform, detail, status=status if status >= 400 else 503, retryable=True,
                              code=code, subcode=subcode)
    effective = status if status >= 400 else 400
    return upstream_error(platform, detail, status=effective, code=code, subcode=subcode)


def parse_publishing_limit(body: Mapping[str, Any]) -> PublishingLimit | None:
    """``{"data":[{"quota_usage":N,"config":{"quota_total":T,"quota_duration":S}}]}`` → PublishingLimit."""
    data = body.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    row = data[0]
    config = as_dict(row.get("config"))
    used, total = _int(row.get("quota_usage")), _int(config.get("quota_total"))
    if used is None or total is None:
        return None
    return PublishingLimit(used=used, limit=total, window_seconds=_int(config.get("quota_duration")) or 86_400)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        nums = [n for n in (_number(v) for v in value.values()) if n is not None]
        return float(sum(nums)) if nums else None
    return None


def insight_values(body: Mapping[str, Any]) -> dict[str, Any]:
    """Graph insights rows → ``{name: latest raw value}`` (handles ``values[]`` and ``total_value`` forms)."""
    out: dict[str, Any] = {}
    for item in body.get("data") or []:
        if not isinstance(item, dict) or "name" not in item:
            continue
        if isinstance(item.get("total_value"), dict):
            out[str(item["name"])] = item["total_value"].get("value")
        elif item.get("values"):
            out[str(item["name"])] = item["values"][-1].get("value")
    return out


def parse_graph_insights(body: Mapping[str, Any]) -> dict[str, float]:
    return {k: n for k, v in insight_values(body).items() if (n := _number(v)) is not None}


async def exchange_long_lived_token(transport: ApiTransport, graph: str, bundle: TokenBundle,
                                    now: datetime) -> TokenBundle:
    """§10: ``GET /oauth/access_token?grant_type=fb_exchange_token`` — renews a Meta long-lived token."""
    if not bundle.client_id or not bundle.client_secret:
        raise ValidationFailed("fb_exchange_token needs client_id and client_secret in the token secret")
    body = await transport.json("GET", f"{graph}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": bundle.client_id,
        "client_secret": bundle.client_secret, "fb_exchange_token": bundle.access_token})
    token = require_id(transport.platform, body, "access_token")
    return bundle.renewed(token, now=now, expires_in=body.get("expires_in"), token_type=body.get("token_type"))


# ───────────────────────────── Facebook Page Reels ─────────────────────────────

_FB_ERROR_PHASE_STATES = frozenset({"error", "failed"})


class FacebookReelsClient:
    """Official Graph API client for Facebook Page Reels."""

    platform = Platform.FACEBOOK
    DESCRIPTION_LIMIT = 5000

    def __init__(self, cfg: PlatformConfig, *, http: httpx.AsyncClient | None = None,
                 timeout: float | httpx.Timeout = 60.0) -> None:
        self.graph = f"{cfg.graph_base.rstrip('/')}/{cfg.graph_version}"
        self.rupload = f"{cfg.rupload_base.rstrip('/')}/video-upload/{cfg.graph_version}"
        self._t = ApiTransport(Platform.FACEBOOK, classify_graph_error, http=http, timeout=timeout)

    async def aclose(self) -> None:
        await self._t.aclose()

    async def publishing_limit(self, user_id: str, token: str) -> PublishingLimit | None:
        """Facebook exposes no publishing-usage endpoint for Page Reels."""
        return None

    async def create(self, req: PostRequest) -> PostResult:
        if not req.video_url and not req.video_path:
            raise ValidationFailed("Facebook Reels need a hosted video_url (file_url) or a local video_path")
        if len(req.text) > self.DESCRIPTION_LIMIT:
            raise ValidationFailed(f"Facebook description exceeds {self.DESCRIPTION_LIMIT} chars")
        page = req.account_platform_user_id
        start = await self._t.json("POST", f"{self.graph}/{page}/video_reels",
                                   data={"upload_phase": "start", "access_token": req.access_token})
        video_id = require_id(self.platform, start, "video_id")
        upload_url = f"{self.rupload}/{video_id}"
        headers = {"Authorization": f"OAuth {req.access_token}"}
        if req.video_url:
            uploaded = await self._t.json("POST", upload_url, headers={**headers, "file_url": req.video_url})
        else:
            payload = await asyncio.to_thread(Path(str(req.video_path)).read_bytes)
            uploaded = await self._t.json("POST", upload_url, content=payload, headers={
                **headers, "offset": "0", "file_size": str(len(payload))})
        if uploaded.get("success") is False:
            raise upstream_error(self.platform, "rupload reported failure", status=502, retryable=True)
        finish = await self._t.json("POST", f"{self.graph}/{page}/video_reels", data={
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": req.text, "access_token": req.access_token})
        if finish.get("success") is False:
            raise upstream_error(self.platform, "reel finish phase rejected", status=400, retryable=False)
        return PostResult(RemoteStatus.IN_PROGRESS, post_id=video_id, container_id=video_id,
                          permalink=f"https://www.facebook.com/reel/{video_id}", raw={"phase": "finish"})

    async def status(self, req: PostRequest, container_id: str) -> PostResult:
        body = await self._t.json("GET", f"{self.graph}/{container_id}",
                                  params={"fields": "status", "access_token": req.access_token})
        st = as_dict(body.get("status"))
        video_status = str(st.get("video_status", "")).lower()
        phases = {name: as_dict(st.get(name)) for name in ("uploading_phase", "processing_phase", "publishing_phase")}
        raw = {"video_status": video_status, **{k: v.get("status") for k, v in phases.items()}}
        if video_status == "error" or any(str(p.get("status", "")).lower() in _FB_ERROR_PHASE_STATES
                                          for p in phases.values()):
            return PostResult(RemoteStatus.ERROR, post_id=container_id, container_id=container_id, raw=raw)
        publishing = phases["publishing_phase"]
        if (str(publishing.get("status", "")).lower() == "complete"
                and str(publishing.get("publish_status", "published")).lower() == "published"):
            return PostResult(RemoteStatus.PUBLISHED, post_id=container_id, container_id=container_id,
                              permalink=f"https://www.facebook.com/reel/{container_id}", raw=raw)
        return PostResult(RemoteStatus.IN_PROGRESS, post_id=container_id, container_id=container_id, raw=raw)

    async def publish(self, req: PostRequest, container_id: str) -> PostResult:
        """Single-phase: the finish step already requested publication; report its progress."""
        return await self.status(req, container_id)

    async def hide_or_delete(self, post_id: str, token: str) -> bool:
        """``DELETE /{video-id}``. An already-deleted object counts as taken down."""
        try:
            body = await self._t.json("DELETE", f"{self.graph}/{post_id}", params={"access_token": token})
        except UpstreamError as exc:
            if exc.status == 404 or (exc.context.get("code") == 100 and exc.context.get("subcode") == 33):
                return True
            raise
        return bool(body.get("success", False))

    async def insights(self, post_id: str, token: str) -> Insights:
        body = await self._t.json("GET", f"{self.graph}/{post_id}/video_insights",
                                  params={"access_token": token})
        values = insight_values(body)
        metrics: dict[str, float] = {}
        views = next((_number(values[k]) for k in ("fb_reels_total_plays", "blue_reels_play_count",
                                                   "total_video_views") if k in values), None)
        if views is not None:
            metrics["views"] = views
        if (reach := _number(values.get("post_impressions_unique"))) is not None:
            metrics["reach"] = reach
        if (avg_ms := _number(values.get("post_video_avg_time_watched"))) is not None:
            metrics["avg_watch_sec"] = avg_ms / 1000.0
        if (likes := _number(values.get("post_video_likes_by_reaction_type"))) is not None:
            metrics["likes"] = likes
        social = values.get("post_video_social_actions")
        if isinstance(social, dict):
            for src, dst in (("COMMENT", "comments"), ("SHARE", "shares")):
                if (n := _number(social.get(src))) is not None:
                    metrics[dst] = n
        return Insights(metrics, raw=body)

    async def refresh_token(self, bundle: TokenBundle, now: datetime) -> TokenBundle:
        return await exchange_long_lived_token(self._t, self.graph, bundle, now)
