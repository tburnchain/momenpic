"""ASS authoring: escaping, cue splitting rules (§4 M6 자막 프리셋) and the locked disclosure events (D-03)."""

from __future__ import annotations

import itertools
import re

import pytest

from acvm.media.ass import (
    CaptionRules,
    ass_color,
    ass_time,
    build_ass_document,
    cue_violations,
    disclosure_events,
    disclosure_only_document,
    ensure_ad_label,
    escape_ass_text,
    layout_lines,
    make_disclosure_overlay,
    split_cues,
    wrap_words,
)
from acvm.media.safe_area import layout_for, union
from acvm.ports import WordTiming


def _speak(text: str, *, rate: float = 6.5, start: float = 0.2) -> list[WordTiming]:
    """Word timings at ``rate`` syllables/s with punctuation pauses (like the offline TTS)."""
    out: list[WordTiming] = []
    t = start
    for w in text.split():
        d = max(0.16, len(w.strip(".,?!")) / rate)
        out.append(WordTiming(w, round(t, 3), round(t + d, 3)))
        t += d + (0.4 if w[-1] in ".?!" else 0.22 if w.endswith(",") else 0.07)
    return out


SCRIPT = ("과자 봉지, 아직도 집게로 대충 막아두세요? 하루만 지나도 눅눅해져서 결국 버리게 되죠. "
          "그래서 찾은 게 과자 밀봉 클립 20개입 세트예요. 20개입이라 넉넉하고, 길이 11센티라 큰 봉지도 한 번에 "
          "잡혀요. 제품 정보는 프로필 링크에서 확인하세요.")


def test_ass_time_format() -> None:
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3.0) == "0:00:03.00"
    assert ass_time(61.234) == "0:01:01.23"
    assert ass_time(3725.5) == "1:02:05.50"


def test_escape_ass_text_neutralises_override_syntax() -> None:
    raw = "가격{\\b1}은\\N 진짜\n둘째 줄 {끝}\r\n셋째\x07"
    esc = escape_ass_text(raw)
    assert "{" not in esc and "}" not in esc
    # the only backslashes left are the \N line breaks we produced ourselves
    assert re.sub(r"\\N", "", esc).count("\\") == 0
    assert esc.count("\\N") == 2
    assert "\x07" not in esc and "\r" not in esc
    assert "｛" in esc and "＼" in esc


def test_ass_color_is_aabbggrr() -> None:
    assert ass_color("#112233") == "&H00332211"
    assert ass_color("#FFFFFF", alpha=128) == "&H80FFFFFF"
    with pytest.raises(ValueError):
        ass_color("#123")


def test_layout_lines_respects_limits_and_word_boundaries() -> None:
    assert layout_lines(["짧은", "문장"]) == ["짧은 문장"]
    lines = layout_lines(["하루만", "지나도", "눅눅해져서", "결국", "버리게"])
    assert lines is not None and len(lines) == 2 and all(len(x) <= 16 for x in lines)
    assert " ".join(lines) == "하루만 지나도 눅눅해져서 결국 버리게"
    assert layout_lines(["가" * 10, "나" * 10, "다" * 10, "라" * 10]) is None


def test_split_cues_obeys_house_rules() -> None:
    words = _speak(SCRIPT)
    rules = CaptionRules()
    cues = split_cues(words, rules, end_limit=words[-1].end + 1.5)
    assert cues
    assert cue_violations(cues, rules) == []
    for c in cues:
        assert len(c.lines) <= 2
        assert all(len(line) <= 16 for line in c.lines)
        assert c.duration >= 0.6 - 1e-6
        assert c.chars / c.duration <= 12.0 + 1e-6
    # every word appears exactly once, in order, never split inside a 어절
    rebuilt = " ".join(" ".join(c.lines) for c in cues).split()
    assert rebuilt == [w.text for w in words]
    # monotone, non-overlapping
    assert all(b.start >= a.end - 1e-9 for a, b in itertools.pairwise(cues))


