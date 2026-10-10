"""Studio edit tools: 컷편집 (frame-accurate cut), 이미지→영상 (camera-motion only) and thumbnails.

``image_to_video`` is ACVM's compliant answer to generative "I2V": it animates an existing ledger image with a
pure virtual-camera move (zoom / pan) — the pixels of the pictured product are never synthesised or altered
(§4 M5: 상품 실물은 AI로 생성·변형하지 않는다). Outputs are 1080×1920, 30 fps, H.264 yuv420p (BT.709), +faststart.
All ffmpeg calls are async subprocesses (:func:`acvm.media.ffmpeg.run`).
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from PIL import Image

from acvm.core.errors import ValidationFailed
from acvm.media.ffmpeg import extract_frame, probe, run, sibling_ffprobe

Motion = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"]
MOTIONS: tuple[str, ...] = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up")
_TO709 = "scale=out_color_matrix=bt709:out_range=tv:flags=bicubic,format=yuv420p"
_COLOR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
_BITEXACT = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
             "-flags:a", "+bitexact"]
UPSCALE = 2


def _f(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def zoompan_filter(kb: Mapping[str, float], frames: int, w: int, h: int, fps: int) -> str:
    """``zoompan`` with z(n) = s0·(s1/s0)^(n/(N−1)) (perceptually uniform zoom) and a linear pan, centred."""
    n1 = max(1, frames - 1)
    s0, s1 = float(kb["s0"]), float(kb["s1"])
    u0, u1, v0, v1 = (float(kb[k]) for k in ("u0", "u1", "v0", "v1"))
    z = f"{_f(s0)}*pow({_f(s1 / s0)},on/{n1})"
    x = f"iw/2-(iw/zoom/2)+({_f(u0)}+({_f(u1 - u0)})*on/{n1})*iw/zoom"
    y = f"ih/2-(ih/zoom/2)+({_f(v0)}+({_f(v1 - v0)})*on/{n1})*ih/zoom"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps}"


def motion_kenburns(motion: str, strength: float = 1.0) -> dict[str, float]:
    """Ken Burns parameters for a named camera move (margin inequality s ≥ 1 + 2|u| always holds)."""
    z = 1.0 + 0.14 * strength
    pan = 0.045 * strength
    s = 1.0 + 2.4 * pan
    table = {
        "zoom_in": (1.0, z, 0.0, 0.0, 0.0, 0.0),
        "zoom_out": (z, 1.0, 0.0, 0.0, 0.0, 0.0),
        "pan_left": (s, s, pan, -pan, 0.0, 0.0),  # camera moves left → content slides right
        "pan_right": (s, s, -pan, pan, 0.0, 0.0),
        "pan_up": (s, s, 0.0, 0.0, pan, -pan),
    }
    if motion not in table:
        raise ValidationFailed("unknown camera motion", motion=motion, allowed=list(MOTIONS))
    s0, s1, u0, u1, v0, v1 = table[motion]
    return {"s0": s0, "s1": s1, "u0": u0, "u1": u1, "v0": v0, "v1": v1}


async def cut_video(src: Path, start: float, end: float, out: Path, *, ffmpeg: str = "ffmpeg",
                    crf: int = 18) -> Path:
    """Frame-accurate cut ``[start, end)`` re-encoded to H.264/AAC (audio kept when present)."""
    if start < 0 or end <= start:
        raise ValidationFailed("cut range must satisfy 0 ≤ start < end", start=start, end=end)
    info = await probe(src, ffprobe=sibling_ffprobe(ffmpeg))
    if not info.has_video:
        raise ValidationFailed("source has no video stream")
    if start >= info.duration:
        raise ValidationFailed("cut starts after the end of the video", start=start, duration=round(info.duration, 3))
    end = min(end, info.duration)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if info.has_audio else ["-an"]
    await run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
               "-t", f"{end - start:.3f}", "-map", "0:v:0", *audio, "-c:v", "libx264", "-preset", "veryfast",
               "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", *_BITEXACT, str(out)],
              time_limit=600)
    return out


async def image_to_video(src_image: Path, out: Path, *, seconds: float = 4.0, motion: str = "zoom_in",
                         ffmpeg: str = "ffmpeg", width: int = 1080, height: int = 1920, fps: int = 30,
                         background: str = "#14161A") -> Path:
    """Animate a still with a virtual camera move → 1080×1920 30 fps H.264 (no audio). Non-9:16 images are
    letterboxed on ``background`` rather than cropped, so the whole product stays visible."""
    if not 1.0 <= seconds <= 15.0:
        raise ValidationFailed("image_to_video seconds must be within 1–15", seconds=seconds)
    kb = motion_kenburns(motion)
    try:
        with Image.open(src_image) as im:
            iw, ih = im.size
    except OSError as exc:
        raise ValidationFailed("source is not a readable image") from exc
    frames = max(2, round(seconds * fps))
    uw, uh = width * UPSCALE, height * UPSCALE
    if iw / max(1, ih) > 0.75:
        pre = (f"scale={int(uw * 0.92)}:{int(uh * 0.62)}:force_original_aspect_ratio=decrease:flags=bicubic,"
               f"{_TO709},pad={uw}:{uh}:(ow-iw)/2:(oh-ih)/2:color=0x{background.lstrip('#')}")
    else:
        pre = f"scale={uw}:{uh}:force_original_aspect_ratio=increase:flags=bicubic,crop={uw}:{uh},{_TO709}"
    vf = f"{pre},{zoompan_filter(kb, frames, width, height, fps)},setsar=1,format=yuv420p"
    out.parent.mkdir(parents=True, exist_ok=True)
    await run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src_image), "-vf", vf,
               "-frames:v", str(frames), "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", *_COLOR, *_BITEXACT, "-an", str(out)], time_limit=300)
    return out


async def video_thumbnail(src: Path, *, at: float = 1.0, ffmpeg: str = "ffmpeg", max_side: int = 640,
                          quality: int = 85) -> bytes:
    """JPEG thumbnail of the frame at ``at`` seconds (clamped into the clip), longest side ≤ ``max_side``."""
    info = await probe(src, ffprobe=sibling_ffprobe(ffmpeg))
    t = max(0.0, min(at, max(0.0, info.duration - 0.1)))
    png = await extract_frame(src, t, ffmpeg=ffmpeg)
    with Image.open(io.BytesIO(png)) as im:
        rgb = im.convert("RGB")
        rgb.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()
