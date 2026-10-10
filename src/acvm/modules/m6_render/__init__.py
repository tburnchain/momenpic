"""M6 Render Engine (§4 M6): TTS → word timings → JSON timeline → ASS → FFmpeg, YAP-1000 reuse optional."""

from acvm.modules.m6_render.render import RENDERER_NAME, FFmpegRenderer, RenderArtifacts
from acvm.modules.m6_render.service import DisclosureVerificationFailed, RenderService
from acvm.modules.m6_render.timeline import TIMELINE_SCHEMA, VisualRef, build_timeline, timeline_bytes
from acvm.modules.m6_render.tts import TimedToneTTS, Yap1000Bridge, build_tts

__all__ = [
    "RENDERER_NAME",
    "TIMELINE_SCHEMA",
    "DisclosureVerificationFailed",
    "FFmpegRenderer",
    "RenderArtifacts",
    "RenderService",
    "TimedToneTTS",
    "VisualRef",
    "Yap1000Bridge",
    "build_timeline",
    "build_tts",
    "timeline_bytes",
]