def test_split_cues_breaks_at_sentence_end_and_hard_breaks() -> None:
    words = _speak("첫 문장입니다. 둘째 문장 시작 여기 그리고 마지막")
    cues = split_cues(words, hard_breaks={6})
    texts = [" ".join(c.lines) for c in cues]
    assert texts[0] == "첫 문장입니다."
    starts = {c.first_word for c in cues}
    assert 2 in starts and 6 in starts


def test_split_cues_min_duration_by_merging() -> None:
    # very fast single words separated by tiny gaps must not produce flicker cues
    words = [WordTiming(w, i * 0.25, i * 0.25 + 0.2) for i, w in enumerate(["가", "나.", "다", "라.", "마"])]
    cues = split_cues(words, end_limit=5.0)
    assert all(c.duration >= 0.6 - 1e-6 for c in cues)


def test_overlong_word_is_split_into_line_sized_pieces() -> None:
    words = [WordTiming("https://example.com/아주긴경로입니다", 0.0, 1.5)]
    cues = split_cues(words)
    assert all(len(line) <= 16 for c in cues for line in c.lines)


def test_wrap_words_truncates_with_ellipsis() -> None:
    lines = wrap_words("이건 정말 아주 아주 긴 온스크린 헤드라인 문장이라서 잘려야 합니다", 8, 2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert all(len(x) <= 9 for x in lines)


def test_disclosure_overlay_is_locked_top_band_with_ad_label() -> None:
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family="Test Sans")
    assert ov["locked"] is True and ov["start"] == 0.0 and ov["end"] == 3.0
    assert ov["box"] == [0, 0, 1080, 220]
    assert "광고" in ov["text"]
    assert 30 <= ov["font_size"] <= 50
    # a policy text that forgot the label still gets one
    assert ensure_ad_label("쿠팡 파트너스 수수료").startswith("광고")


def test_disclosure_events_draw_opaque_band_and_boxed_text() -> None:
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family="Test Sans")
    band, text = disclosure_events(ov)
    assert band.startswith("Dialogue: 0,0:00:00.00,0:00:03.00,DisclosureBand")
    assert "\\p1}m 0 0 l 1080 0 1080 220 0 220" in band
    assert text.startswith("Dialogue: 1,0:00:00.00,0:00:03.00,Disclosure,")
    doc = disclosure_only_document(ov, width=1080, height=1920, font_family="Test Sans")
    style = next(line for line in doc.splitlines() if line.startswith("Style: Disclosure,"))
    fields = style.split(",")
    assert fields[15] == "3"  # BorderStyle=3 → opaque box
    assert fields[3] == "&H00FFFFFF" and fields[5] == "&H00111111"  # white text, dark opaque box


def test_build_ass_document_refuses_without_locked_overlay() -> None:
    lay = layout_for(union(None))
    with pytest.raises(ValueError):
        build_ass_document(width=1080, height=1920, font_family="X", layout=lay, overlay={})
    ov = make_disclosure_overlay("광고", seconds=3.0, font_family="X")
    ov["locked"] = False
    with pytest.raises(ValueError):
        build_ass_document(width=1080, height=1920, font_family="X", layout=lay, overlay=ov)


def test_build_ass_document_contains_all_layers() -> None:
    lay = layout_for(union(None))
    ov = make_disclosure_overlay("광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", seconds=3.0, font_family="X")
    cues = split_cues(_speak("첫 자막 문장입니다. 두 번째 {자막}"))
    doc = build_ass_document(width=1080, height=1920, font_family="X", layout=lay, overlay=ov, captions=cues,
                             onscreen=[{"start": 3.2, "end": 6.0, "lines": ["20개입", "11cm"],
                                        "box": list(lay.onscreen_box)}])
    assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc
    assert doc.count("Dialogue: 2,") == len(cues)
    assert "OnScreen" in doc and "DisclosureBand" in doc
    cap = next(line for line in doc.splitlines() if line.startswith("Style: Caption,"))
    assert cap.split(",")[-2] == str(lay.caption_margin_v)
    assert "{자막}" not in doc
