"""Windows SAPI provider: SSML construction, word alignment and (on Windows with a Korean voice) real synthesis."""

from __future__ import annotations

import itertools
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from acvm.media.sapi import (
    WindowsSapiTTS,
    align_words,
    build_ssml,
    engine_rate,
    engine_speed,
    korean_sapi_voice,
    parse_progress,
)
from acvm.media.voices import VOICE_CATALOG
from acvm.modules.m6_render.timeline import speaking_rate


def test_build_ssml_escapes_and_tracks_utf16_spans() -> None:
    ssml, words, spans = build_ssml(["가격 <특가> & 'AI'", "다음 😀문장"], pitch_st=-3)
    assert words == ["가격", "<특가>", "&", "'AI'", "다음", "😀문장"]
    assert "pitch='-3st'" in ssml and "<break time='350ms'/>" in ssml
    assert "rate=" not in ssml  # System.Speech ignores relative prosody rates; speed is the engine Rate
    assert "&lt;특가&gt;" in ssml and "&amp;" in ssml and "&apos;AI&apos;" in ssml
    u16 = ssml.encode("utf-16-le")
    for w, (a, b) in zip(words, spans, strict=True):
        piece = u16[2 * a:2 * b].decode("utf-16-le")
        assert piece.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&apos;", "'") == w


def test_engine_rate_makes_every_catalog_voice_fast() -> None:
    assert engine_rate(1.0) == 3 and engine_speed(0) == 1.0  # natural pace (6.5) is already above Heami (4.7)
    assert engine_speed(10) == pytest.approx(3.0)
    assert engine_rate(5.0) == 10 and engine_rate(0.01) == -10  # clamped to SAPI's range
    for v in VOICE_CATALOG:  # Heami ≈ 4.7 syl/s at Rate 0 → every fast voice lands at ≥ 7.5 syl/s
        step = engine_rate(v.rate)
        assert step >= 4 and 4.7 * engine_speed(step) >= 7.5, (v.id, step)
    assert [engine_rate(r) for r in (1.2, 1.25, 1.3)] == sorted(engine_rate(r) for r in (1.2, 1.25, 1.3))


def test_align_words_from_events_and_fallback() -> None:
    _ssml, words, spans = build_ssml(["하나 둘 셋 넷"])
    events = parse_progress("\n".join(f"{a}\t{b - a}\t{ms}" for (a, b), ms in zip(spans, (100, 600, 1100), strict=False)))
    timed = align_words(words, spans, events, duration=2.4)
    assert [w.text for w in timed] == words
    assert timed[0].start == pytest.approx(0.1) and timed[1].start == pytest.approx(0.6)
    assert timed[3].start > timed[2].start  # missing event interpolated/extrapolated
    assert all(w.end > w.start for w in timed) and timed[-1].end <= 2.4
    blind = align_words(words, spans, [], duration=2.0)  # no events at all → proportional
    assert all(b.start > a.start for a, b in itertools.pairwise(blind))


@pytest.mark.skipif(sys.platform != "win32", reason="SAPI is Windows-only")
async def test_real_sapi_synthesis(tmp_path: Path) -> None:
    voice = await korean_sapi_voice()
    if voice is None:
        pytest.skip("no Korean SAPI voice installed")
    tts = WindowsSapiTTS(voice)
    chunks = ["과자 봉지, 아직도 집게로 막아두세요?", "가격은 12,900원이에요."]
    a = await tts.synthesize(chunks, tmp_path / "a.wav", "ko-F-30s-brisk")
    b = await tts.synthesize(chunks, tmp_path / "b.wav", "ko-M-40s-punchy")
    assert a.provider == "sapi" and [w.text for w in a.words] == " ".join(chunks).split()
    assert all(y.start >= x.start for x, y in zip(a.words, a.words[1:], strict=False))
    assert a.usage["events"] >= 4  # real word boundaries were captured
    with wave.open(str(a.wav_path), "rb") as fh:
        assert fh.getframerate() == 16_000 and fh.getnchannels() == 1
        pcm = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2")
    assert np.abs(pcm).max() > 2000 and a.duration_s > 2.0
    assert (tmp_path / "a.wav").read_bytes() != (tmp_path / "b.wav").read_bytes()  # pitch differs per voice
    for r in (a, b):  # the engine really is sped up: fast short-form delivery, not narration pace
        assert r.usage["engine_rate"] >= 4 and speaking_rate(r.words) >= 6.0  # Heami at Rate 0 ≈ 4.7
