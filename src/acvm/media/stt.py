"""Speech-to-text for Studio "동영상 대본 추출" — the operator's OWN footage only.

Extracting scripts from other people's videos would be the copy-and-reproduce pattern the design forbids
(§0 #1, §8.2); the Studio API therefore only calls this for ledger assets whose rights source is own footage.

Engines are pluggable (:class:`STTEngine`). The bundled engine uses ``faster-whisper`` when it is importable
(it is intentionally *not* a dependency); without it :func:`transcribe` raises :class:`STTUnavailable` with a
Korean explanation of how to enable it. Audio is first extracted to 16 kHz mono WAV with ffmpeg.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from acvm.core.errors import ACVMError, ValidationFailed
from acvm.media.ffmpeg import MediaError, probe, run, sibling_ffprobe

ENABLE_HINT = ("음성 인식(STT) 엔진이 설치되어 있지 않습니다. 서버에서 `uv add faster-whisper` 로 설치한 뒤 다시 "
               "시도하세요 (CPU는 small 모델·int8 권장, 환경변수 ACVM_STT_MODEL 로 모델 크기 지정).")


class STTUnavailable(ACVMError):
    """No speech-to-text engine is available in this deployment."""

    code = "stt_unavailable"


class TranscriptWord(NamedTuple):
    """Iterable (text, start, end) so API layers can serialise it as a list."""

    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    words: list[TranscriptWord] = field(default_factory=list)
    language: str = "ko"
    engine: str = ""
    duration: float = 0.0


class STTEngine(Protocol):
    name: str

    async def transcribe_wav(self, wav: Path, *, language: str) -> Transcript: ...


class FasterWhisperEngine:
    """faster-whisper (CTranslate2) with word timestamps; the model is loaded once per process."""

    name = "faster-whisper"
    _models: dict[tuple[str, str, str], Any] = {}  # noqa: RUF012 — process-wide model cache

    def __init__(self, model_size: str | None = None, *, device: str = "auto", compute_type: str = "int8") -> None:
        self.model_size = model_size or os.environ.get("ACVM_STT_MODEL", "small")
        self.device = device
        self.compute_type = compute_type

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def _model(self) -> Any:
        key = (self.model_size, self.device, self.compute_type)
        if key not in self._models:
            from faster_whisper import WhisperModel

            self._models[key] = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._models[key]

    def _run(self, wav: Path, language: str) -> Transcript:
        segments, info = self._model().transcribe(str(wav), language=language, word_timestamps=True, vad_filter=True)
        words: list[TranscriptWord] = []
        texts: list[str] = []
        for seg in segments:
            texts.append(str(seg.text).strip())
            for w in getattr(seg, "words", None) or []:
                token = str(w.word).strip()
                if token:
                    words.append(TranscriptWord(token, round(float(w.start), 3), round(float(w.end), 3)))
        return Transcript(text=" ".join(t for t in texts if t), words=words,
                          language=str(getattr(info, "language", language) or language), engine=self.name,
                          duration=float(getattr(info, "duration", 0.0) or 0.0))

    async def transcribe_wav(self, wav: Path, *, language: str) -> Transcript:
        return await asyncio.to_thread(self._run, wav, language)


def available_engines() -> list[str]:
    return ["faster-whisper"] if FasterWhisperEngine.available() else []


def default_engine() -> STTEngine | None:
    return FasterWhisperEngine() if FasterWhisperEngine.available() else None


async def transcribe(path: Path, *, language: str = "ko", engine: STTEngine | None = None,
                     ffmpeg: str = "ffmpeg") -> Transcript:
    """Transcribe the audio track of ``path`` (video or audio file). Raises STTUnavailable without an engine and
    ValidationFailed when the file has no audio."""
    eng = engine or default_engine()
    if eng is None:
        raise STTUnavailable(ENABLE_HINT)
    try:
        info = await probe(path, ffprobe=sibling_ffprobe(ffmpeg))
    except MediaError as exc:
        raise ValidationFailed("file is not readable media") from exc
    if not info.has_audio:
        raise ValidationFailed("영상에 오디오 트랙이 없어 대본을 추출할 수 없습니다")
    with tempfile.TemporaryDirectory(prefix="acvm_stt_", ignore_cleanup_errors=True) as td:
        wav = Path(td) / "audio.wav"
        await run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                   "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], time_limit=600)
        result = await eng.transcribe_wav(wav, language=language)
    if not result.duration:
        result = Transcript(result.text, result.words, result.language, result.engine, round(info.duration, 3))
    return result


def words_text(words: Sequence[TranscriptWord]) -> str:
    return " ".join(w.text for w in words)
