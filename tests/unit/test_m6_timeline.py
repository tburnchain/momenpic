"""M6 JSON timeline: re-timing, 20–25 s clamp, fast-delivery floor, locked disclosure, captions, variants."""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from typing import Any

import pytest

from acvm.config import GatePolicy, RenderSpec
from acvm.core.errors import ValidationFailed
from acvm.domain.models import ScriptBeat
from acvm.media.ass import CaptionRules, Cue, cue_violations
from acvm.modules.m6_render.timeline import (
    TIMELINE_SCHEMA,
    VisualRef,
    assert_renderable,
    build_timeline,
    speaking_rate,
    timeline_bytes,
)
from acvm.ports import WordTiming

SPEC = RenderSpec()
GATE = GatePolicy()


def _beats(narrations: Sequence[str]) -> list[ScriptBeat]:
    roles = ["hook", "amplify", "reveal", "benefits", "benefits", "cta"]
    visuals = ["hook_text", "problem_scene", "product_closeup", "product_in_use", "benefit_card", "cta_card"]
    out = []
    for i, n in enumerate(narrations):
        out.append(ScriptBeat(start_sec=i * 4, end_sec=i * 4 + 4, role=roles[i % 6], narration=n,  # type: ignore[arg-type]
                              on_screen_text="20개입 · 11cm · ABS" if i == 3 else "", visual=visuals[i % 6]))
    return out


def _words(beats: Sequence[ScriptBeat], *, wps: float = 2.6, start: float = 0.12) -> tuple[list[WordTiming], float]:
    t = start
    out: list[WordTiming] = []
    for b in beats:
        toks = b.narration.split()
        for k, w in enumerate(toks):
            d = 1.0 / wps
            out.append(WordTiming(w, round(t, 4), round(t + d * 0.85, 4)))
            t += d + (0.3 if k == len(toks) - 1 else 0.0)
    return out, t + 0.2


def _visuals(n: int, *, product_beats: Sequence[int] = (2, 3)) -> dict[int, VisualRef]:
    refs = {}
    for i in range(n):
        if i in product_beats:
            refs[i] = VisualRef(asset_id=f"asset-{i}", uri=f"file://assets/p{i}.jpg", kind="image",
                                rights_source="coupang_product_image", aspect=1.0)
        else:
            refs[i] = VisualRef(asset_id=f"asset-{i}", uri=f"file://assets/c{i}.png", kind="image",
                                rights_source="generated_card", text_baked=True, aspect=0.5625, generated_card=True)
    return refs


def _build(narrations: Sequence[str], **kw: Any) -> dict[str, Any]:
    beats = _beats(narrations)
    words, dur = _words(beats, **{k: kw.pop(k) for k in ("wps",) if k in kw})
    return build_timeline(content_id="c-1", template_id="problem-solution-v3", beats=beats, words=words,
                          narration_duration=dur, visuals=_visuals(len(beats)), spec=SPEC, gate=GATE,
                          font_family="Test Sans", **kw)


NORMAL = ["과자 봉지, 아직도 집게로 대충 막아두세요?", "하루만 지나도 눅눅해져서 결국 버리게 되죠.",
          "그래서 찾은 게 과자 밀봉 클립 20개입 세트예요.", "20개입이라 넉넉하고 길이 11센티라 큰 봉지도 잡혀요.",
          "리뷰도 3,812개나 있어요.", "제품 정보는 프로필 링크에서 확인하세요."]


