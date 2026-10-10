"""ASS (Advanced SubStation Alpha) authoring for 1080×1920 shorts (§4 M6 "자막 스타일·위치 프리셋",
"대가성 오버레이 필수", §8.1 D-03).

Three style families are emitted:

* ``Caption`` — bold white with a black outline, bottom-centred above the platform's bottom UI strip and
  left of the action rail (margins come from :mod:`acvm.media.safe_area`).
* ``OnScreen`` — headline text (benefit lists, hook lines) in the upper safe box.
* ``DisclosureBand`` + ``Disclosure`` — the **locked** disclosure: a full-width opaque band drawn as an ASS
  vector shape plus the disclosure line in an opaque box (``BorderStyle=3``). Because every pixel inside the
  band is painted opaquely, the band's appearance is independent of the video underneath; this is what lets
  :mod:`acvm.media.verify` prove its presence by comparing decoded frames with a reference rendering.

Caption cues are cut from word timings under the house rules: ≤ 16 characters per line, ≤ 2 lines, ≥ 0.6 s
on screen, ≤ 12 characters per second, and breaks only on 어절 (space-delimited word) boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from acvm.media.caption_styles import (
    CAPTION_POSITIONS,
    WATERMARK_FONT_SIZE,
    CaptionStyle,
    caption_placement,
    caption_style,
    sanitize_watermark,
    watermark_anchor,
)
from acvm.media.safe_area import DISCLOSURE_BAND_PX, Layout
from acvm.ports import WordTiming

DISCLOSURE_STYLE_VERSION = "acvm-disclosure/1"
DISCLOSURE_BG = "#111111"
DISCLOSURE_FG = "#FFFFFF"
DISCLOSURE_BOX_PAD = 8  # BorderStyle=3 box padding (ASS Outline value)
DISCLOSURE_MAX_FONT = 50
DISCLOSURE_MIN_FONT = 30
CAPTION_FONT_SIZE = 64
ONSCREEN_FONT_SIZE = 70

_SENTENCE_END = re.compile(r"[.!?…。！？]['\"”’)]*$")
_CLAUSE_END = re.compile(r"[,，;:]['\"”’)]*$")


# ───────────────────────────── primitives ─────────────────────────────


def ass_time(sec: float) -> str:
    """Seconds → ``H:MM:SS.cc`` (ASS uses centiseconds)."""
    cs = max(0, round(sec * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass_text(text: str) -> str:
    """Make arbitrary text safe for an ASS ``Text`` field.

    ``{``/``}`` would open override blocks and ``\\`` starts escapes (``\\N``, ``\\h`` …), so they are mapped
    to visually equivalent full-width forms; line breaks become ``\\N``; other control characters are dropped.
    """
    t = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(ch for ch in t if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    t = t.replace("\t", " ")
    return "\\N".join(t.split("\n"))


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """``#RRGGBB`` + alpha (0 = opaque, 255 = transparent) → ``&HAABBGGRR``."""
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected #RRGGBB, got {hex_rgb!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _font_name(family: str) -> str:
    return family.replace(",", " ").strip() or "Sans"


# ───────────────────────────── caption cues ─────────────────────────────


@dataclass(frozen=True, slots=True)
class CaptionRules:
    """House caption rules (§4 M6 자막 프리셋)."""

    max_line_chars: int = 16
    max_lines: int = 2
    min_cue_sec: float = 0.6
    max_cps: float = 12.0
    soft_break_chars: int = 9  # break after a comma once the cue holds this many characters
    pause_break_sec: float = 0.55  # a pause this long always starts a new cue
    gap_fill_sec: float = 0.35  # gaps shorter than this are closed by extending the previous cue


@dataclass(frozen=True, slots=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]
    first_word: int = 0
    last_word: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def chars(self) -> int:
        """Characters excluding spaces (the unit of the cps rule)."""
        return sum(len(line.replace(" ", "")) for line in self.lines)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "lines": list(self.lines),
                "words": [self.first_word, self.last_word]}


