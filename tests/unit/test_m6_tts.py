"""M6 TTS adapters: native timed-tone fallback and the optional YAP-1000 bridge."""

from __future__ import annotations

import itertools
import wave
from pathlib import Path

import numpy as np
import pytest

from acvm.context import AppContext
from acvm.media._yap import load_yap
from acvm.modules.m6_render.tts import (
    TimedToneTTS,
    Yap1000Bridge,
    _sino_korean,
    build_tts,
    estimate_syllables,
    regroup_tokens,
)
from acvm.ports import WordTiming

YAP_PATH = r"C:\Users\user\Documents\YAP1000gpt"
CHUNKS = ["과자 봉지, 아직도 집게로 대충 막아두세요?", "가격은 12,900원이고 20개입이에요.", "제품 정보는 프로필 링크에서 확인하세요."]
yap_available = pytest.mark.skipif(load_yap(YAP_PATH) is None, reason="YAP-1000 checkout not importable here")


def _read(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as fh:
        sr, ch = fh.getframerate(), fh.getnchannels()
        data = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    return data, sr, ch


def _monotone(words: list[WordTiming]) -> bool:
    return all(w.end > w.start for w in words) and all(b.start >= a.start for a, b in itertools.pairwise(words))


@pytest.mark.parametrize(("word", "syllables"), [("과자", 2), ("12,900원", 6), ("20개입이에요.", 7), ("11cm", 3),
                                                  ("30%", 5), ("AI", 1), ("?", 1)])
def test_estimate_syllables(word: str, syllables: int) -> None:
    assert estimate_syllables(word) == syllables


def test_sino_korean_reading() -> None:
    assert _sino_korean(12_900) == "만이천구백"
    assert _sino_korean(3_812) == "삼천팔백십이"
    assert _sino_korean(100_000_000) == "일억"
    assert _sino_korean(0) == "영"


async def test_timed_tone_is_deterministic_monotone_and_audible(tmp_path: Path) -> None:
    tts = TimedToneTTS()
    a = await tts.synthesize(CHUNKS, tmp_path / "a.wav", "ko-F-30s-brisk")
    b = await tts.synthesize(CHUNKS, tmp_path / "b.wav", "ko-F-30s-brisk")
    assert (tmp_path / "a.wav").read_bytes() == (tmp_path / "b.wav").read_bytes()
    assert a.words == b.words and a.provider == "acvm-tone" and a.voice_id == "ko-F-30s-brisk"
    assert [w.text for w in a.words] == " ".join(CHUNKS).split()
    assert _monotone(a.words)
    assert all(b2.start >= a2.end for a2, b2 in zip(a.words, a.words[1:], strict=False))
    data, sr, ch = _read(a.wav_path)
    assert (sr, ch) == (48_000, 1)
    assert a.duration_s == pytest.approx(len(data) / sr, abs=1e-3)
    assert float(np.sqrt(np.mean(data ** 2))) > 0.02  # not silence
    # fast catalog voice: ≈ 6.5 × 1.22 ≈ 7.9 syllables/s while speaking
    spoken = sum(w.end - w.start for w in a.words)
    syl = sum(estimate_syllables(w.text) for w in a.words)
    assert 7.5 <= syl / spoken <= 8.5


async def test_timed_tone_pauses_at_punctuation_and_voices_differ(tmp_path: Path) -> None:
    tts = TimedToneTTS()
    r = await tts.synthesize(["하나, 둘. 셋 넷"], tmp_path / "p.wav", "ko-M-30s-brisk")
    gaps = [b.start - a.end for a, b in zip(r.words, r.words[1:], strict=False)]
    assert gaps[1] > gaps[0] > gaps[2]  # sentence end > comma > plain word gap
    await tts.synthesize(["하나, 둘. 셋 넷"], tmp_path / "f.wav", "ko-F-20s-bright")
    assert (tmp_path / "p.wav").read_bytes() != (tmp_path / "f.wav").read_bytes()


def test_regroup_tokens_exact_and_proportional() -> None:
    toks = [{"text": "만이천구백원이고", "start": 0.0, "end": 0.9}, {"text": "이십", "start": 1.0, "end": 1.2},
            {"text": "개입", "start": 1.2, "end": 1.5}]
    exact = regroup_tokens(["12,900원이고", "20개입"], [1, 2], toks)
    assert exact == [WordTiming("12,900원이고", 0.0, 0.9), WordTiming("20개입", 1.0, 1.5)]
    approx = regroup_tokens(["가격은", "12,900원이고", "20개입"], [1, 1, 2], toks)  # count mismatch → interpolate
    assert [w.text for w in approx] == ["가격은", "12,900원이고", "20개입"]
    assert _monotone(approx) and approx[-1].end == pytest.approx(1.5)


async def test_build_tts_order_preference_and_cache(ctx: AppContext) -> None:
    from acvm.media.sapi import korean_sapi_voice

    auto = await build_tts(ctx)
    expected = "sapi" if await korean_sapi_voice() else "acvm-tone"
    assert auto.provider == expected  # no ElevenLabs key, no YAP path configured
    assert await build_tts(ctx) is auto  # cached
    ctx.extras["tts.provider"] = "acvm-tone"
    tone = await build_tts(ctx)
    assert isinstance(tone, TimedToneTTS)
    ctx.extras["tts.provider"] = "elevenlabs"  # unavailable (no key) → falls back through the order
    assert (await build_tts(ctx)).provider == expected
    sentinel = TimedToneTTS(syllables_per_second=7.0)
    ctx.tts = sentinel
    assert await build_tts(ctx) is sentinel


@yap_available
async def test_yap_bridge_mock_keeps_original_words(tmp_path: Path) -> None:
    bridge = Yap1000Bridge(YAP_PATH)
    assert bridge.provider == "yap-mock"
    res = await bridge.synthesize(CHUNKS, tmp_path / "yap.wav", "ko-F-30s-brisk")
    assert res.provider == "yap-mock" and res.wav_path.is_file()
    assert [w.text for w in res.words] == " ".join(CHUNKS).split()  # "12,900원이고" survives normalisation
    assert _monotone(res.words)
    data, sr, _ = _read(res.wav_path)
    assert sr == 48_000 and float(np.abs(data).max()) > 0.05
    assert res.usage["via"] == "yap1000"


@yap_available
async def test_build_tts_uses_yap_bridge_when_configured(ctx: AppContext) -> None:
    ctx.settings.render.yap1000_path = YAP_PATH
    ctx.extras["tts.provider"] = "yap-mock"
    tts = await build_tts(ctx)
    assert isinstance(tts, Yap1000Bridge) and tts.provider == "yap-mock"  # no ElevenLabs key in the test secrets
    await ctx.secrets.put("elevenlabs/api_key", "test-key-not-used")
    await ctx.secrets.put("elevenlabs/voice_map", '{"ko-F-30s-brisk": "voice-abc", "default": "voice-def"}')
    ctx.extras["tts.provider"] = "auto"
    keyed = await build_tts(ctx)  # a key outranks SAPI / mock
    assert isinstance(keyed, Yap1000Bridge) and keyed.provider == "elevenlabs"
    assert keyed.voice_map["ko-F-30s-brisk"] == "voice-abc"


# ───────────────────────────── Studio: status, previews, native ElevenLabs ─────────────────────────────


class _CountingTTS(TimedToneTTS):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def synthesize(self, chunks, out_wav, voice_id, *, speed=1.0, directives=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return await super().synthesize(chunks, out_wav, voice_id, speed=speed, directives=directives)


async def test_tts_status_reports_provider_and_korean_note(ctx: AppContext) -> None:
    from acvm.media.voices import VOICE_CATALOG
    from acvm.modules.m6_render.tts import tts_status

    ctx.extras["tts.provider"] = "acvm-tone"
    st = await tts_status(ctx)
    assert st["provider"] == "acvm-tone" and st["real_voice"] is False
    assert st["voices_available"] == [v.id for v in VOICE_CATALOG]
    assert "실제 음성 엔진 없음" in st["note"]
    ctx.extras["tts.provider"] = "auto"
    auto = await tts_status(ctx)
    if auto["provider"] == "sapi":
        assert auto["real_voice"] is True and "Heami" in auto["note"] and "ElevenLabs 키 미등록" in auto["note"]


async def test_synthesize_preview_is_cached(ctx: AppContext) -> None:
    from acvm.core.errors import ValidationFailed
    from acvm.media.voices import synthesize_preview

    counting = _CountingTTS()
    ctx.tts = counting
    data, mime = await synthesize_preview(ctx, "ko-F-20s-bright")
    assert mime == "audio/wav" and data[:4] == b"RIFF"
    again, _ = await synthesize_preview(ctx, "ko-F-20s-bright")
    assert again == data and counting.calls == 1
    stored = await ctx.objects.local_path("file://voices/preview/acvm-tone/ko-F-20s-bright.wav")
    assert stored.read_bytes() == data
    stored.unlink()  # object lost → re-synthesised, not an error
    await synthesize_preview(ctx, "ko-F-20s-bright")
    assert counting.calls == 2
    with pytest.raises(ValidationFailed):
        await synthesize_preview(ctx, "not-a-voice")


@pytest.mark.render
async def test_native_elevenlabs_with_timestamps(tmp_path: Path) -> None:
    import base64

    import httpx
    import respx

    from acvm.media.ffmpeg import run
    from acvm.modules.m6_render.tts import ElevenLabsTTS

    mp3 = tmp_path / "s.mp3"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               "sine=frequency=220:sample_rate=44100:d=1.2", "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)])
    audio_b64 = base64.b64encode(mp3.read_bytes()).decode()

    def alignment(text: str) -> dict[str, object]:
        chars = list(text)
        starts = [round(i * 0.05, 3) for i in range(len(chars))]
        return {"characters": chars, "character_start_times_seconds": starts,
                "character_end_times_seconds": [s + 0.05 for s in starts]}

    def synth(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "k-test"
        body = __import__("json").loads(request.content)
        return httpx.Response(200, json={"audio_base64": audio_b64, "alignment": alignment(body["text"])})

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.elevenlabs.io/v1/voices").mock(return_value=httpx.Response(
            200, json={"voices": [{"voice_id": "cloned-1", "category": "cloned"},
                                  {"voice_id": "premade-1", "category": "premade"}]}))
        route = mock.post(url__regex=r"https://api\.elevenlabs\.io/v1/text-to-speech/premade-1/with-timestamps.*")
        route.mock(side_effect=synth)
        tts = ElevenLabsTTS("k-test", voice_map={})
        res = await tts.synthesize(["과자 봉지 아직도", "가격은 12,900원"], tmp_path / "el.wav", "ko-F-30s-brisk")
    assert res.provider == "elevenlabs" and res.usage["voice"] == "premade-1"
    assert [w.text for w in res.words] == ["과자", "봉지", "아직도", "가격은", "12,900원"]
    assert res.words[3].start >= 1.2 - 1e-6  # second chunk offset by the first chunk's audio length
    assert res.duration_s == pytest.approx(2.4, abs=0.15)
    with respx.mock() as mock:
        mock.post(url__regex=r".*/text-to-speech/mapped-9/.*").mock(return_value=httpx.Response(401))
        from acvm.core.errors import UpstreamError

        with pytest.raises(UpstreamError) as ei:
            await ElevenLabsTTS("bad", voice_map={"ko-F-30s-brisk": "mapped-9"}).synthesize(
                ["안녕"], tmp_path / "x.wav", "ko-F-30s-brisk")
        assert ei.value.retryable is False