def test_timeline_structure_and_contract() -> None:
    tl = _build(NORMAL)
    assert tl["schema"] == TIMELINE_SCHEMA
    assert tl["canvas"] == {"w": 1080, "h": 1920, "fps": 30}
    assert 20.0 <= tl["duration"] <= 25.0
    assert tl["frames"] == round(tl["duration"] * 30)
    assert_renderable(tl, SPEC)
    scenes = tl["scenes"]
    assert scenes[0]["start"] == 0.0 and sum(s["frames"] for s in scenes) == tl["frames"]
    assert all(b["start"] > a["start"] for a, b in itertools.pairwise(scenes))
    # each narrated beat's scene starts no later than its first spoken word
    for s in scenes:
        a, _ = s["words"]
        assert s["start"] <= tl["words"][a][1] + 1e-6
    # product photo (square) is letterboxed, cards cover; cards get the gentle zoom, photos a Ken Burns move
    assert scenes[2]["fit"] == "contain" and scenes[0]["fit"] == "cover"
    assert scenes[0]["kenburns"]["u0"] == 0.0 and scenes[0]["kenburns"]["s1"] <= 1.05
    for s in scenes:
        kb = s["kenburns"]
        assert kb["s0"] >= 1 + 2 * abs(kb["u0"]) - 1e-9 and kb["s1"] >= 1 + 2 * abs(kb["u1"]) - 1e-9


def test_disclosure_overlay_always_present_and_cannot_be_disabled() -> None:
    tl = _build(NORMAL, template_params={"disclosure": False, "overlays": {}, "disclosure_seconds": 0.5,
                                         "cta_tail_sec": 2.0})
    ov = tl["overlays"]["disclosure"]
    assert ov["locked"] is True and "광고" in ov["text"]
    assert ov["start"] == 0.0 and ov["end"] >= 3.0
    assert ov["text"] == GATE.video_disclosure
    assert set(tl["template"]["ignored_params"]) == {"disclosure", "disclosure_seconds", "overlays"}
    assert tl["template"]["cta_tail_sec"] == 2.0  # a legitimate tunable still applies
    weak_gate = GATE.model_copy(update={"video_disclosure": "쿠팡 파트너스 수수료", "disclosure_overlay_seconds": 1.0})
    beats = _beats(NORMAL)
    words, dur = _words(beats)
    tl2 = build_timeline(content_id="c", template_id="x", beats=beats, words=words, narration_duration=dur,
                         visuals=_visuals(6), spec=SPEC, gate=weak_gate, font_family="T")
    assert tl2["overlays"]["disclosure"]["text"].startswith("광고")
    assert tl2["overlays"]["disclosure"]["end"] == 3.0


def test_short_narration_is_padded_to_20_seconds() -> None:
    tl = _build(["짧은 훅", "문제", "상품", "장점", "리뷰", "링크 확인"], wps=4.0)  # brisk but brief
    assert tl["duration"] == pytest.approx(20.0)
    assert tl["scenes"][-1]["end"] == pytest.approx(20.0)
    assert tl["audio"]["tempo"] == 1.0


def test_long_narration_is_tightened_to_25_seconds() -> None:
    long = [" ".join([n] * 2) for n in NORMAL]  # ~29 s of speech at 2.6 words/s
    tl = _build(long)
    assert tl["duration"] <= 25.0 + 1e-9
    assert 1.0 < tl["audio"]["tempo"] <= SPEC.max_tempo
    assert tl["words"][-1][2] < tl["duration"]
    assert_renderable(tl, SPEC)
    with pytest.raises(ValidationFailed, match="too long"):  # ~42 s cannot be rescued by tempo alone
        _build([" ".join([n] * 3) for n in NORMAL])


def test_fast_delivery_is_left_alone() -> None:
    tl = _build(NORMAL, wps=3.0)
    assert tl["audio"]["tempo"] == 1.0 and tl["audio"]["speech_sps_measured"] >= SPEC.min_speech_sps


def test_slow_delivery_is_lifted_to_the_fast_target() -> None:
    tl = _build(NORMAL, wps=2.0)  # ≈ 6.5 syllables/s — narration pace, too slow for a short
    a = tl["audio"]
    assert a["speech_sps_measured"] < SPEC.min_speech_sps
    assert a["tempo"] == pytest.approx(SPEC.target_speech_sps / a["speech_sps_measured"], abs=2e-3)
    assert a["speech_sps_final"] >= SPEC.target_speech_sps - 0.01
    assert 20.0 <= tl["duration"] <= 25.0
    assert_renderable(tl, SPEC)


