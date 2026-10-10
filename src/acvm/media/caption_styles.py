"""Caption style presets, caption positions and watermark placement (Studio ⑤ 자막 스타일 · 자막 위치, ③ 워터마크;
§4 M6 "자막 스타일·위치 프리셋", "플랫폼 변형: 안전영역 재배치").

* :data:`CAPTION_STYLES` — tasteful presets expressed as ASS style parameters (font size, colours with alpha,
  outline vs. opaque box, shadow, blur) plus an optional *active-word highlight* colour: karaoke-style
  emphasis driven by the TTS word timings.
* :data:`CAPTION_POSITIONS` — ``bottom`` (above the platform's bottom UI strip), ``middle`` (centre band) and
  ``upper`` (below the locked disclosure band *and* the watermark row, below every platform's top UI).
  Headline (on-screen) text and the text area of generated cards move out of the caption band accordingly.
* Watermark — small semi-transparent channel name, top-right just below the disclosure band (never inside the
  D-03 band box, never under the right action rail).
* :func:`render_style_preview` — deterministic 360×200 PNG for the style carousel.

None of this can affect the locked disclosure overlay: it has its own styles and layer (see :mod:`.ass`).
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from acvm.media.fonts import FontInfo, pil_font, resolve_font
from acvm.media.safe_area import DISCLOSURE_BAND_PX, WATERMARK_ROW_PX, Layout, layout_for, union

CaptionPosition = Literal["bottom", "middle", "upper"]
CAPTION_POSITIONS: tuple[str, ...] = ("bottom", "middle", "upper")
DEFAULT_CAPTION_STYLE = "classic"
DEFAULT_CAPTION_POSITION = "bottom"
WATERMARK_MAX_CHARS = 24
WATERMARK_FONT_SIZE = 30
CAPTION_BLOCK_PX = 180  # two caption lines at the default size, incl. outline
PREVIEW_SIZE = (360, 200)


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    """One caption preset. Colours are ``#RRGGBB``; alpha is ASS-style (0 = opaque, 255 = transparent)."""

    id: str
    label: str
    description: str
    font_size: int = 64
    primary: str = "#FFFFFF"
    primary_alpha: int = 0
    outline: str = "#000000"  # outline colour, or the box colour when border_style == 3 (libass)
    outline_alpha: int = 0
    back: str = "#000000"  # shadow colour
    back_alpha: int = 128
    border_style: Literal[1, 3] = 1  # 1 = outline + shadow, 3 = opaque box
    outline_px: float = 6.0  # outline width, or box padding for border_style 3
    shadow_px: float = 1.5
    bold: bool = True
    blur: float = 0.0  # edge blur (soft glow)
    spacing: float = 0.0
    highlight: str | None = None  # active-word colour (karaoke emphasis from word timings)

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "label": self.label, "description": self.description, "highlight": bool(self.highlight),
                "box": self.border_style == 3}


CAPTION_STYLES: tuple[CaptionStyle, ...] = (
    CaptionStyle("classic", "기본", "흰 글자 + 검정 테두리. 어떤 화면에서도 잘 읽히는 기본 자막"),
    CaptionStyle("yellow", "노랑 자막", "노란 글자 + 검정 테두리. 예능 느낌의 눈에 띄는 자막",
                 primary="#FFE14D"),
    CaptionStyle("highlight_yellow", "노랑 강조", "흰 글자에 지금 읽는 단어만 노랗게 강조",
                 highlight="#FFE14D"),
    CaptionStyle("box_dark", "반투명 박스", "반투명 검정 박스 위 흰 글자. 복잡한 배경에서도 선명",
                 outline="#000000", outline_alpha=0x50, border_style=3, outline_px=14, shadow_px=0, font_size=60),
    CaptionStyle("box_white", "흰 박스", "흰 박스 위 검정 글자. 깔끔한 정보형 자막",
                 primary="#111111", outline="#FFFFFF", border_style=3, outline_px=14, shadow_px=0, font_size=60),
    CaptionStyle("mint_neon", "민트 네온", "민트색 글자에 은은한 네온 번짐",
                 primary="#3DFFD0", outline="#00453A", outline_px=5, shadow_px=0, blur=3.0),
    CaptionStyle("pink_pop", "핑크 팝", "흰 글자 + 진한 테두리, 읽는 단어는 핑크로 톡톡",
                 outline="#2B0A1F", outline_px=7, shadow_px=2, highlight="#FF5FA8"),
    CaptionStyle("news_bar", "뉴스 바", "남색 바 위 흰 글자. 뉴스 하단 자막 느낌",
                 outline="#14213D", border_style=3, outline_px=18, shadow_px=0, font_size=56),
    CaptionStyle("minimal", "미니멀", "가는 테두리와 부드러운 그림자의 차분한 자막",
                 bold=False, outline_px=2.5, shadow_px=3, back_alpha=150, font_size=58),
)
_BY_ID = {s.id: s for s in CAPTION_STYLES}


