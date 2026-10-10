"""Shared media utilities (FFmpeg wrappers, fonts, ASS authoring, caption styles, safe areas, cards, D-03
verification, voice catalog + TTS providers, Studio edit tools, STT and ledger-backed visual selection).

This package is a library, not a module (§4): M5, M6, the M7 compliance gate and the Studio API may all import
it, and it never imports ``acvm.modules``. Submodules are imported lazily by callers that need heavier pieces
(``acvm.media.tts``, ``acvm.media.stt``); the names below are the stable surface.
"""

from acvm.media.caption_styles import (
    CAPTION_POSITIONS,
    CAPTION_STYLES,
    DEFAULT_CAPTION_STYLE,
    WATERMARK_MAX_CHARS,
    CaptionStyle,
    caption_style,
    render_style_preview,
)
from acvm.media.edit import MOTIONS, cut_video, image_to_video, video_thumbnail
from acvm.media.ffmpeg import (
    LoudnessStats,
    MediaError,
    MediaInfo,
    extract_frame,
    loudness_stats,
    measure_loudness,
    probe,
    run,
    sha256_file,
)
from acvm.media.render_options import RenderOptions, parse_render_options
from acvm.media.stt import STTUnavailable, Transcript, TranscriptWord, transcribe
from acvm.media.verify import DisclosureCheck, verify_disclosure_overlay
from acvm.media.voices import (
    VOICE_CATALOG,
    VoiceProfile,
    get_voice,
    synthesize_preview,
    voice_for_persona,
    voice_id_for_persona,
)

__all__ = [
    "CAPTION_POSITIONS",
    "CAPTION_STYLES",
    "DEFAULT_CAPTION_STYLE",
    "MOTIONS",
    "VOICE_CATALOG",
    "WATERMARK_MAX_CHARS",
    "CaptionStyle",
    "DisclosureCheck",
    "LoudnessStats",
    "MediaError",
    "MediaInfo",
    "RenderOptions",
    "STTUnavailable",
    "Transcript",
    "TranscriptWord",
    "VoiceProfile",
    "caption_style",
    "cut_video",
    "extract_frame",
    "get_voice",
    "image_to_video",
    "loudness_stats",
    "measure_loudness",
    "parse_render_options",
    "probe",
    "render_style_preview",
    "run",
    "sha256_file",
    "synthesize_preview",
    "transcribe",
    "verify_disclosure_overlay",
    "video_thumbnail",
    "voice_for_persona",
    "voice_id_for_persona",
]
