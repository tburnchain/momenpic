"""Deterministic typographic cards (§4 M5 시각 소스 우선순위 최하위: ACVM 자체 생성 카드).

Cards are the visual of last resort and the visual of choice for text-only beats (hook, benefit list, CTA).
They are drawn with Pillow from a :class:`CardSpec` only — no randomness, no timestamps, no metadata — so the
same spec always yields byte-identical PNG bytes. That makes them content-addressable: the SHA-256 of the
PNG is the ledger key (``AssetRow`` is unique on sha256 + rights_source), and re-rendering the same content
re-uses the same ledger rows (§3.1 멱등).

Product placeholder cards show the product *name and price as text* plus a neutral package glyph; they never
depict or synthesise the product itself (§4 M5: 상품 실물은 AI로 생성·변형하지 않는다).

Layout rules: the top :data:`~acvm.media.safe_area.DISCLOSURE_BAND_PX` px stay a single flat colour (the
locked disclosure band sits there during the first 3 s), and all text lives inside the union safe text box
(clear of every platform's bottom strip and right action rail).
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from acvm.core.ids import canonical_json
from acvm.media.fonts import FontInfo, pil_font, resolve_font
from acvm.media.safe_area import DISCLOSURE_BAND_PX, card_text_box, union

CardKind = Literal["hook", "scene", "benefit", "cta", "product"]

CARD_VERSION = "acvm-card/1"
DEFAULT_CTA = "프로필 링크에서 확인하세요"
DEFAULT_BENEFIT_TITLE = "이런 점이 좋아요"

# (top, bottom, accent, text) — calm, high-contrast palettes; text colour ≥ 7:1 against both stops.
PALETTES: tuple[tuple[str, str, str, str], ...] = (
    ("#14213D", "#1F3A68", "#FCA311", "#FFFFFF"),
    ("#1B263B", "#415A77", "#E0B04B", "#FFFFFF"),
    ("#2B2D42", "#3D405B", "#EF8354", "#FFFFFF"),
    ("#0B3C49", "#145C6B", "#F2C14E", "#FFFFFF"),
    ("#3A0CA3", "#4A2BB5", "#FFD166", "#FFFFFF"),
    ("#22333B", "#3E5641", "#F4D35E", "#FFFFFF"),
    ("#312244", "#4D2C5E", "#FF9F1C", "#FFFFFF"),
    ("#1D3557", "#2A4D7A", "#F1FAEE", "#FFFFFF"),
)


@dataclass(frozen=True, slots=True)
class CardSpec:
    """Everything that determines a card's pixels (and therefore its SHA-256)."""

    kind: CardKind
    title: str = ""
    bullets: tuple[str, ...] = ()
    subtitle: str = ""
    price: int | None = None
    seed: str = ""
    width: int = 1080
    height: int = 1920
    text_box: tuple[int, int, int, int] | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def key(self) -> dict[str, Any]:
        """Canonical description (stored as ledger attribution / used for cache keys)."""
        return {"v": CARD_VERSION, "kind": self.kind, "title": self.title, "bullets": list(self.bullets),
                "subtitle": self.subtitle, "price": self.price, "seed": self.seed, "w": self.width,
                "h": self.height, "box": list(self.box), "extra": dict(sorted(self.extra.items()))}

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.text_box or card_text_box(union(None), self.width, self.height)

    def palette(self) -> tuple[str, str, str, str]:
        idx = int.from_bytes(hashlib.blake2b(self.seed.encode(), digest_size=4).digest(), "big") % len(PALETTES)
        return PALETTES[idx]

    @property
    def spec_hash(self) -> str:
        return hashlib.blake2b(canonical_json(self.key()), digest_size=8).hexdigest()


def _rgb(hex_rgb: str) -> tuple[int, int, int]:
    h = hex_rgb.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _background(spec: CardSpec) -> Image.Image:
    """Flat top band (calm under the disclosure overlay) then a vertical gradient."""
    top, bottom, _accent, _ = spec.palette()
    t, b = np.array(_rgb(top), dtype=np.float64), np.array(_rgb(bottom), dtype=np.float64)
    h, w = spec.height, spec.width
    band = round(DISCLOSURE_BAND_PX * h / 1920)
    ramp = np.zeros(h, dtype=np.float64)
    ramp[band:] = np.linspace(0.0, 1.0, h - band)
    rows = (t[None, :] * (1.0 - ramp[:, None]) + b[None, :] * ramp[:, None]).round().astype(np.uint8)
    arr = np.repeat(rows[:, None, :], w, axis=1)
    return Image.fromarray(arr, "RGB")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for wd in words:
        cand = f"{cur} {wd}".strip()
        if draw.textlength(cand, font=font) <= max_w or not cur:
            if draw.textlength(cand, font=font) <= max_w:
                cur = cand
                continue
            # a single word wider than the box: hard-split by characters
            piece = ""
            for ch in cand:
                if draw.textlength(piece + ch, font=font) > max_w and piece:
                    lines.append(piece)
                    piece = ch
                else:
                    piece += ch
            cur = piece
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, font_info: FontInfo, *, max_w: int, max_lines: int, start: int,
         minimum: int, bold: bool = True) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    size = start
    while True:
        font = pil_font(font_info, size, bold=bold)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= max_lines or size <= minimum:
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                last = lines[-1]
                while last and draw.textlength(last + "…", font=font) > max_w:
                    last = last[:-1]
                lines[-1] = last + "…"
            return font, lines
        size -= 4