def caption_style(style_id: str | None) -> CaptionStyle:
    """Preset by id (None/"" → default). Raises KeyError for unknown ids."""
    if not style_id:
        return _BY_ID[DEFAULT_CAPTION_STYLE]
    return _BY_ID[style_id]


# ───────────────────────────── geometry ─────────────────────────────


def upper_caption_top(layout: Layout, height: int = 1920) -> int:
    """Top edge for ``upper`` captions: below the disclosure band, the watermark row and the top UI."""
    return max(round(layout.safe_area.top * height), DISCLOSURE_BAND_PX) + WATERMARK_ROW_PX + 32


def caption_placement(layout: Layout, position: str, height: int = 1920) -> tuple[int, int]:
    """(ASS Alignment, MarginV) for captions at ``position``."""
    if position == "middle":
        return 5, 0
    if position == "upper":
        return 8, upper_caption_top(layout, height)
    return 2, layout.caption_margin_v


def caption_band(layout: Layout, position: str, height: int = 1920) -> tuple[int, int]:
    """Vertical pixel band [top, bottom) that captions may occupy at ``position``."""
    if position == "middle":
        return height // 2 - CAPTION_BLOCK_PX // 2 - 20, height // 2 + CAPTION_BLOCK_PX // 2 + 20
    if position == "upper":
        top = upper_caption_top(layout, height)
        return top, top + CAPTION_BLOCK_PX
    bottom = height - layout.caption_margin_v
    return bottom - CAPTION_BLOCK_PX, bottom


def onscreen_box_for(layout: Layout, position: str, height: int = 1920) -> tuple[int, int, int, int]:
    """Headline box that never overlaps the caption band at ``position``."""
    x, y, w, h = layout.onscreen_box
    if position == "upper":
        floor = height - round(layout.safe_area.bottom * height) - 24
        top = caption_band(layout, position, height)[1] + 40  # same top as the card text box (product cards
        return (x, top, w, max(160, min(h, floor - top)))  # keep their upper area free for this headline)
    if position == "middle":
        limit = caption_band(layout, position, height)[0] - 20
        return (x, y, w, max(160, min(h, limit - y)))
    return (x, y, w, h)


def card_text_box_for(position: str, width: int = 1080, height: int = 1920) -> tuple[int, int, int, int]:
    """Text area for generated cards (platform-agnostic union layout) clear of the caption band."""
    lay = layout_for(union(None), width, height)
    x, y, w, h = lay.card_text_box
    if position == "middle":
        band_top = caption_band(lay, position, height)[0]
        return (x, y, w, max(300, band_top - 30 - y))
    if position == "upper":
        top = caption_band(lay, position, height)[1] + 40
        floor = height - round(lay.safe_area.bottom * height) - 60
        return (x, top, w, max(300, floor - top))
    return (x, y, w, h)


def watermark_anchor(layout: Layout, width: int = 1080, height: int = 1920) -> tuple[int, int]:
    """Top-right anchor (ASS \\an9) for the watermark: just below the disclosure band / top UI, left of the
    action rail."""
    return (width - layout.caption_margin_r, max(round(layout.safe_area.top * height), DISCLOSURE_BAND_PX) + 12)


