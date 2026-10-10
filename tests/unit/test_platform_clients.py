"""Request-shape and error-mapping tests for the official-API platform clients (respx, no network)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from acvm.config import PlatformConfig
from acvm.core.errors import UpstreamError, ValidationFailed
from acvm.infra.platforms.base import PlatformWarning, PostRequest, RemoteStatus
from acvm.infra.platforms.facebook import FacebookReelsClient
from acvm.infra.platforms.instagram import InstagramReelsClient
from acvm.infra.platforms.kakao import ManualQueueClient
from acvm.infra.platforms.threads import ThreadsClient
from acvm.infra.platforms.tiktok import TikTokClient
from acvm.infra.platforms.youtube import YouTubeShortsClient

CFG = PlatformConfig()
TH = CFG.threads_base
GRAPH = f"{CFG.graph_base}/{CFG.graph_version}"
TOKEN = "tok-SECRET-123"
VIDEO = "https://media.example/r/abc.mp4?exp=1&sig=s"


def form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def req(**kw: object) -> PostRequest:
    base: dict[str, object] = {"account_platform_user_id": "uid", "access_token": TOKEN,
                               "text": "이 포스팅은 쿠팡 파트너스 활동의 일환으로…\n본문", "idempotency_key": "k1"}
    base.update(kw)
    return PostRequest(**base)  # type: ignore[arg-type]


# ───────────────────────────── Threads ─────────────────────────────


@respx.mock
async def test_threads_text_post_with_link_attachment() -> None:
    create = respx.post(f"{TH}/uid/threads").mock(return_value=httpx.Response(200, json={"id": "c1"}))
    publish = respx.post(f"{TH}/uid/threads_publish").mock(return_value=httpx.Response(200, json={"id": "m1"}))
    respx.get(f"{TH}/m1").mock(return_value=httpx.Response(200, json={"permalink": "https://threads.net/p/1"}))
    client = ThreadsClient(CFG)
    r = req(link_url="https://link.coupang.com/a/xyz")
    created = await client.create(r)
    assert created.status is RemoteStatus.FINISHED and created.container_id == "c1"
    sent = form(create.calls.last.request)
    assert sent["media_type"] == "TEXT" and sent["link_attachment"] == "https://link.coupang.com/a/xyz"
    assert TOKEN not in str(create.calls.last.request.url)  # POST tokens travel in the body
    done = await client.publish(r, "c1")
    assert done.status is RemoteStatus.PUBLISHED and done.post_id == "m1"
    assert done.permalink == "https://threads.net/p/1"
    assert form(publish.calls.last.request)["creation_id"] == "c1"


@respx.mock
async def test_threads_video_two_phase_then_first_comment_reply() -> None:
    create = respx.post(f"{TH}/uid/threads").mock(side_effect=[
        httpx.Response(200, json={"id": "c2"}), httpx.Response(200, json={"id": "rc1"})])
    status = respx.get(f"{TH}/c2").mock(return_value=httpx.Response(200, json={"status": "FINISHED"}))
    publish = respx.post(f"{TH}/uid/threads_publish").mock(side_effect=[
        httpx.Response(200, json={"id": "m2"}), httpx.Response(200, json={"id": "r2"})])
    respx.get(f"{TH}/m2").mock(return_value=httpx.Response(200, json={"permalink": "https://threads.net/p/2"}))
    client = ThreadsClient(CFG)
    r = req(video_url=VIDEO, link_url="https://link.coupang.com/a/xyz", link_in_first_comment=True)
    created = await client.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS
    first = form(create.calls[0].request)
    assert first["media_type"] == "VIDEO" and first["video_url"] == VIDEO and "link_attachment" not in first
    polled = await client.status(r, "c2")
    assert polled.status is RemoteStatus.FINISHED
    assert status.calls.last.request.url.params["fields"] == "status,error_message"
    done = await client.publish(r, "c2")
    assert done.status is RemoteStatus.PUBLISHED and done.raw["first_comment_id"] == "r2"
    reply = form(create.calls[1].request)
    assert reply == {"media_type": "TEXT", "text": "https://link.coupang.com/a/xyz", "reply_to_id": "m2",
                     "access_token": TOKEN}
    assert form(publish.calls[1].request)["creation_id"] == "rc1"


async def test_threads_enforces_500_chars_without_calling_api() -> None:
    with respx.mock, pytest.raises(ValidationFailed):  # any request would fail as unmocked
        await ThreadsClient(CFG).create(req(text="가" * 501))


@respx.mock
async def test_threads_limit_insights_delete() -> None:
    lim = respx.get(f"{TH}/uid/threads_publishing_limit").mock(return_value=httpx.Response(200, json={
        "data": [{"quota_usage": 7, "config": {"quota_total": 250, "quota_duration": 86400}}]}))
    respx.get(f"{TH}/m1/insights").mock(return_value=httpx.Response(200, json={"data": [
        {"name": "views", "period": "lifetime", "values": [{"value": 1200}]},
        {"name": "likes", "total_value": {"value": 31}}, {"name": "replies", "values": [{"value": 4}]}]}))
    respx.delete(f"{TH}/m1").mock(return_value=httpx.Response(200, json={"success": True}))
    client = ThreadsClient(CFG)
    limit = await client.publishing_limit("uid", TOKEN)
    assert limit is not None and (limit.used, limit.limit, limit.window_seconds) == (7, 250, 86400)
    assert lim.calls.last.request.url.params["fields"] == "quota_usage,config"
    ins = await client.insights("m1", TOKEN)
    assert ins.metrics == {"views": 1200.0, "likes": 31.0, "replies": 4.0}
    assert await client.hide_or_delete("m1", TOKEN) is True


# ───────────────────────────── Instagram ─────────────────────────────


@respx.mock
async def test_instagram_reels_two_phase_container() -> None:
    create = respx.post(f"{GRAPH}/uid/media").mock(return_value=httpx.Response(200, json={"id": "ig-c"}))
    respx.get(f"{GRAPH}/ig-c").mock(side_effect=[httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
                                                 httpx.Response(200, json={"status_code": "FINISHED"})])
    publish = respx.post(f"{GRAPH}/uid/media_publish").mock(return_value=httpx.Response(200, json={"id": "ig-m"}))
    respx.get(f"{GRAPH}/ig-m").mock(return_value=httpx.Response(200, json={"permalink": "https://ig/p/1"}))
    client = InstagramReelsClient(CFG)
    r = req(video_url=VIDEO)
    created = await client.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS and created.container_id == "ig-c"
    sent = form(create.calls.last.request)
    assert sent["media_type"] == "REELS" and sent["share_to_feed"] == "true" and sent["video_url"] == VIDEO
    assert sent["caption"] == r.text and sent["thumb_offset"] == "1000"
    assert (await client.status(r, "ig-c")).status is RemoteStatus.IN_PROGRESS
    assert (await client.status(r, "ig-c")).status is RemoteStatus.FINISHED
    done = await client.publish(r, "ig-c")
    assert done.status is RemoteStatus.PUBLISHED and done.post_id == "ig-m" and done.permalink == "https://ig/p/1"
    assert form(publish.calls.last.request)["creation_id"] == "ig-c"


@respx.mock
async def test_instagram_limits_insights_and_no_fake_delete() -> None:
    respx.get(f"{GRAPH}/uid/content_publishing_limit").mock(return_value=httpx.Response(200, json={
        "data": [{"quota_usage": 3, "config": {"quota_total": 100, "quota_duration": 86400}}]}))
    ins = respx.get(f"{GRAPH}/ig-m/insights").mock(return_value=httpx.Response(200, json={"data": [
        {"name": "views", "values": [{"value": 900}]}, {"name": "reach", "values": [{"value": 700}]},
        {"name": "saved", "values": [{"value": 12}]}]}))
    client = InstagramReelsClient(CFG)
    limit = await client.publishing_limit("uid", TOKEN)
    assert limit is not None and (limit.used, limit.limit) == (3, 100)
    assert (await client.insights("ig-m", TOKEN)).metrics == {"views": 900.0, "reach": 700.0, "saved": 12.0}
    assert ins.calls.last.request.url.params["metric"] == "views,reach,likes,comments,shares,saved"
    assert await client.hide_or_delete("ig-m", TOKEN) is False  # unsupported by the API, no HTTP call made
    with pytest.raises(ValidationFailed):
        await client.create(req())  # Reels need a video_url
    with pytest.raises(ValidationFailed):
        await client.create(req(video_url=VIDEO, text="x" * 2201))


# ───────────────────────────── Facebook ─────────────────────────────


@respx.mock
async def test_facebook_reels_three_step_upload() -> None:
    reels = respx.post(f"{GRAPH}/uid/video_reels").mock(side_effect=[
        httpx.Response(200, json={"video_id": "v1", "upload_url": "https://rupload.facebook.com/x"}),
        httpx.Response(200, json={"success": True})])
    rupload = respx.post(f"{CFG.rupload_base}/video-upload/{CFG.graph_version}/v1").mock(
        return_value=httpx.Response(200, json={"success": True}))
    status = respx.get(f"{GRAPH}/v1").mock(return_value=httpx.Response(200, json={"status": {
        "video_status": "ready", "uploading_phase": {"status": "complete"},
        "processing_phase": {"status": "complete"},
        "publishing_phase": {"status": "complete", "publish_status": "published"}}}))
    client = FacebookReelsClient(CFG)
    r = req(video_url=VIDEO)
    created = await client.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS and created.container_id == "v1"
    start, finish = form(reels.calls[0].request), form(reels.calls[1].request)
    assert start["upload_phase"] == "start"
    assert finish["upload_phase"] == "finish" and finish["video_id"] == "v1"
    assert finish["video_state"] == "PUBLISHED" and finish["description"] == r.text
    up = rupload.calls.last.request
    assert up.headers["Authorization"] == f"OAuth {TOKEN}" and up.headers["file_url"] == VIDEO
    done = await client.status(r, "v1")
    assert done.status is RemoteStatus.PUBLISHED and done.post_id == "v1"
    assert status.calls.last.request.url.params["fields"] == "status"


@respx.mock
async def test_facebook_status_error_and_delete() -> None:
    respx.get(f"{GRAPH}/v9").mock(return_value=httpx.Response(200, json={"status": {
        "video_status": "processing", "processing_phase": {"status": "error"}}}))
    respx.delete(f"{GRAPH}/v9").mock(return_value=httpx.Response(200, json={"success": True}))
    client = FacebookReelsClient(CFG)
    assert (await client.status(req(), "v9")).status is RemoteStatus.ERROR
    assert await client.hide_or_delete("v9", TOKEN) is True


# ───────────────────────────── YouTube ─────────────────────────────


@respx.mock
async def test_youtube_resumable_upload_declares_synthetic_media(tmp_path: Path) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"
    init = respx.post(f"{CFG.youtube_upload_base}/videos").mock(
        return_value=httpx.Response(200, headers={"Location": session}))
    put = respx.put(session).mock(return_value=httpx.Response(200, json={
        "id": "yt1", "status": {"uploadStatus": "uploaded"}}))
    client = YouTubeShortsClient(CFG)
    r = req(video_path=str(video), title="[광고] 과자 봉지 밀봉 클립", tags=["주방꿀템", "#살림"])
    created = await client.create(r)
    assert created.status is RemoteStatus.IN_PROGRESS and created.post_id == "yt1"
    sent = init.calls.last.request
    assert sent.url.params["uploadType"] == "resumable" and sent.url.params["part"] == "snippet,status"
    assert sent.headers["X-Upload-Content-Length"] == str(video.stat().st_size)
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(sent.content)
    assert body["snippet"] == {"title": "[광고] 과자 봉지 밀봉 클립", "description": r.text,
                               "tags": ["주방꿀템", "살림"], "categoryId": "26"}
    assert body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": True}
    assert put.calls.last.request.content == video.read_bytes()


@respx.mock
async def test_youtube_status_insights_and_policy_rejection() -> None:
    respx.get(f"{CFG.youtube_base}/videos", params={"part": "status"}).mock(side_effect=[
        httpx.Response(200, json={"items": [{"status": {"uploadStatus": "processed"}}]}),
        httpx.Response(200, json={"items": [{"status": {"uploadStatus": "rejected",
                                                        "rejectionReason": "termsOfUse"}}]})])
    respx.get(f"{CFG.youtube_base}/videos", params={"part": "statistics"}).mock(return_value=httpx.Response(
        200, json={"items": [{"statistics": {"viewCount": "321", "likeCount": "9", "commentCount": "2"}}]}))
    client = YouTubeShortsClient(CFG)
    assert (await client.status(req(), "yt1")).status is RemoteStatus.PUBLISHED
    with pytest.raises(PlatformWarning):
        await client.status(req(), "yt1")
    assert (await client.insights("yt1", TOKEN)).metrics == {"views": 321.0, "likes": 9.0, "comments": 2.0}


async def test_youtube_validates_title_and_file() -> None:
    client = YouTubeShortsClient(CFG)
    with pytest.raises(ValidationFailed):
        await client.create(req(video_path="missing.mp4", title="x" * 101))
    with pytest.raises(ValidationFailed):
        await client.create(req(title="ok"))  # no local file → resumable upload impossible


# ───────────────────────────── TikTok ─────────────────────────────


def _tiktok_routes() -> tuple[respx.Route, respx.Route]:
    info = respx.post(f"{CFG.tiktok_base}/v2/post/publish/creator_info/query/").mock(return_value=httpx.Response(
        200, json={"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
                            "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False,
                            "max_video_post_duration_sec": 300},
                   "error": {"code": "ok", "message": "", "log_id": "l1"}}))
    init = respx.post(f"{CFG.tiktok_base}/v2/post/publish/video/init/").mock(return_value=httpx.Response(
        200, json={"data": {"publish_id": "v_pub_url~v2.1"}, "error": {"code": "ok", "message": ""}}))
    return info, init


@respx.mock
async def test_tiktok_unaudited_forces_self_only_and_aigc() -> None:
    info, init = _tiktok_routes()
    client = TikTokClient(CFG)  # tiktok_audited defaults to False (§7)
    created = await client.create(req(video_url=VIDEO))
    assert created.status is RemoteStatus.IN_PROGRESS and created.container_id == "v_pub_url~v2.1"
    assert created.raw["unaudited"] is True and created.raw["brand_content_deferred"] is True
    body = json.loads(init.calls.last.request.content)
    post = body["post_info"]
    assert post["privacy_level"] == "SELF_ONLY" and post["is_aigc"] is True
    assert post["brand_content_toggle"] is False and post["disable_duet"] is True
    assert body["source_info"] == {"source": "PULL_FROM_URL", "video_url": VIDEO}
    assert info.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_tiktok_audited_posts_public_with_brand_toggle_and_status() -> None:
    _, init = _tiktok_routes()
    status = respx.post(f"{CFG.tiktok_base}/v2/post/publish/status/fetch/").mock(side_effect=[
        httpx.Response(200, json={"data": {"status": "PROCESSING_DOWNLOAD"}, "error": {"code": "ok"}}),
        httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7351]},
                                  "error": {"code": "ok"}})])
    client = TikTokClient(CFG, audited=True)
    created = await client.create(req(video_url=VIDEO))
    post = json.loads(init.calls.last.request.content)["post_info"]
    assert post["privacy_level"] == "PUBLIC_TO_EVERYONE" and post["brand_content_toggle"] is True
    assert created.raw["unaudited"] is False
    assert (await client.status(req(), "v_pub_url~v2.1")).status is RemoteStatus.IN_PROGRESS
    done = await client.status(req(), "v_pub_url~v2.1")
    assert done.status is RemoteStatus.PUBLISHED and done.post_id == "7351"
    assert json.loads(status.calls.last.request.content) == {"publish_id": "v_pub_url~v2.1"}
    assert await client.hide_or_delete("7351", TOKEN) is False  # no official delete endpoint


# ───────────────────────────── error mapping ─────────────────────────────


@pytest.mark.parametrize(("status", "body", "retryable"), [
    (429, {"error": {"message": "slow down", "code": 613}}, True),
    (500, {"error": {"message": "boom", "code": 1}}, True),
    (503, {}, True),
    (400, {"error": {"message": "Invalid parameter", "code": 100}}, False),
    (400, {"error": {"message": "Application request limit reached", "code": 4}}, True),
    (401, {"error": {"message": "Error validating access token", "code": 190}}, False),
])
async def test_graph_error_mapping(status: int, body: dict[str, object], retryable: bool) -> None:
    with respx.mock:
        respx.post(f"{TH}/uid/threads").mock(return_value=httpx.Response(status, json=body))
        with pytest.raises(UpstreamError) as info:
            await ThreadsClient(CFG).create(req())
    assert info.value.retryable is retryable
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize("body", [
    {"error": {"message": "It looks like you were misusing this feature", "code": 368}},
    {"error": {"message": "Action blocked", "code": 9, "error_subcode": 2207051}},
    {"error": {"message": "We limit how often you can post to protect our community from spam", "code": 9004}},
])
async def test_graph_policy_signals_raise_platform_warning(body: dict[str, object]) -> None:
    with respx.mock:
        respx.post(f"{GRAPH}/uid/media").mock(return_value=httpx.Response(400, json=body))
        with pytest.raises(PlatformWarning):
            await InstagramReelsClient(CFG).create(req(video_url=VIDEO))


@respx.mock
async def test_tiktok_and_youtube_error_mapping() -> None:
    respx.post(f"{CFG.tiktok_base}/v2/post/publish/creator_info/query/").mock(side_effect=[
        httpx.Response(403, json={"error": {"code": "spam_risk_user_banned_from_posting", "message": "banned"}}),
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "later"}}),
        httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "bad"}})])
    tiktok = TikTokClient(CFG)
    with pytest.raises(PlatformWarning):
        await tiktok.create(req(video_url=VIDEO))
    with pytest.raises(UpstreamError) as rate:
        await tiktok.create(req(video_url=VIDEO))
    assert rate.value.retryable is True
    with pytest.raises(UpstreamError) as auth:
        await tiktok.create(req(video_url=VIDEO))
    assert auth.value.retryable is False
    respx.get(f"{CFG.youtube_base}/videos").mock(return_value=httpx.Response(403, json={"error": {
        "code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(UpstreamError) as quota:
        await YouTubeShortsClient(CFG).insights("yt1", TOKEN)
    assert quota.value.retryable is True


@respx.mock
async def test_network_failure_is_retryable() -> None:
    respx.post(f"{TH}/uid/threads").mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(UpstreamError) as info:
        await ThreadsClient(CFG).create(req())
    assert info.value.retryable is True and info.value.status is None


@respx.mock
async def test_tokens_never_reach_the_httpx_log(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(f"{TH}/uid/threads_publishing_limit").mock(return_value=httpx.Response(200, json={"data": []}))
    caplog.set_level(logging.INFO, logger="httpx")
    await ThreadsClient(CFG).publishing_limit("uid", TOKEN)
    assert "threads_publishing_limit" in caplog.text
    assert TOKEN not in caplog.text and "access_token=***" in caplog.text


async def test_kakao_manual_client_never_posts() -> None:
    with respx.mock:  # no routes: any HTTP call would fail the test
        client = ManualQueueClient()
        created = await client.create(req(idempotency_key="c:kakao:a"))
        assert created.status is RemoteStatus.IN_PROGRESS and created.container_id == "manual:c:kakao:a"
        assert (await client.publish(req(), created.container_id)).status is RemoteStatus.IN_PROGRESS
        assert await client.hide_or_delete("x", "") is False
        assert (await client.insights("x", "")).metrics == {}
        assert await client.publishing_limit("uid", "") is None
