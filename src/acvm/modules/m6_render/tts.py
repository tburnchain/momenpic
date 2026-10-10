"""M6 TTS entry points (§4 M6 "TTS(페르소나별 보이스 매핑) → 강제 정렬").

The providers live in :mod:`acvm.media.tts` so the Studio API and M5 can use the same selection logic
(voice previews, provider status) without importing this module; M6 re-exports them here as its public TTS
surface.
"""

from __future__ import annotations

from acvm.media.korean import estimate_syllables, sino_korean
from acvm.media.sapi import WindowsSapiTTS, korean_sapi_voice, sapi_voices
from acvm.media.tts import (
    PREFERENCE_KEY,
    PROVIDERS,
    ElevenLabsTTS,
    TimedToneTTS,
    Yap1000Bridge,
    build_tts,
    chars_to_word_tokens,
    regroup_tokens,
    synthesize_preview,
    tts_status,
)

_sino_korean = sino_korean

__all__ = [
    "PREFERENCE_KEY",
    "PROVIDERS",
    "ElevenLabsTTS",
    "TimedToneTTS",
    "WindowsSapiTTS",
    "Yap1000Bridge",
    "build_tts",
    "chars_to_word_tokens",
    "estimate_syllables",
    "korean_sapi_voice",
    "regroup_tokens",
    "sapi_voices",
    "synthesize_preview",
    "tts_status",
]