def test_voice_too_slow_even_with_tempo_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="too slowly"):
        _build(NORMAL, wps=1.6)


def test_speaking_rate_ignores_beat_cuts_but_not_in_sentence_drawl() -> None:
    words = [WordTiming("과자", 0.0, 0.25), WordTiming("봉지", 0.25, 0.5),  # beat 1
             WordTiming("밀봉", 2.0, 2.25), WordTiming("클립", 2.25, 2.5)]  # beat 2, after a 1.5 s cut
    assert speaking_rate(words, beat_starts={2}) == pytest.approx(8 / 1.0)
    assert speaking_rate(words) == pytest.approx(8 / 1.25)  # in-beat silence counts, capped at 0.25 s
    drawl = [WordTiming("과자", 0.0, 0.25), WordTiming("봉지", 0.75, 1.0)]  # 0.5 s gap inside one sentence
    assert speaking_rate(drawl) < speaking_rate(drawl[:1])


def test_far_too_long_narration_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        _build([" ".join([n] * 8) for n in NORMAL])


def test_captions_follow_house_rules() -> None:
    tl = _build(NORMAL)
    cues = [Cue(c["start"], c["end"], tuple(c["lines"])) for c in tl["captions"]]
    assert cue_violations(cues, CaptionRules()) == []
    assert " ".join(" ".join(c.lines) for c in cues).split() == [w[0] for w in tl["words"]]
    assert cues[-1].end <= tl["duration"]


def test_onscreen_only_where_visual_does_not_carry_text() -> None:
    tl = _build(NORMAL)
    assert [o["scene"] for o in tl["onscreen"]] == [3]
    assert tl["onscreen"][0]["lines"] == ["20개입", "11cm", "ABS"]
    assert tl["onscreen"][0]["box"][1] >= 220


def test_empty_beat_gets_silent_hold_and_audio_segments() -> None:
    narr = list(NORMAL)
    narr[4] = ""  # a silent benefit card
    tl = _build(narr)
    segs = tl["audio"]["segments"]
    assert len(segs) == 2
    hold = tl["scenes"][4]
    assert hold["words"][0] == hold["words"][1]
    assert hold["end"] - hold["start"] >= 1.0 - 1e-6
    gap = segs[1]["dst_start"] - (segs[0]["dst_start"] + segs[0]["src_end"] - segs[0]["src_start"])
    assert gap == pytest.approx(hold["end"] - hold["start"], abs=0.05)


def test_platform_variants_only_when_layout_differs() -> None:
    assert _build(NORMAL, platforms=["instagram"])["variants"] == {}
    tl = _build(NORMAL, platforms=["instagram", "threads"])
    assert set(tl["variants"]) == {"threads"}
    assert tl["variants"]["threads"]["layout"]["caption_margin_v"] < tl["layout"]["caption_margin_v"]


def test_timeline_is_json_stable_and_seeded() -> None:
    a, b = _build(NORMAL), _build(NORMAL)
    assert timeline_bytes(a) == timeline_bytes(b)
    assert json.loads(timeline_bytes(a))["duration"] == a["duration"]
    other = _build(NORMAL, seed="another-content")
    kb_a = [s["kenburns"] for s in a["scenes"]]
    kb_o = [s["kenburns"] for s in other["scenes"]]
    assert kb_a != kb_o


def test_missing_visual_is_rejected() -> None:
    beats = _beats(NORMAL)
    words, dur = _words(beats)
    vis = _visuals(6)
    vis.pop(3)
    with pytest.raises(ValidationFailed):
        build_timeline(content_id="c", template_id="x", beats=beats, words=words, narration_duration=dur,
                       visuals=vis, spec=SPEC, gate=GATE, font_family="T")