@dataclass(frozen=True, slots=True)
class _Tok:
    text: str
    start: float
    end: float
    orig: int


def layout_lines(tokens: Sequence[str], max_line_chars: int = 16, max_lines: int = 2) -> list[str] | None:
    """Lay 어절 tokens out into ≤ ``max_lines`` lines of ≤ ``max_line_chars`` characters (spaces count).

    Returns None when impossible. Two-line layouts are balanced; on ties the bottom line is the longer one
    (pyramid shape reads faster)."""
    if not tokens:
        return []
    one = " ".join(tokens)
    if len(one) <= max_line_chars:
        return [one]
    if max_lines < 2:
        return None
    best: tuple[float, list[str]] | None = None
    for k in range(1, len(tokens)):
        a, b = " ".join(tokens[:k]), " ".join(tokens[k:])
        if len(a) > max_line_chars:
            break
        if len(b) > max_line_chars:
            continue
        score = max(len(a), len(b)) + (0.25 if len(a) > len(b) else 0.0)
        if best is None or score < best[0]:
            best = (score, [a, b])
    if best is not None:
        return best[1]
    if max_lines > 2:
        for k in range(1, len(tokens)):
            head = " ".join(tokens[:k])
            if len(head) > max_line_chars:
                break
            rest = layout_lines(tokens[k:], max_line_chars, max_lines - 1)
            if rest is not None:
                return [head, *rest]
    return None


def _explode(words: Sequence[WordTiming], max_chars: int) -> list[_Tok]:
    """Split pathological tokens longer than one line into line-sized pieces (times interpolated)."""
    out: list[_Tok] = []
    for i, w in enumerate(words):
        text = w.text.strip()
        if not text:
            continue
        if len(text) <= max_chars:
            out.append(_Tok(text, w.start, w.end, i))
            continue
        pieces = [text[k:k + max_chars] for k in range(0, len(text), max_chars)]
        span = max(w.end - w.start, 1e-3)
        acc = 0
        for p in pieces:
            s = w.start + span * acc / len(text)
            acc += len(p)
            out.append(_Tok(p, s, w.start + span * acc / len(text), i))
    return out


def _nonspace(texts: Sequence[str]) -> int:
    return sum(len(t.replace(" ", "")) for t in texts)


def split_cues(words: Sequence[WordTiming], rules: CaptionRules | None = None, *,
               hard_breaks: Collection[int] = (), end_limit: float | None = None) -> list[Cue]:
    """Cut word timings into caption cues following :class:`CaptionRules`.

    ``hard_breaks`` are indices of words that must start a new cue (beat boundaries). ``end_limit`` caps the
    last cue (the timeline end). Cues never overlap; each cue's ``end`` is extended for readability (min
    duration, max cps) only into silence before the next cue.
    """
    r = rules or CaptionRules()
    toks = _explode(words, r.max_line_chars)
    hard = set(hard_breaks)
    groups: list[list[_Tok]] = []
    cur: list[_Tok] = []
    for t in toks:
        if cur:
            prev = cur[-1]
            texts = [c.text for c in cur]
            brk = (t.orig in hard and t.orig != prev.orig)
            brk = brk or bool(_SENTENCE_END.search(prev.text))
            brk = brk or (bool(_CLAUSE_END.search(prev.text)) and _nonspace(texts) >= r.soft_break_chars)
            brk = brk or (t.start - prev.end) >= r.pause_break_sec
            brk = brk or layout_lines([*texts, t.text], r.max_line_chars, r.max_lines) is None
            if brk:
                groups.append(cur)
                cur = []
        cur.append(t)
    if cur:
        groups.append(cur)
    cues = [_cue_of(g, r) for g in groups]
    cues = _enforce_timing(cues, r, end_limit)
    cues = _merge_short(cues, r)
    return _enforce_timing(cues, r, end_limit)


