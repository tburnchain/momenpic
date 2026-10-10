"""Platform caption variants (§4 M3 link-placement table, §4 M8, §8.1 D-01, §7 text limits).

Rules enforced here:

* **D-01** — the standard disclosure is always the *first line*; any copy of it already present in the script
  body (M7 may have inserted one) is removed so it appears exactly once.
* **Link placement** (§4 M3): Threads → link in the body (or first comment); Facebook → link in the caption;
  Instagram / TikTok / YouTube Shorts → "프로필 링크(링크 허브)에서 확인하세요" (+ the hub URL in the YouTube
  description, where it is at least visible); Kakao (manual) → link in the text the operator copies.
* **Limits** — Threads 500, Instagram 2,200, TikTok 2,200 (UTF-16 units), YouTube title 100 / description
  5,000, Facebook 5,000. Hashtags are dropped first, then body text is truncated; the disclosure and the link
  or hub notice are never truncated.
* **No identical text across platforms** (§1 #10 "동일 파일 반복 업로드 금지", §7 spam risk): each platform
  has its own layout, CTA wording and hashtag budget, so one content yields distinct captions per platform.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from acvm.core.enums import ContentKind, Platform
from acvm.core.errors import ValidationFailed
from acvm.domain.models import AffiliateLink, Content

HUB_NOTICE = "프로필 링크(링크 허브)에서 확인하세요"
FIRST_COMMENT_NOTICE = "구매 링크는 첫 댓글에 있어요"
AD_TITLE_PREFIX = "[광고] "
ELLIPSIS = "…"

LIMITS: dict[Platform, int] = {
    Platform.THREADS: 500,
    Platform.INSTAGRAM: 2200,
    Platform.TIKTOK: 2200,
    Platform.YOUTUBE: 5000,
    Platform.FACEBOOK: 5000,
    Platform.KAKAO: 5000,
}
YOUTUBE_TITLE_LIMIT = 100
HASHTAG_BUDGET: dict[Platform, int] = {
    Platform.THREADS: 1,  # Threads supports a single topic tag per post
    Platform.INSTAGRAM: 5,
    Platform.TIKTOK: 4,
    Platform.YOUTUBE: 3,
    Platform.FACEBOOK: 3,
    Platform.KAKAO: 0,
}
DEFAULT_PLACEMENT: dict[Platform, str] = {
    Platform.THREADS: "body",
    Platform.FACEBOOK: "caption_first_line",
    Platform.INSTAGRAM: "hub",
    Platform.TIKTOK: "hub",
    Platform.YOUTUBE: "hub",
    Platform.KAKAO: "manual",
}
LINK_PLACEMENTS = frozenset({"body", "first_comment", "caption_first_line"})  # need the affiliate link
_ALLOWED: dict[Platform, frozenset[str]] = {
    Platform.THREADS: frozenset({"body", "first_comment", "hub"}),
    Platform.FACEBOOK: frozenset({"caption_first_line", "body", "hub"}),
    Platform.INSTAGRAM: frozenset({"hub"}),
    Platform.TIKTOK: frozenset({"hub"}),
    Platform.YOUTUBE: frozenset({"hub"}),
    Platform.KAKAO: frozenset({"manual", "body", "hub"}),
}


def resolve_placement(platform: Platform, requested: str | None) -> str:
    """Requested placement if the platform allows it (§4 M3), else the platform default.

    Caption links are not clickable on Instagram / TikTok / Shorts, so any link-in-caption request there is
    coerced to the hub.
    """
    if requested and requested in _ALLOWED[platform]:
        return requested
    return DEFAULT_PLACEMENT[platform]


def text_length(platform: Platform, text: str) -> int:
    if platform is Platform.TIKTOK:
        return len(text.encode("utf-16-le")) // 2
    return len(text)


def _clean(text: str, disclosures: tuple[str, ...]) -> str:
    """Drop disclosure copies (D-01 puts exactly one, first) and collapse blank-line runs."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    kept = [ln for ln in lines if not any(d and d in ln for d in disclosures)]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return out


def _hashtags(tags: list[str], budget: int, *, lead: tuple[str, ...] = ()) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (*lead, *tags):
        tag = "#" + re.sub(r"[\s#]+", "", raw)
        if len(tag) < 2 or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[: budget + len(lead)]


def _body_source(content: Content) -> str:
    s = content.script
    if content.kind is ContentKind.TEXT:
        return s.body or s.caption or s.narration
    return s.caption or s.body or s.narration


@dataclass(frozen=True, slots=True)
class _Parts:
    hook: str
    body: str
    tags: list[str]


Renderer = Callable[[_Parts], str]


