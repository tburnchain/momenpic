"""Caption composition rules (D-01, §4 M3 link placement, §7 limits, per-platform variants)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from acvm.config import GatePolicy
from acvm.core.enums import ContentKind, Platform
from acvm.core.errors import ValidationFailed
from acvm.domain.models import AffiliateLink, Content, Script
from acvm.modules.m8_distribution.captions import (
    DEFAULT_PLACEMENT,
    FIRST_COMMENT_NOTICE,
    HUB_NOTICE,
    LIMITS,
    compose_caption,
    compose_title,
    resolve_placement,
    text_length,
)

DISCLOSURE = GatePolicy().standard_disclosure
NOW = datetime(2026, 10, 8, tzinfo=UTC)
SHORT_URL = "https://link.coupang.com/a/bXyZ12"
HUB = "https://hub.example/hub/kitchen_pick"


def content(*, body: str = "봉지 입구가 눅눅해질 때 집게 하나면 끝.\n20개입이라 냉동실 정리까지 됩니다.",
            hook: str = "과자 봉지, 아직도 고무줄로 묶어요?", kind: ContentKind = ContentKind.SHORT,
            hashtags: list[str] | None = None, title: str = "", script_disclosure: str = "") -> Content:
    script = Script(hooks=[hook], hook_type="question", body=body if kind is ContentKind.TEXT else "",
                    caption=body if kind is ContentKind.SHORT else "", persona="30대 여성", template_id="t1",
                    hashtags=hashtags if hashtags is not None else ["주방꿀템", "#살림", "밀봉클립", "정리", "자취", "꿀팁"],
                    title=title, disclosure=script_disclosure)
    return Content(workspace_id=uuid.uuid4(), product_id=1, kind=kind, persona="30대 여성", template_id="t1",
                   script=script, created_at=NOW)


LINK = AffiliateLink(product_id=1, sub_id="a1", deeplink="https://link.coupang.com/re/x", short_url=SHORT_URL,
                     signature="s", publication_key="k", created_at=NOW)


def caption(platform: Platform, c: Content | None = None, placement: str | None = None) -> str:
    return compose_caption(c or content(), platform, LINK, placement or DEFAULT_PLACEMENT[platform], DISCLOSURE, HUB)


@pytest.mark.parametrize("platform", list(Platform))
def test_disclosure_is_always_first_line_and_appears_once(platform: Platform) -> None:
    c = content(body=f"{DISCLOSURE}\n본문 한 줄", script_disclosure=DISCLOSURE)
    text = caption(platform, c)
    assert text.split("\n", 1)[0] == DISCLOSURE
    assert text.count(DISCLOSURE) == 1


def test_link_placement_per_platform() -> None:
    threads = caption(Platform.THREADS)
    assert SHORT_URL in threads and HUB_NOTICE not in threads
    first_comment = caption(Platform.THREADS, placement="first_comment")
    assert SHORT_URL not in first_comment and FIRST_COMMENT_NOTICE in first_comment
    assert SHORT_URL in caption(Platform.FACEBOOK)
    for p in (Platform.INSTAGRAM, Platform.TIKTOK):
        text = caption(p)
        assert HUB_NOTICE in text and SHORT_URL not in text and HUB not in text
    youtube = caption(Platform.YOUTUBE)
    assert HUB_NOTICE in youtube and HUB in youtube and SHORT_URL not in youtube and "#Shorts" in youtube
    assert SHORT_URL in caption(Platform.KAKAO)


def test_placement_is_coerced_to_what_the_platform_supports() -> None:
    assert resolve_placement(Platform.INSTAGRAM, "body") == "hub"
    assert resolve_placement(Platform.TIKTOK, "caption_first_line") == "hub"
    assert resolve_placement(Platform.THREADS, "first_comment") == "first_comment"
    assert resolve_placement(Platform.FACEBOOK, None) == "caption_first_line"
    assert SHORT_URL not in caption(Platform.INSTAGRAM, placement="body")


def test_variants_differ_across_platforms() -> None:
    c = content()
    texts = {p: caption(p, c) for p in Platform}
    assert len(set(texts.values())) == len(Platform)


@pytest.mark.parametrize("platform", [p for p in Platform if p is not Platform.KAKAO])
def test_length_limits_truncate_body_never_disclosure_or_link(platform: Platform) -> None:
    long_body = "정말 편해요. " * 800
    text = caption(platform, content(body=long_body, kind=ContentKind.TEXT if platform is Platform.THREADS
                                     else ContentKind.SHORT))
    assert text_length(platform, text) <= LIMITS[platform]
    assert text.startswith(DISCLOSURE)
    assert "…" in text
    if platform in (Platform.THREADS, Platform.FACEBOOK):
        assert SHORT_URL in text
    else:
        assert HUB_NOTICE in text


def test_tiktok_counts_utf16_units() -> None:
    emoji_body = "😀" * 1500  # 3,000 UTF-16 units, 1,500 code points
    text = caption(Platform.TIKTOK, content(body=emoji_body))
    assert text_length(Platform.TIKTOK, text) <= 2200
    assert len(text) < text_length(Platform.TIKTOK, text)  # code points alone would have under-counted


def test_threads_keeps_a_single_topic_tag() -> None:
    text = caption(Platform.THREADS, content(kind=ContentKind.TEXT))
    assert text.count("#") == 1


def test_youtube_title_rules() -> None:
    title = compose_title(content(title="<최고> 밀봉 클립 " + "아주 " * 60))
    assert title.startswith("[광고] ") and len(title) <= 100 and "<" not in title and ">" not in title
    assert compose_title(content(title="", hook="짧은 훅")) == "[광고] 짧은 훅"


def test_missing_link_or_disclosure_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        compose_caption(content(), Platform.THREADS, None, "body", DISCLOSURE, HUB)
    with pytest.raises(ValidationFailed):
        compose_caption(content(), Platform.INSTAGRAM, LINK, "hub", "  ", HUB)
    # hub platforms do not need the link itself
    assert HUB_NOTICE in compose_caption(content(), Platform.INSTAGRAM, None, "hub", DISCLOSURE, HUB)
