"""Native FFmpeg renderer for ACVM timelines (§4 M6 출력 규격: 1080×1920, 30 fps, H.264, −14 LUFS, 15–45 s;
목표 성능: 30초 영상 1건 CPU 렌더 60초 이내, 동시 8건).

Pipeline (every ffmpeg call is an async subprocess; nothing goes through a shell):

1. **Video** — one ffmpeg process per output (master, and each platform variant) with a single filter graph:
   every scene is an input; images are pre-scaled 2× (cover-crop, or contain-on-colour for non-9:16 product
   photos), converted once to BT.709 yuv420p and animated with ``zoompan`` (geometric zoom curve = perceptually
   uniform Ken Burns, linear pan, exact frame count); videos are scaled/cropped, looped and trimmed. Scenes are
   ``concat``-ed, the ASS (captions, on-screen text and the locked disclosure band) is burned with libass, and the
   result is encoded **once** with libx264 (veryfast, CRF 21, yuv420p, BT.709 tags). Encoding every frame only
   once is what keeps a 30 s short well inside the 60 s CPU budget (x264 throughput is the bottleneck).
2. **Audio** (concurrently with 1) — narration → stereo 48 kHz → optional ``atempo`` → segment placement
   (silent holds for narration-less beats) → pad/trim to the timeline length → **two-pass** ``loudnorm`` to
   −14 LUFS integrated / −1.5 dBTP true peak.
3. **Mux** — video stream copied, audio encoded to AAC 48 kHz, ``+faststart``.

Bit-exact flags and fixed parameters make identical timelines produce identical files on the same build.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acvm.config import RenderSpec
from acvm.core.errors import ValidationFailed
from acvm.core.logging import get_logger
from acvm.media.ass import Cue, build_ass_document
from acvm.media.caption_styles import onscreen_box_for
from acvm.media.edit import zoompan_filter
from acvm.media.ffmpeg import MediaError, loudnorm_measure, run
from acvm.media.fonts import FontInfo, materialize_fonts_dir
from acvm.media.safe_area import Layout, layout_from_dict
from acvm.modules.m6_render.timeline import assert_renderable
from acvm.ports import WordTiming

log = get_logger(__name__)

RENDERER_NAME = "acvm-ffmpeg/1"
UPSCALE = 2
_BITEXACT = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
             "-flags:a", "+bitexact"]
_COLOR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
_TO709 = "scale=out_color_matrix=bt709:out_range=tv:flags=bicubic,format=yuv420p"


@dataclass(slots=True)
class RenderArtifacts:
    """Files produced in the work directory for one timeline."""

    workdir: Path
    master: Path
    master_ass: Path
    audio: Path
    variants: dict[str, Path] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


def _hex_to_ff(color: str) -> str:
    return "0x" + color.lstrip("#").upper()


class FFmpegRenderer:
    """Renders an ``acvm.timeline/1`` dict to MP4 (plus optional platform variants)."""

    name = RENDERER_NAME

    def __init__(self, spec: RenderSpec, *, preset: str = "veryfast", crf: int = 21, audio_bitrate: str = "160k",
                 time_limit: float = 600.0) -> None:
        self.spec = spec
        self.preset = preset
        self.crf = crf
        self.audio_bitrate = audio_bitrate
        self.time_limit = time_limit

    @property
    def ffmpeg(self) -> str:
        return self.spec.ffmpeg

    # ── video ──

    def scene_chain(self, index: int, scene: Mapping[str, Any], background: str) -> str:
        """Filter chain turning input ``index`` into exactly ``scene['frames']`` 1080×1920 yuv420p frames."""
        w, h, fps = self.spec.width, self.spec.height, self.spec.fps
        n = int(scene["frames"])
        if scene["kind"] == "video":
            body = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic,crop={w}:{h},fps={fps},"
                    f"{_TO709}")
        else:
            uw, uh = w * UPSCALE, h * UPSCALE
            if scene.get("fit") == "contain":
                pre = (f"scale={int(uw * 0.92)}:{int(uh * 0.55)}:force_original_aspect_ratio=decrease:flags=bicubic,"
                       f"{_TO709},pad={uw}:{uh}:(ow-iw)/2:(oh*0.43-ih/2):color={_hex_to_ff(background)}")
            else:
                pre = f"scale={uw}:{uh}:force_original_aspect_ratio=increase:flags=bicubic,crop={uw}:{uh},{_TO709}"
            kb = scene.get("kenburns") or {"s0": 1.0, "s1": 1.0, "u0": 0.0, "u1": 0.0, "v0": 0.0, "v1": 0.0}
            body = f"{pre},{zoompan_filter(kb, n, w, h, fps)}"
        return f"[{index}:v]{body},setsar=1,format=yuv420p,trim=end_frame={n},setpts=PTS-STARTPTS[s{index}]"

    def video_args(self, timeline: Mapping[str, Any], asset_paths: Mapping[str, Path], *, ass_name: str | None,
                   out_name: str, background: str) -> list[str]:
        """Full ffmpeg argv for one video-only output (run with ``cwd`` = work dir)."""
        scenes: Sequence[Mapping[str, Any]] = timeline["scenes"]
        inputs: list[str] = []
        chains: list[str] = []
        for i, sc in enumerate(scenes):
            src = asset_paths.get(str(sc["asset_id"]))
            if src is None:
                raise ValidationFailed("scene asset file missing", asset_id=str(sc["asset_id"]))
            inputs += (["-stream_loop", "-1", "-i", str(src)] if sc["kind"] == "video" else ["-i", str(src)])
            chains.append(self.scene_chain(i, sc, background))
        joined = "".join(f"[s{i}]" for i in range(len(scenes)))
        tail = f"{joined}concat=n={len(scenes)}:v=1:a=0"
        if ass_name is not None:
            tail += f",ass=filename={ass_name}:fontsdir=fonts"
        tail += ",format=yuv420p[v]"
        fps = self.spec.fps
        return [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", *inputs,
                "-filter_complex", ";".join([*chains, tail]), "-map", "[v]", "-frames:v", str(int(timeline["frames"])),
                "-r", str(fps), "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf), "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-video_track_timescale", str(fps * 1000), *_COLOR,
                *_BITEXACT, "-an", out_name]

    async def encode_video(self, timeline: Mapping[str, Any], asset_paths: Mapping[str, Path], workdir: Path, *,
                           ass_name: str | None, out_name: str, background: str = "#14161A") -> Path:
        """Encode the scene graph (+ burned ``ass_name`` relative to ``workdir``) to a video-only file."""
        for p in asset_paths.values():
            if not await asyncio.to_thread(Path(p).is_file):
                raise ValidationFailed("scene asset file missing", path=str(p))
        await run(self.video_args(timeline, asset_paths, ass_name=ass_name, out_name=out_name, background=background),
                  cwd=workdir, time_limit=self.time_limit)
        return workdir / out_name

    # ── audio ──

    def audio_graph(self, timeline: Mapping[str, Any]) -> str:
        audio = timeline["audio"]
        total = float(timeline["duration"])
        tempo = float(audio.get("tempo", 1.0))
        segs = list(audio["segments"])
        base = "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if abs(tempo - 1.0) > 1e-4:
            base += f",atempo={tempo:.4f}"
        parts: list[str] = []
        labels: list[str] = []
        if len(segs) > 1:
            parts.append(base + ",asplit=" + str(len(segs)) + "".join(f"[b{k}]" for k in range(len(segs))))
        else:
            parts.append(base + "[b0]")
        cursor = 0.0
        for k, sg in enumerate(segs):
            s, e, dst = float(sg["src_start"]), float(sg["src_end"]), float(sg["dst_start"])
            gap = dst - cursor
            if gap > 0.0005:
                parts.append(f"aevalsrc=0|0:c=stereo:s=48000:d={gap:.4f}[g{k}]")
                labels.append(f"[g{k}]")
            parts.append(f"[b{k}]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS[p{k}]")
            labels.append(f"[p{k}]")
            cursor = dst + (e - s)
        if len(labels) > 1:
            parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[cat]")
            tail = "[cat]"
        else:
            tail = labels[0]
        parts.append(f"{tail}apad=whole_dur={total:.4f},atrim=end={total:.4f}[aout]")
        return ";".join(parts)

    async def render_audio(self, timeline: Mapping[str, Any], narration: Path, workdir: Path) -> Path:
        """Place, pad and two-pass loudness-normalise the narration → ``workdir/audio.wav`` (stereo 48 kHz)."""
        prep, out = workdir / "audio_prep.wav", workdir / "audio.wav"
        total = float(timeline["duration"])
        await run([self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(narration),
                   "-filter_complex", self.audio_graph(timeline), "-map", "[aout]", "-ar", "48000", "-ac", "2",
                   "-c:a", "pcm_f32le", str(prep)], time_limit=self.time_limit)
        target, tp = self.spec.target_lufs, self.spec.true_peak
        meas = await loudnorm_measure(prep, target_i=target, true_peak=tp, ffmpeg=self.ffmpeg,
                                      time_limit=self.time_limit)
        if meas["input_i"] < -70.0:
            raise MediaError("narration is silent; cannot normalise loudness", measured=meas["input_i"])
        af = (f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
              f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
              f"offset={meas['target_offset']}:linear=true:print_format=none,aresample=48000")
        await run([self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(prep), "-af", af,
                   "-t", f"{total:.4f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", str(out)],
                  time_limit=self.time_limit)
        return out

    # ── mux ──

    async def mux(self, workdir: Path, *, video: Path, audio: Path, out_name: str, duration: float) -> Path:
        await run([self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", video.name, "-i", audio.name,
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", self.audio_bitrate,
                   "-ar", "48000", "-ac", "2", "-t", f"{duration:.4f}", "-movflags", "+faststart", *_BITEXACT,
                   out_name], cwd=workdir, time_limit=self.time_limit)
        return workdir / out_name

    # ── ASS ──

    def ass_text(self, timeline: Mapping[str, Any], font: FontInfo, layout: Layout) -> str:
        """ASS for one output layout, honouring the Studio style options recorded in ``timeline["style"]``."""
        st = timeline.get("style") or {}
        position = str(st.get("caption_position") or "bottom")
        box = onscreen_box_for(layout, position, self.spec.height)
        onscreen = [{**item, "box": list(box)} for item in timeline.get("onscreen", [])]
        cues = [Cue(float(c["start"]), float(c["end"]), tuple(c["lines"]), int(c.get("words", [0, -1])[0]),
                    int(c.get("words", [0, -1])[1])) for c in timeline.get("captions", [])]
        words = [WordTiming(str(w[0]), float(w[1]), float(w[2])) for w in timeline.get("words", [])]
        return build_ass_document(width=self.spec.width, height=self.spec.height, font_family=font.family,
                                  layout=layout, overlay=timeline["overlays"]["disclosure"], captions=cues,
                                  onscreen=onscreen, caption_style_id=st.get("caption_style"),
                                  caption_position=position, captions_enabled=bool(st.get("captions_enabled", True)),
                                  watermark=str(st.get("watermark") or ""), words=words,
                                  duration=float(timeline["duration"]), ai_labels=timeline.get("ai_labels") or ())

    # ── orchestration ──

    async def render(self, timeline: Mapping[str, Any], *, narration_wav: Path, asset_paths: Mapping[str, Path],
                     font: FontInfo, workdir: Path, background: str = "#14161A") -> RenderArtifacts:
        """Render ``timeline`` into ``workdir`` (created if needed). Returns the produced artifacts."""
        assert_renderable(timeline, self.spec)
        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        t0 = time.perf_counter()
        await asyncio.to_thread(materialize_fonts_dir, font, workdir / "fonts")
        jobs: list[tuple[str, Layout]] = [("master", layout_from_dict(timeline["layout"]))]
        jobs += [(p, layout_from_dict(v["layout"])) for p, v in sorted(timeline.get("variants", {}).items())]
        for label, lay in jobs:
            await asyncio.to_thread(_write_text, workdir / f"{label}.ass", self.ass_text(timeline, font, lay))
        tasks: list[asyncio.Task[Path]] = [asyncio.create_task(self.render_audio(timeline, narration_wav, workdir))]
        tasks += [asyncio.create_task(self.encode_video(timeline, asset_paths, workdir, ass_name=f"{label}.ass",
                                                        out_name=f"{label}.video.mp4", background=background))
                  for label, _ in jobs]
        try:
            audio, *videos = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        t1 = time.perf_counter()
        duration = float(timeline["duration"])
        outputs = await asyncio.gather(*(self.mux(workdir, video=v, audio=audio, out_name=f"{label}.mp4",
                                                  duration=duration) for (label, _), v in zip(jobs, videos, strict=True)))
        produced = {label: out for (label, _), out in zip(jobs, outputs, strict=True)}
        t2 = time.perf_counter()
        timings = {"encode": round(t1 - t0, 3), "mux": round(t2 - t1, 3)}
        log.info("render_encoded", scenes=len(timeline["scenes"]), duration=duration, variants=len(jobs) - 1, **timings)
        return RenderArtifacts(workdir=workdir, master=produced.pop("master"), master_ass=workdir / "master.ass",
                               audio=audio, variants=produced, timings=timings)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
