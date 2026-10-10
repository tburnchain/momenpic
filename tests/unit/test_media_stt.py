"""Speech-to-text plumbing for 대본 추출 (own footage only; engine optional)."""

from __future__ import annotations

from pathlib import Path

import pytest

from acvm.core.errors import ValidationFailed
from acvm.media import stt
from acvm.media.ffmpeg import probe, run
from acvm.media.stt import STTUnavailable, Transcript, TranscriptWord, transcribe


class FakeEngine:
    name = "fake"

    def __init__(self) -> None:
        self.seen: list[Path] = []

    async def transcribe_wav(self, wav: Path, *, language: str) -> Transcript:
        info = await probe(wav)
        assert info.sample_rate == 16_000 and info.channels == 1
        self.seen.append(wav)
        return Transcript("과자 봉지 밀봉 클립", [TranscriptWord("과자", 0.1, 0.4), TranscriptWord("봉지", 0.45, 0.8)],
                          language, self.name)


async def test_unavailable_engine_raises_korean_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stt, "default_engine", lambda: None)
    with pytest.raises(STTUnavailable) as ei:
        await transcribe(tmp_path / "x.mp4")
    assert "faster-whisper" in str(ei.value) and "설치" in str(ei.value)


@pytest.mark.render
async def test_transcribe_extracts_audio_and_returns_iterable_words(tmp_path: Path) -> None:
    clip = tmp_path / "own.mp4"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=s=320x568:r=30:d=2", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:d=2",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip)])
    eng = FakeEngine()
    tr = await transcribe(clip, engine=eng)
    assert tr.text and tr.engine == "fake" and tr.language == "ko" and tr.duration > 1.5
    assert next(list(w) for w in tr.words) == ["과자", 0.1, 0.4]
    mute = tmp_path / "mute.mp4"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               "testsrc2=s=320x568:r=30:d=1", "-c:v", "libx264", "-preset", "ultrafast", str(mute)])
    with pytest.raises(ValidationFailed):
        await transcribe(mute, engine=eng)


def test_engine_discovery_is_optional() -> None:
    names = stt.available_engines()
    assert names in ([], ["faster-whisper"])
    assert (stt.default_engine() is None) == (names == [])