def _cue_of(group: Sequence[_Tok], r: CaptionRules) -> Cue:
    lines = layout_lines([t.text for t in group], r.max_line_chars, r.max_lines)
    if lines is None:  # cannot happen for groups built by split_cues; keep a safe fallback
        lines = [" ".join(t.text for t in group)[: r.max_line_chars]]
    return Cue(start=group[0].start, end=group[-1].end, lines=tuple(lines), first_word=group[0].orig,
               last_word=group[-1].orig)


def _enforce_timing(cues: list[Cue], r: CaptionRules, end_limit: float | None) -> list[Cue]:
    out: list[Cue] = []
    n = len(cues)
    for i, c in enumerate(cues):
        limit = cues[i + 1].start if i + 1 < n else (end_limit if end_limit is not None else c.end + 60.0)
        need = max(c.end, c.start + r.min_cue_sec, c.start + c.chars / r.max_cps)
        end = min(need, limit)
        if i + 1 < n and 0 < limit - end < r.gap_fill_sec:
            end = limit
        out.append(Cue(c.start, max(end, c.start + 0.01), c.lines, c.first_word, c.last_word))
    return out


def _merge_short(cues: list[Cue], r: CaptionRules) -> list[Cue]:
    """Merge cues shorter than ``min_cue_sec`` into a neighbour when the merged text still fits."""
    items = list(cues)
    changed = True
    while changed:
        changed = False
        for i, c in enumerate(items):
            if c.duration >= r.min_cue_sec - 1e-6:
                continue
            for j in (i + 1, i - 1):
                if not 0 <= j < len(items):
                    continue
                a, b = (c, items[j]) if j > i else (items[j], c)
                toks = " ".join(a.lines).split() + " ".join(b.lines).split()
                lines = layout_lines(toks, r.max_line_chars, r.max_lines)
                if lines is None:
                    continue
                merged = Cue(a.start, b.end, tuple(lines), a.first_word, b.last_word)
                lo = min(i, j)
                items[lo:lo + 2] = [merged]
                changed = True
                break
            if changed:
                break
    return items


def cue_violations(cues: Sequence[Cue], rules: CaptionRules | None = None) -> list[str]:
    """Human-readable rule violations (empty list = compliant). Used by tests and render QC."""
    r = rules or CaptionRules()
    issues: list[str] = []
    for i, c in enumerate(cues):
        if len(c.lines) > r.max_lines:
            issues.append(f"cue {i}: {len(c.lines)} lines")
        for line in c.lines:
            if len(line) > r.max_line_chars:
                issues.append(f"cue {i}: line too long ({len(line)}): {line}")
        if c.duration < r.min_cue_sec - 1e-6:
            issues.append(f"cue {i}: {c.duration:.2f}s < {r.min_cue_sec}s")
        if c.duration > 0 and c.chars / c.duration > r.max_cps + 1e-6:
            issues.append(f"cue {i}: {c.chars / c.duration:.1f} cps > {r.max_cps}")
        if i and c.start < cues[i - 1].end - 1e-6:
            issues.append(f"cue {i}: overlaps previous")
    return issues