def _layout(platform: Platform, placement: str, disclosure: str, link_url: str, hub_url: str) -> Renderer:
    """Platform-specific layout — the reason captions differ per platform even for the same content."""

    def para(*blocks: str) -> str:
        return "\n\n".join(b for b in blocks if b)

    def lines(*rows: str) -> str:
        return "\n".join(r for r in rows if r)

    if platform is Platform.THREADS:
        if placement == "body":
            tail = f"👉 {link_url}"
        elif placement == "first_comment":
            tail = f"👇 {FIRST_COMMENT_NOTICE}"
        else:
            tail = f"🔗 링크 허브: {hub_url}" if hub_url else f"🔗 {HUB_NOTICE}"  # clickable here
        return lambda p: para(disclosure, lines(p.hook, p.body), lines(tail, " ".join(p.tags)))
    if platform is Platform.FACEBOOK:
        hub_tail = f"▶ 링크 허브: {hub_url}" if hub_url else f"▶ {HUB_NOTICE}"
        tail = f"▶ 제품 보러 가기: {link_url}" if placement != "hub" else hub_tail
        return lambda p: lines(disclosure, para(p.hook, p.body, tail, " ".join(p.tags)))
    if platform is Platform.INSTAGRAM:
        return lambda p: para(disclosure, p.hook, p.body, f"🔗 제품 정보는 {HUB_NOTICE}", " ".join(p.tags))
    if platform is Platform.TIKTOK:
        return lambda p: lines(disclosure, " ".join(x for x in (p.hook, p.body.replace("\n", " ")) if x),
                               f"📌 {HUB_NOTICE}", " ".join(p.tags))
    if platform is Platform.YOUTUBE:
        hub_line = f"링크 허브: {hub_url}" if hub_url else ""
        return lambda p: para(disclosure, p.body or p.hook, lines(f"▶ {HUB_NOTICE}", hub_line), " ".join(p.tags))
    # Kakao manual queue: the operator pastes this text, link included
    tail = f"구매 링크: {link_url}" if placement != "hub" and link_url else f"{HUB_NOTICE}: {hub_url}".rstrip(": ")
    return lambda p: para(disclosure, lines(p.hook, p.body), tail)


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - len(ELLIPSIS))].rstrip() + ELLIPSIS


def compose_caption(content: Content, platform: Platform, link: AffiliateLink | None, placement: str,
                    disclosure: str, hub_url: str = "") -> str:
    """Platform caption (YouTube: the description). Disclosure first, link/hub notice never truncated."""
    disclosure = disclosure.strip()
    if not disclosure:
        raise ValidationFailed("standard disclosure is empty (D-01)")
    placement = resolve_placement(platform, placement)
    link_url = link.short_url if link is not None else ""
    if placement in LINK_PLACEMENTS and not link_url:
        raise ValidationFailed(f"{platform.value} placement {placement!r} needs an affiliate link")
    disclosures = tuple(d for d in (disclosure, content.script.disclosure.strip()) if d)
    hook = _clean(content.script.hook, disclosures)
    body = _clean(_body_source(content), disclosures)
    if hook and body.startswith(hook):
        body = body[len(hook):].lstrip()
    if platform is Platform.YOUTUBE:
        hook, body = hook.replace("<", "").replace(">", ""), body.replace("<", "").replace(">", "")
    lead = ("Shorts",) if platform is Platform.YOUTUBE else ()
    tags = _hashtags(content.script.hashtags, HASHTAG_BUDGET[platform], lead=lead)
    render = _layout(platform, placement, disclosure, link_url, hub_url)
    limit = LIMITS[platform]
    parts = _Parts(hook, body, tags)
    for _ in range(64):
        text = render(parts)
        overflow = text_length(platform, text) - limit
        if overflow <= 0:
            return text
        if len(parts.tags) > len(lead):
            parts = _Parts(parts.hook, parts.body, parts.tags[:-1])
        elif parts.body:
            parts = _Parts(parts.hook, _truncate(parts.body, len(parts.body) - overflow), parts.tags)
        elif parts.tags:
            parts = _Parts(parts.hook, parts.body, [])
        elif parts.hook:
            parts = _Parts(_truncate(parts.hook, len(parts.hook) - overflow), parts.body, parts.tags)
        else:
            break
    raise ValidationFailed(f"disclosure and link alone exceed the {platform.value} limit of {limit}")


def compose_title(content: Content, platform: Platform = Platform.YOUTUBE) -> str:
    """YouTube title ≤ 100 chars, "[광고]" prefix kept intact, no '<'/'>' (rejected by the API)."""
    s = content.script
    base = (s.title or s.hook or s.caption or "").replace("<", "").replace(">", "")
    base = " ".join(base.split())
    room = YOUTUBE_TITLE_LIMIT - len(AD_TITLE_PREFIX)
    return AD_TITLE_PREFIX + _truncate(base, room) if base else AD_TITLE_PREFIX.strip()


def first_comment_text(link: AffiliateLink) -> str:
    """Text of the Threads first-comment reply (the link itself)."""
    return link.short_url


def hashtag_words(content: Content) -> list[str]:
    """Plain tag words (no '#') for platforms with a separate tag field (YouTube ``snippet.tags``)."""
    return [t.lstrip("#") for t in _hashtags(content.script.hashtags, 50)]