def sanitize_watermark(text: str | None) -> str:
    """Printable, single-line, ≤ :data:`WATERMARK_MAX_CHARS` characters (ASS escaping happens at authoring)."""
    if not text:
        return ""
    t = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in str(text))
    t = " ".join(t.split())
    return t[:WATERMARK_MAX_CHARS].strip()


# ───────────────────────────── previews ─────────────────────────────


def _rgb(hex_rgb: str) -> tuple[int, int, int]:
    h = hex_rgb.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgba(hex_rgb: str, ass_alpha: int) -> tuple[int, int, int, int]:
    return (*_rgb(hex_rgb), 255 - max(0, min(255, ass_alpha)))


def _preview_background(w: int, h: int) -> Image.Image:
    """Neutral 'photo-like' backdrop: warm→cool diagonal gradient, soft bokeh and vignette (fixed, no RNG)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    t = (xx / w * 0.6 + yy / h * 0.4)
    warm, cool = np.array([196, 160, 128.0]), np.array([70, 92, 120.0])
    img = warm[None, None, :] * (1 - t[..., None]) + cool[None, None, :] * t[..., None]
    for cx, cy, r, gain in ((0.18, 0.30, 0.22, 38.0), (0.72, 0.22, 0.16, 30.0), (0.86, 0.70, 0.25, 22.0),
                            (0.40, 0.78, 0.18, 18.0)):
        d = np.hypot(xx / w - cx, (yy / h - cy) * h / w)
        img += gain * np.clip(1 - d / r, 0, 1)[..., None] ** 2
    vig = 1 - 0.35 * np.clip(np.hypot(xx / w - 0.5, yy / h - 0.5) / 0.7, 0, 1) ** 2
    img *= vig[..., None]
    return Image.fromarray(np.clip(img, 0, 255).round().astype(np.uint8), "RGB")


def render_style_preview(style_id: str, *, font: FontInfo | None = None, text: str = "자막 예시입니다") -> bytes:
    """Deterministic 360×200 PNG showing ``text`` in the given caption style (KeyError for unknown ids)."""
    style = caption_style(style_id) if style_id else caption_style(None)
    fi = font or resolve_font()
    w, h = PREVIEW_SIZE
    scale = 0.62
    size = max(14, round(style.font_size * scale))
    base = _preview_background(w, h).convert("RGBA")
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    pf = pil_font(fi, size, bold=style.bold)
    words = text.split()
    widths = [draw.textlength(wd, font=pf) for wd in words]
    space = draw.textlength(" ", font=pf)
    total = sum(widths) + space * (len(words) - 1)
    x0 = (w - total) / 2
    asc, desc = pf.getmetrics()
    y0 = round(h * 0.62 - (asc + desc) / 2)
    stroke = max(0, round(style.outline_px * scale)) if style.border_style == 1 else 0
    if style.border_style == 3:
        pad = round(style.outline_px * scale)
        draw.rectangle((round(x0) - pad, y0 - pad + 2, round(x0 + total) + pad, y0 + asc + desc + pad),
                       fill=_rgba(style.outline, style.outline_alpha))
    if style.shadow_px > 0 and style.border_style == 1:
        off = max(1, round(style.shadow_px * scale))
        draw.text((x0 + off, y0 + off), text, font=pf, fill=_rgba(style.back, style.back_alpha),
                  stroke_width=stroke, stroke_fill=_rgba(style.back, style.back_alpha))
    if style.blur > 0:
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(glow).text((x0, y0), text, font=pf, fill=_rgba(style.primary, 0),
                                  stroke_width=stroke + 3, stroke_fill=_rgba(style.primary, 60))
        layer = Image.alpha_composite(layer, glow.filter(ImageFilter.GaussianBlur(style.blur * 1.5)))
        draw = ImageDraw.Draw(layer)
    x = x0
    hl_index = len(words) - 1 if style.highlight else -1
    for i, (wd, ww) in enumerate(zip(words, widths, strict=True)):
        colour = style.highlight if i == hl_index and style.highlight else style.primary
        draw.text((x, y0), wd, font=pf, fill=_rgba(colour, style.primary_alpha), stroke_width=stroke,
                  stroke_fill=_rgba(style.outline, style.outline_alpha))
        x += ww + space
    out = Image.alpha_composite(base, layer).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()