def _line_h(font: ImageFont.FreeTypeFont) -> int:
    asc, desc = font.getmetrics()
    return int((asc + desc) * 1.12)


def _draw_block(draw: ImageDraw.ImageDraw, lines: Sequence[str], font: ImageFont.FreeTypeFont, *, cx: int, y: int,
                fill: tuple[int, int, int]) -> int:
    lh = _line_h(font)
    for i, line in enumerate(lines):
        w = draw.textlength(line, font=font)
        draw.text((round(cx - w / 2), y + i * lh), line, font=font, fill=fill)
    return y + lh * len(lines)


def _accent_bar(draw: ImageDraw.ImageDraw, cx: int, y: int, accent: tuple[int, int, int], width: int = 160) -> None:
    draw.rounded_rectangle((cx - width // 2, y, cx + width // 2, y + 14), radius=7, fill=accent)


def _check_icon(draw: ImageDraw.ImageDraw, x: int, y: int, d: int, accent: tuple[int, int, int],
                ink: tuple[int, int, int]) -> None:
    draw.ellipse((x, y, x + d, y + d), fill=accent)
    w = max(4, d // 9)
    draw.line([(x + d * 0.26, y + d * 0.52), (x + d * 0.44, y + d * 0.70), (x + d * 0.76, y + d * 0.34)],
              fill=ink, width=w, joint="curve")


def _package_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, s: int, ink: tuple[int, int, int]) -> None:
    """Neutral parcel glyph (never a depiction of the actual product)."""
    w = max(6, s // 18)
    draw.rounded_rectangle((cx - s // 2, cy - s // 3, cx + s // 2, cy + s // 2), radius=s // 12, outline=ink, width=w)
    draw.line([(cx - s // 2, cy - s // 3 + s // 5), (cx + s // 2, cy - s // 3 + s // 5)], fill=ink, width=w)
    draw.line([(cx, cy - s // 3), (cx, cy + s // 2)], fill=ink, width=w)


def _price_text(price: int) -> str:
    return f"{price:,}원"


REFERENCE_BOX_H = 953  # text-box height the type scale below is designed for (union layout, bottom captions)


def render_card_image(spec: CardSpec, font: FontInfo | None = None) -> Image.Image:
    """Draw the card described by ``spec`` (pure function of the spec and the font file).

    The type scale shrinks proportionally when the text box is shorter than the reference (e.g. captions in the
    middle of the frame leave only the upper half for card text).
    """
    fi = font or resolve_font()
    img = _background(spec)
    draw = ImageDraw.Draw(img)
    _top, _bottom, accent_hex, ink_hex = spec.palette()
    accent, ink = _rgb(accent_hex), _rgb(ink_hex)
    bx, by, bw, bh = spec.box
    cx = bx + bw // 2
    k = min(1.0, bh / REFERENCE_BOX_H)

    def sc(v: float) -> int:
        return max(1, round(v * k))

    if spec.kind in ("hook", "scene"):
        title = spec.title.strip() or " "
        tfont, lines = _fit(draw, title, fi, max_w=bw, max_lines=4, start=sc(112 if spec.kind == "hook" else 96),
                            minimum=sc(56))
        block_h = _line_h(tfont) * len(lines)
        y = by + max(0, round((bh * 0.78 - block_h) / 2))
        if spec.kind == "scene":
            _accent_bar(draw, cx, y - sc(48), accent, width=sc(96))
        end = _draw_block(draw, lines, tfont, cx=cx, y=y, fill=ink)
        if spec.kind == "hook":
            _accent_bar(draw, cx, end + sc(36), accent, width=sc(160))
        if spec.subtitle:
            sfont, slines = _fit(draw, spec.subtitle, fi, max_w=bw, max_lines=2, start=sc(52), minimum=sc(36),
                                 bold=False)
            _draw_block(draw, slines, sfont, cx=cx, y=end + sc(90), fill=ink)
    elif spec.kind == "benefit":
        title = spec.title.strip() or DEFAULT_BENEFIT_TITLE
        tfont, tlines = _fit(draw, title, fi, max_w=bw, max_lines=2, start=sc(84), minimum=sc(52))
        y = by + sc(40)
        y = _draw_block(draw, tlines, tfont, cx=cx, y=y, fill=ink)
        _accent_bar(draw, cx, y + sc(24), accent, width=sc(160))
        y += sc(110)
        bullets = [b.strip() for b in spec.bullets if b.strip()][:3]
        icon = sc(76)
        for b in bullets:
            bfont, blines = _fit(draw, b, fi, max_w=bw - icon - sc(96), max_lines=2, start=sc(60), minimum=sc(40))
            lh = _line_h(bfont)
            pill_h = max(icon + sc(40), lh * len(blines) + sc(44))
            draw.rounded_rectangle((bx, y, bx + bw, y + pill_h), radius=sc(36), outline=accent, width=max(2, sc(4)))
            _check_icon(draw, bx + sc(28), y + (pill_h - icon) // 2, icon, accent, _rgb(spec.palette()[0]))
            ty = y + (pill_h - lh * len(blines)) // 2
            for i, line in enumerate(blines):
                draw.text((bx + sc(28) + icon + sc(32), ty + i * lh), line, font=bfont, fill=ink)
            y += pill_h + sc(36)
    elif spec.kind == "cta":
        headline = spec.title.strip() or DEFAULT_CTA
        tfont, lines = _fit(draw, headline, fi, max_w=bw, max_lines=3, start=sc(96), minimum=sc(56))
        block_h = _line_h(tfont) * len(lines)
        y = by + max(0, round((bh * 0.62 - block_h) / 2))
        end = _draw_block(draw, lines, tfont, cx=cx, y=y, fill=ink)
        # downward chevron towards the profile/handle area
        ay = end + sc(70)
        draw.polygon([(cx - sc(70), ay), (cx + sc(70), ay), (cx, ay + sc(80))], fill=accent)
        if spec.subtitle:
            sfont, slines = _fit(draw, spec.subtitle, fi, max_w=bw, max_lines=2, start=sc(48), minimum=sc(34),
                                 bold=False)
            _draw_block(draw, slines, sfont, cx=cx, y=ay + sc(140), fill=ink)
    elif spec.kind == "product":
        # content sits in the lower part of the text box: the upper ~40 % is left free for the beat's
        # on-screen headline, which is overlaid at render time (product cards are not text-baked)
        top = max(by + round(bh * 0.36), by + bh - sc(575))  # bottom-anchored content block
        _package_icon(draw, cx, top + sc(80), sc(160), accent)
        name = spec.title.strip() or " "
        tfont, lines = _fit(draw, name, fi, max_w=bw, max_lines=2, start=sc(68), minimum=sc(44))
        end = _draw_block(draw, lines, tfont, cx=cx, y=top + sc(190), fill=ink)
        if spec.price is not None:
            pfont = pil_font(fi, sc(96), bold=True)
            _draw_block(draw, [_price_text(spec.price)], pfont, cx=cx, y=end + sc(40), fill=accent)
    else:  # pragma: no cover - Literal guards this
        raise ValueError(f"unknown card kind {spec.kind!r}")
    return img


def render_card(spec: CardSpec, font: FontInfo | None = None) -> bytes:
    """PNG bytes of the card. Identical spec + font file ⇒ identical bytes."""
    img = render_card_image(spec, font)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


# ───────────────────────────── convenience constructors ─────────────────────────────


def hook_card(text: str, *, seed: str, box: tuple[int, int, int, int] | None = None) -> CardSpec:
    return CardSpec(kind="hook", title=text, seed=seed, text_box=box)


def scene_card(text: str, *, seed: str, box: tuple[int, int, int, int] | None = None) -> CardSpec:
    return CardSpec(kind="scene", title=text, seed=seed, text_box=box)


def benefit_card(title: str, bullets: Sequence[str], *, seed: str,
                 box: tuple[int, int, int, int] | None = None) -> CardSpec:
    return CardSpec(kind="benefit", title=title, bullets=tuple(bullets)[:3], seed=seed, text_box=box)


def cta_card(headline: str = DEFAULT_CTA, *, subtitle: str = "", seed: str,
             box: tuple[int, int, int, int] | None = None) -> CardSpec:
    return CardSpec(kind="cta", title=headline, subtitle=subtitle, seed=seed, text_box=box)


def product_card(name: str, price: int | None, *, seed: str, box: tuple[int, int, int, int] | None = None) -> CardSpec:
    return CardSpec(kind="product", title=name, price=price, seed=seed, text_box=box)


def card_background_hex(seed: str) -> str:
    """Top colour of the palette a seed maps to (used as letterbox colour for non-9:16 product photos)."""
    return CardSpec(kind="scene", seed=seed).palette()[0]
