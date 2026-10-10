"""Per-platform 9:16 safe areas (§4 M6 "플랫폼 변형: 안전영역(UI 가림 영역) 재배치").

Each platform overlays its own UI on a vertical video: a header row at the top, the caption / handle /
audio strip at the bottom and the like-comment-share action rail on the right. Text placed under those
zones is unreadable, so captions (ASS ``MarginV`` / ``MarginL`` / ``MarginR``), on-screen headlines and
the text boxes of generated cards are all derived from the rectangles here.

Fractions are of the full frame and deliberately conservative (measured on 1080×1920 screenshots of each
app's player UI, rounded *up*). ``union`` gives the layout that is safe on several platforms at once —
that is what a single master render uses when it is destined for more than one platform.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

#: Height of the locked disclosure band at the top of the frame (px at 1920 tall). Kept here because the
#: on-screen text box and the card generator must stay below it (§4 M6 대가성 오버레이).
DISCLOSURE_BAND_PX = 220
CAPTION_PADDING_PX = 24
SIDE_PADDING_PX = 16
ONSCREEN_GAP_PX = 32
#: Row under the disclosure band reserved for the channel watermark (Studio ③), kept free of headline text.
WATERMARK_ROW_PX = 48


@dataclass(frozen=True, slots=True)
class SafeArea:
    """UI-occluded margins as fractions of the frame (top/bottom of height, left/right of width)."""

    platform: str
    top: float
    bottom: float
    left: float
    right: float

    def rect(self, width: int = 1080, height: int = 1920) -> tuple[int, int, int, int]:
        """The *visible* rectangle [x, y, w, h] in pixels."""
        x = round(self.left * width)
        y = round(self.top * height)
        w = width - x - round(self.right * width)
        h = height - y - round(self.bottom * height)
        return (x, y, w, h)


SAFE_AREAS: dict[str, SafeArea] = {
    # Reels: header row ("Reels", camera) / caption+handle+audio strip / action rail.
    "instagram": SafeArea("instagram", top=0.10, bottom=0.20, left=0.05, right=0.13),
    # TikTok: "Following | For You" tabs / caption + sound + nav bar / tall action rail.
    "tiktok": SafeArea("tiktok", top=0.08, bottom=0.21, left=0.05, right=0.14),
    # YouTube Shorts: search/menu row / title + channel + subscribe / action rail.
    "youtube": SafeArea("youtube", top=0.07, bottom=0.19, left=0.05, right=0.12),
    # Facebook Reels: similar to Instagram with a slightly taller bottom strip.
    "facebook": SafeArea("facebook", top=0.08, bottom=0.21, left=0.05, right=0.13),
    # Threads: inline feed player; only small corner controls.
    "threads": SafeArea("threads", top=0.04, bottom=0.08, left=0.04, right=0.04),
}

VERTICAL_PLATFORMS: tuple[str, ...] = tuple(SAFE_AREAS)


def union(platforms: Iterable[str] | None = None) -> SafeArea:
    """Most conservative margins over ``platforms`` (all vertical platforms when None/empty)."""
    names = [p for p in (platforms or ()) if p in SAFE_AREAS] or list(VERTICAL_PLATFORMS)
    areas = [SAFE_AREAS[n] for n in names]
    label = names[0] if len(names) == 1 else "union:" + "+".join(sorted(names))
    return SafeArea(label, top=max(a.top for a in areas), bottom=max(a.bottom for a in areas),
                    left=max(a.left for a in areas), right=max(a.right for a in areas))


def safe_area_for(platform: str | None) -> SafeArea:
    """Safe area of one platform; unknown platforms (e.g. kakao) and None get the all-platform union."""
    if platform and platform in SAFE_AREAS:
        return SAFE_AREAS[platform]
    return union(None)


def caption_margin_v(sa: SafeArea, height: int = 1920) -> int:
    """ASS MarginV for bottom-centred captions: clear of the bottom UI strip plus padding."""
    return round(sa.bottom * height) + CAPTION_PADDING_PX


def caption_margins_lr(sa: SafeArea, width: int = 1080) -> tuple[int, int]:
    """ASS MarginL / MarginR: clear of the left edge and of the right action rail."""
    return (round(sa.left * width) + SIDE_PADDING_PX, round(sa.right * width) + SIDE_PADDING_PX)


def onscreen_box(sa: SafeArea, width: int = 1080, height: int = 1920) -> tuple[int, int, int, int]:
    """Box [x, y, w, h] for headline text: below the disclosure band, the watermark row and the top UI row,
    left of the action rail, and ending well above the caption zone (captions own the lower third)."""
    x = round(sa.left * width) + SIDE_PADDING_PX + 8
    y = max(round(sa.top * height), DISCLOSURE_BAND_PX) + WATERMARK_ROW_PX + ONSCREEN_GAP_PX
    right = round(sa.right * width) + SIDE_PADDING_PX + 8
    w = width - x - right
    caption_top = height - caption_margin_v(sa, height) - 2 * 90  # two caption lines above MarginV
    h = max(200, round((caption_top - y) * 0.45))
    return (x, y, w, h)


def card_text_box(sa: SafeArea, width: int = 1080, height: int = 1920) -> tuple[int, int, int, int]:
    """Box [x, y, w, h] where generated cards may place text: same horizontal limits as captions, between
    the disclosure band and the top of the caption zone."""
    x, y, w, _ = onscreen_box(sa, width, height)
    caption_top = height - caption_margin_v(sa, height) - 2 * 90
    return (x, y + 20, w, max(300, caption_top - 40 - (y + 20)))


@dataclass(frozen=True, slots=True)
class Layout:
    """Resolved pixel layout for one render (master or platform variant). JSON-serialisable via ``as_dict``."""

    platform: str
    safe_area: SafeArea
    caption_margin_v: int
    caption_margin_l: int
    caption_margin_r: int
    onscreen_box: tuple[int, int, int, int]
    card_text_box: tuple[int, int, int, int]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["onscreen_box"] = list(self.onscreen_box)
        d["card_text_box"] = list(self.card_text_box)
        return d

    def differs_from(self, other: Layout, tolerance_px: int = 24) -> bool:
        """True when this layout moves caption/headline text by more than ``tolerance_px`` vs ``other``."""
        pairs = [(self.caption_margin_v, other.caption_margin_v), (self.caption_margin_l, other.caption_margin_l),
                 (self.caption_margin_r, other.caption_margin_r)]
        pairs += list(zip(self.onscreen_box, other.onscreen_box, strict=True))
        return any(abs(a - b) > tolerance_px for a, b in pairs)


def layout_for(sa: SafeArea, width: int = 1080, height: int = 1920) -> Layout:
    ml, mr = caption_margins_lr(sa, width)
    return Layout(platform=sa.platform, safe_area=sa, caption_margin_v=caption_margin_v(sa, height),
                  caption_margin_l=ml, caption_margin_r=mr, onscreen_box=onscreen_box(sa, width, height),
                  card_text_box=card_text_box(sa, width, height))


def layout_from_dict(d: dict[str, Any]) -> Layout:
    sa = d["safe_area"]
    return Layout(platform=d["platform"], safe_area=SafeArea(**sa), caption_margin_v=int(d["caption_margin_v"]),
                  caption_margin_l=int(d["caption_margin_l"]), caption_margin_r=int(d["caption_margin_r"]),
                  onscreen_box=tuple(int(v) for v in d["onscreen_box"]),  # type: ignore[arg-type]
                  card_text_box=tuple(int(v) for v in d["card_text_box"]))  # type: ignore[arg-type]