def wrap_words(text: str, max_chars: int, max_lines: int) -> list[str]:
    """Greedy 어절 wrap for headline text; overflow is truncated with an ellipsis."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if len(cand) <= max_chars or not cur:
            cur = cand if len(cand) <= max_chars else cand[: max_chars - 1] + "…"
        else:
            lines.append(cur)
            cur = w if len(w) <= max_chars else w[: max_chars - 1] + "…"
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    consumed = " ".join(lines).replace("…", "")
    if len(lines) == max_lines and len(consumed.replace(" ", "")) < len(text.replace(" ", "")):
        last = lines[-1]
        lines[-1] = (last[: max_chars - 1] if len(last) >= max_chars else last) + "…"
    return lines


# ───────────────────────────── disclosure overlay ─────────────────────────────


def ensure_ad_label(text: str) -> str:
    """The video disclosure must say "광고" (D-03 OCR target); prepend it if a policy edit dropped it."""
    t = " ".join(text.split())
    return t if "광고" in t else f"광고 · {t}"


def make_disclosure_overlay(text: str, *, seconds: float, width: int = 1080, height: int = 1920,
                            font_family: str, measure: Any | None = None, font_file: str = "") -> dict[str, Any]:
    """Build the locked disclosure overlay spec (persisted in ``RenderMeta.disclosure_overlay``).

    ``measure(text, size) -> (width_px, line_height_px)`` estimates the libass rendered extent for font-size
    fitting; without it a conservative per-character estimate is used. The band always spans the full width
    and the top :data:`DISCLOSURE_BAND_PX` px (scaled for other canvas heights); text sits in its lower half,
    below the platforms' top UI row.
    """
    label = ensure_ad_label(text)
    band_h = round(DISCLOSURE_BAND_PX * height / 1920)
    max_w = width - 2 * max(48, round(width * 0.05))
    lines = [label]
    size = DISCLOSURE_MAX_FONT
    for candidate_lines in ([label], _two_line_split(label)):
        size = DISCLOSURE_MAX_FONT
        while size > DISCLOSURE_MIN_FONT and max(_text_width(ln, size, measure) for ln in candidate_lines) > max_w:
            size -= 1
        lines = candidate_lines
        if max(_text_width(ln, size, measure) for ln in candidate_lines) <= max_w:
            break
    text_y = round(band_h * (0.70 if len(lines) == 1 else 0.62))
    return {
        "text": label,
        "lines": lines,
        "start": 0.0,
        "end": float(seconds),
        "box": [0, 0, width, band_h],
        "text_pos": [width // 2, text_y],
        "bg": DISCLOSURE_BG,
        "fg": DISCLOSURE_FG,
        "font_size": int(size),
        "font_family": font_family,
        "font_file": font_file,
        "canvas": [width, height],
        "style": DISCLOSURE_STYLE_VERSION,
        "locked": True,
    }


def _two_line_split(text: str) -> list[str]:
    words = text.split()
    if len(words) < 2:
        return [text]
    best = min(range(1, len(words)),
               key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
    return [" ".join(words[:best]), " ".join(words[best:])]


def _text_width(text: str, size: int, measure: Any | None) -> float:
    if measure is not None:
        return float(measure(text, size)[0]) * 1.04
    # libass em ≈ 0.74 × Fontsize for typical CJK fonts; Hangul ≈ 1 em, spaces/punctuation ≈ 0.3 em.
    em = size * 0.74
    wide = sum(1 for ch in text if ord(ch) >= 0x1100)
    narrow = len(text) - wide
    return (wide * 1.0 + narrow * 0.45) * em * 1.04


def caption_style_line(style: CaptionStyle, font_family: str, *, alignment: int, margin_l: int, margin_r: int,
                       margin_v: int, name: str = "Caption") -> str:
    """ASS ``Style:`` row for a caption preset (libass draws BorderStyle=3 boxes in OutlineColour)."""
    font = _font_name(font_family)
    prim = ass_color(style.primary, style.primary_alpha)
    return (f"Style: {name},{font},{style.font_size},{prim},{prim},{ass_color(style.outline, style.outline_alpha)},"
            f"{ass_color(style.back, style.back_alpha)},{-1 if style.bold else 0},0,0,0,100,100,{style.spacing:g},0,"
            f"{style.border_style},{style.outline_px:g},{style.shadow_px:g},{alignment},{margin_l},{margin_r},"
            f"{margin_v},1")


def _styles_block(font_family: str, layout: Layout | None, overlay: Mapping[str, Any], *,
                  style: CaptionStyle | None = None, position: str = "bottom", height: int = 1920) -> str:
    font = _font_name(font_family)
    bg, fg = str(overlay.get("bg", DISCLOSURE_BG)), str(overlay.get("fg", DISCLOSURE_FG))
    fs = int(overlay.get("font_size", 44))
    rows = [
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
    ]
    if layout is not None:
        alignment, margin_v = caption_placement(layout, position, height)
        rows.append(caption_style_line(style or caption_style(None), font_family, alignment=alignment,
                                       margin_l=layout.caption_margin_l, margin_r=layout.caption_margin_r,
                                       margin_v=margin_v))
        rows.append(
            f"Style: OnScreen,{font},{ONSCREEN_FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00141414,&H96000000,-1,0,0,0,"
            "100,100,0,0,1,5,2,8,0,0,0,1")
        rows.append(
            f"Style: Watermark,{font},{WATERMARK_FONT_SIZE},&H60FFFFFF,&H60FFFFFF,&H90000000,&HFF000000,-1,0,0,0,"
            "100,100,0,0,1,1.5,0,9,0,0,0,1")
    rows.append(f"Style: DisclosureBand,{font},20,{ass_color(bg)},{ass_color(bg)},{ass_color(bg)},{ass_color(bg)},"
                "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")
    rows.append(f"Style: Disclosure,{font},{fs},{ass_color(fg)},{ass_color(fg)},{ass_color(bg)},{ass_color(bg)},"
                f"-1,0,0,0,100,100,0,0,3,{DISCLOSURE_BOX_PAD},0,5,0,0,0,1")
    return "\n".join(rows)


def _header(width: int, height: int) -> str:
    return "\n".join([
        "[Script Info]",
        "; generated by ACVM M6 — the Disclosure* events are template-locked (§4 M6, §8.1 D-03)",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "Kerning: yes",
    ])


_EVENTS_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


def disclosure_events(overlay: Mapping[str, Any]) -> list[str]:
    """The two locked Dialogue lines: opaque band (vector shape, layer 0) + disclosure text (layer 1)."""
    x, y, w, h = (int(v) for v in overlay["box"])
    start, end = ass_time(float(overlay["start"])), ass_time(float(overlay["end"]))
    tx, ty = (int(v) for v in overlay.get("text_pos", [x + w // 2, y + round(h * 0.7)]))
    lines = overlay.get("lines") or [overlay["text"]]
    text = "\\N".join(escape_ass_text(str(ln)) for ln in lines)
    shape = f"m 0 0 l {w} 0 {w} {h} 0 {h}"
    return [
        f"Dialogue: 0,{start},{end},DisclosureBand,,0,0,0,,{{\\an7\\pos({x},{y})\\bord0\\shad0\\p1}}{shape}{{\\p0}}",
        f"Dialogue: 1,{start},{end},Disclosure,,0,0,0,,{{\\an5\\pos({tx},{ty})}}{text}",
    ]


def _highlight_events(cue: Cue, words: Sequence[WordTiming], style: CaptionStyle, prefix: str) -> list[str] | None:
    """Karaoke-style emphasis: one event per spoken word with that word recoloured. None when the cue's words
    cannot be matched to the timings (e.g. an over-long token split across lines)."""
    if style.highlight is None or cue.last_word < cue.first_word or cue.last_word >= len(words):
        return None
    span = list(words[cue.first_word:cue.last_word + 1])
    line_words = [ln.split() for ln in cue.lines]
    flat = [w for ln in line_words for w in ln]
    if [w.text for w in span] != flat:
        return None
    hl = ass_color(style.highlight).replace("&H00", "&H", 1)
    base = ass_color(style.primary).replace("&H00", "&H", 1)
    out: list[str] = []
    for i, wt in enumerate(span):
        start = cue.start if i == 0 else max(cue.start, wt.start)
        end = cue.end if i + 1 == len(span) else min(cue.end, max(start, span[i + 1].start))
        if end - start < 0.01:
            continue
        k = 0
        rendered: list[str] = []
        for ln in line_words:
            parts = []
            for w in ln:
                esc = escape_ass_text(w)
                parts.append(f"{{\\1c{hl}&}}{esc}{{\\1c{base}&}}" if k == i else esc)
                k += 1
            rendered.append(" ".join(parts))
        body = "\\N".join(rendered)
        out.append(f"Dialogue: 2,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{prefix}{body}")
    return out


def build_ass_document(*, width: int, height: int, font_family: str, layout: Layout, overlay: Mapping[str, Any],
                       captions: Sequence[Cue] = (), onscreen: Sequence[Mapping[str, Any]] = (),
                       caption_style_id: str | None = None, caption_position: str = "bottom",
                       captions_enabled: bool = True, watermark: str = "",
                       words: Sequence[WordTiming] = (), duration: float | None = None,
                       ai_labels: Sequence[Mapping[str, Any]] = ()) -> str:
    """Full subtitle document for a render: locked disclosure + on-screen headlines + captions (+ watermark)
    (+ "AI 생성 장면" labels on scenes that show generated imagery — top-left, opposite the watermark).

    Caption style/position, the captions on/off switch and the watermark are Studio options; none of them can
    touch the locked disclosure events (layers 0–1, own styles), which are always emitted first.
    """
    if not overlay or not overlay.get("locked", False):
        raise ValueError("disclosure overlay missing or not locked — refusing to author ASS without it")
    if caption_position not in CAPTION_POSITIONS:
        raise ValueError(f"unknown caption position {caption_position!r}")
    style = caption_style(caption_style_id)
    events = [*disclosure_events(overlay)]
    band_bottom = int(overlay["box"][1]) + int(overlay["box"][3])
    for item in onscreen:
        bx, by, bw, _bh = (int(v) for v in item["box"])
        top = max(by, band_bottom + 20) if float(item["start"]) < float(overlay["end"]) else by
        text = "\\N".join(escape_ass_text(str(ln)) for ln in item["lines"])
        events.append(f"Dialogue: 3,{ass_time(float(item['start']))},{ass_time(float(item['end']))},OnScreen,,0,0,0,,"
                      f"{{\\an8\\pos({bx + bw // 2},{top})\\fad(120,120)}}{text}")
    if captions_enabled:
        prefix = f"{{\\blur{style.blur:g}}}" if style.blur > 0 else ""
        for c in captions:
            hl = _highlight_events(c, words, style, prefix) if words else None
            if hl:
                events.extend(hl)
                continue
            text = "\\N".join(escape_ass_text(line) for line in c.lines)
            events.append(f"Dialogue: 2,{ass_time(c.start)},{ass_time(c.end)},Caption,,0,0,0,,{prefix}{text}")
    mark = sanitize_watermark(watermark)
    if mark:
        end = duration if duration is not None else max([float(overlay["end"]), *(c.end for c in captions)])
        wx, wy = watermark_anchor(layout, width, height)
        wy = max(wy, band_bottom + 12)
        events.append(f"Dialogue: 4,{ass_time(0.0)},{ass_time(end)},Watermark,,0,0,0,,"
                      f"{{\\an9\\pos({wx},{wy})}}{escape_ass_text(mark)}")
    if ai_labels:
        rx, ry = watermark_anchor(layout, width, height)
        lx, ly = width - rx, max(ry, band_bottom + 12)
        for lab in ai_labels:
            text = sanitize_watermark(str(lab.get("text") or ""))
            if text:
                events.append(f"Dialogue: 4,{ass_time(float(lab['start']))},{ass_time(float(lab['end']))},Watermark,"
                              f",0,0,0,,{{\\an7\\pos({lx},{ly})}}{escape_ass_text(text)}")
    return "\n\n".join([
        _header(width, height),
        _styles_block(font_family, layout, overlay, style=style, position=caption_position, height=height),
        "\n".join(["[Events]", _EVENTS_FORMAT, *events]),
    ]) + "\n"


def disclosure_only_document(overlay: Mapping[str, Any], *, width: int, height: int, font_family: str) -> str:
    """ASS containing only the locked disclosure events (reference rendering for D-03 verification)."""
    return "\n\n".join([
        _header(width, height),
        _styles_block(font_family, None, overlay),
        "\n".join(["[Events]", _EVENTS_FORMAT, *disclosure_events(overlay)]),
    ]) + "\n"


@dataclass(frozen=True, slots=True)
class OnScreenItem:
    start: float
    end: float
    lines: tuple[str, ...]
    box: tuple[int, int, int, int]
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "lines": list(self.lines),
                "box": list(self.box), **self.extra}
