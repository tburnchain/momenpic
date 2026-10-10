"""Caption style presets, positions, watermark placement and previews (Studio ③ / ⑤)."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image

from acvm.media.ass import Cue, build_ass_document, disclosure_events, make_disclosure_overlay, split_cues
from acvm.media.caption_styles import (
    CAPTION_POSITIONS,
    CAPTION_STYLES,
    DEFAULT_CAPTION_STYLE,
    WATERMARK_MAX_CHARS,
    caption_band,
    caption_placement,
    caption_style,
    card_text_box_for,
    onscreen_box_for,
    render_style_preview,
    sanitize_watermark,
    watermark_anchor,
)
from acvm.media.ffmpeg import run
from acvm.media.fonts import materialize_fonts_dir, resolve_font
from acvm.media.safe_area import DISCLOSURE_BAND_PX, SAFE_AREAS, layout_for, union
from acvm.ports import WordTiming

LAYOUTS = [layout_for(union(None)), *(layout_for(sa) for sa in SAFE_AREAS.values())]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _words(text: str) -> list[WordTiming]:
    out, t = [], 3.2
    for w in text.split():
        out.append(WordTiming(w, round(t, 3), round(t + 0.35, 3)))
        t += 0.42
    return out


def test_catalog_is_complete_and_unique() -> None:
    ids = [s.id for s in CAPTION_STYLES]
    assert len(ids) >= 8 and len(set(ids)) == len(ids)
    assert DEFAULT_CAPTION_STYLE == "classic" and caption_style(None).id == "classic"
    assert all(s.label and s.description for s in CAPTION_STYLES)
    assert any(s.highlight for s in CAPTION_STYLES) and any(s.border_style == 3 for s in CAPTION_STYLES)
    with pytest.raises(KeyError):
        caption_style("nope")
    assert CAPTION_POSITIONS == ("bottom", "middle", "upper")


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda lay: lay.platform)
def test_positions_respect_band_watermark_and_ui(layout) -> None:  # type: ignore[no-untyped-def]
    for pos in CAPTION_POSITIONS:
        band = caption_band(layout, pos)
        assert band[0] >= DISCLOSURE_BAND_PX + 48  # below the disclosure band and the watermark row
        assert band[1] <= 1920 - round(layout.safe_area.bottom * 1920)  # above the bottom UI strip
        _x, y, _w, h = onscreen_box_for(layout, pos)
        assert not _overlaps((y, y + h), band), pos
        assert y >= DISCLOSURE_BAND_PX
    assert caption_placement(layout, "bottom") == (2, layout.caption_margin_v)
    assert caption_placement(layout, "middle")[0] == 5
    align, top = caption_placement(layout, "upper")
    assert align == 8 and top >= DISCLOSURE_BAND_PX + 48
    wx, wy = watermark_anchor(layout)
    assert wy > DISCLOSURE_BAND_PX and wx <= 1080 - round(layout.safe_area.right * 1080)


def test_card_text_boxes_avoid_caption_band() -> None:
    lay = layout_for(union(None))
    for pos in CAPTION_POSITIONS:
        _x, y, _w, h = card_text_box_for(pos)
        assert y >= DISCLOSURE_BAND_PX and h >= 300
        assert not _overlaps((y, y + h), caption_band(lay, pos)), pos


def test_sanitize_watermark() -> None:
    assert sanitize_watermark("  @kitchen_pick  ") == "@kitchen_pick"
    assert sanitize_watermark("a\x00b\nc\td") == "a b c d"
    assert len(sanitize_watermark("가" * 50)) == WATERMARK_MAX_CHARS
    assert sanitize_watermark(None) == ""


def test_style_previews_are_deterministic_pngs() -> None:
    seen = set()
    for s in CAPTION_STYLES:
        a, b = render_style_preview(s.id), render_style_preview(s.id)
        assert a == b
        with Image.open(io.BytesIO(a)) as im:
            assert im.size == (360, 200) and im.format == "PNG"
        seen.add(a)
    assert len(seen) == len(CAPTION_STYLES)
    with pytest.raises(KeyError):
        render_style_preview("missing")


def _doc(style: str, position: str, *, enabled: bool = True, watermark: str = "@kitchen_pick") -> str:
    lay = layout_for(union(None))
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family="X")
    words = _words("과자 봉지 아직도 이렇게 막아두세요? 이제 클립 하나면 끝이에요.")
    cues = split_cues(words)
    return build_ass_document(width=1080, height=1920, font_family="X", layout=lay, overlay=ov, captions=cues,
                              caption_style_id=style, caption_position=position, captions_enabled=enabled,
                              watermark=watermark, words=words, duration=20.0)


def test_ass_highlight_watermark_and_captions_off() -> None:
    doc = _doc("highlight_yellow", "upper")
    cap = next(line for line in doc.splitlines() if line.startswith("Style: Caption,"))
    assert cap.split(",")[18] == "8"  # upper → top-centre alignment
    assert doc.count("\\1c&H4DE1FF&") >= 5  # one emphasised word per spoken word
    wm = next(line for line in doc.splitlines() if ",Watermark," in line and line.startswith("Dialogue"))
    m = re.search(r"\\pos\((\d+),(\d+)\)", wm)
    assert m and int(m.group(2)) > DISCLOSURE_BAND_PX and wm.startswith("Dialogue: 4,0:00:00.00,0:00:20.00")
    off = _doc("classic", "bottom", enabled=False, watermark="")
    assert ",Caption," not in "\n".join(line for line in off.splitlines() if line.startswith("Dialogue"))
    assert "Watermark,," not in off
    box = _doc("box_dark", "middle")
    cap = next(line for line in box.splitlines() if line.startswith("Style: Caption,")).split(",")
    assert cap[15] == "3" and cap[5] == "&H50000000" and cap[18] == "5"  # opaque-box style, middle


def test_options_never_touch_the_disclosure_events() -> None:
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family="X")
    locked = disclosure_events(ov)
    for style in ("classic", "box_white", "mint_neon"):
        for pos in CAPTION_POSITIONS:
            doc = _doc(style, pos)
            for line in locked:
                assert line in doc
            disc = next(row for row in doc.splitlines() if row.startswith("Style: Disclosure,"))
            assert disc.split(",")[15] == "3"


@pytest.mark.render
async def test_libass_renders_every_style(tmp_path: Path) -> None:
    """Each preset (with highlight, blur, boxes) renders through ffmpeg's libass without errors and draws text."""
    font = resolve_font()
    materialize_fonts_dir(font, tmp_path / "fonts")
    lay = layout_for(union(None))
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family=font.family)
    words = _words("과자 봉지 아직도 이렇게 막아두세요?")
    cues = [Cue(3.2, 6.0, ("과자 봉지 아직도", "이렇게 막아두세요?"), 0, 4)]
    for i, s in enumerate(CAPTION_STYLES):
        doc = build_ass_document(width=1080, height=1920, font_family=font.family, layout=lay, overlay=ov,
                                 captions=cues, caption_style_id=s.id, caption_position=CAPTION_POSITIONS[i % 3],
                                 watermark="@kitchen_pick", words=words, duration=8.0)
        (tmp_path / f"s{i}.ass").write_text(doc, encoding="utf-8")
        res = await run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                         "color=c=0x808080:s=1080x1920:d=6:r=30,format=yuv420p", "-ss", "4.0",
                         "-vf", f"ass=filename=s{i}.ass:fontsdir=fonts", "-frames:v", "1", "-f", "image2pipe",
                         "-c:v", "png", "-"], cwd=tmp_path)
        with Image.open(io.BytesIO(res.stdout)) as im:
            gray = im.convert("L")
            assert gray.getextrema()[1] > 200 or gray.getextrema()[0] < 60, s.id  # glyphs/boxes were drawn
