"""Studio edit tools: frame-accurate cut, image→video camera motion (never product synthesis), thumbnails."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from acvm.core.errors import ValidationFailed
from acvm.media.edit import MOTIONS, cut_video, image_to_video, motion_kenburns, video_thumbnail
from acvm.media.ffmpeg import extract_frame, probe, run

pytestmark = pytest.mark.render


async def _clip(path: Path, *, seconds: float = 4.0, audio: bool = True) -> Path:
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc2=s=720x1280:r=30:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else []
    await run([*args, str(path)])
    return path


async def test_cut_is_frame_accurate_and_keeps_audio(tmp_path: Path) -> None:
    src = await _clip(tmp_path / "src.mp4")
    out = await cut_video(src, 1.0, 2.5, tmp_path / "cut.mp4")
    info = await probe(out)
    assert info.duration == pytest.approx(1.5, abs=0.07)
    assert info.video_codec == "h264" and info.audio_codec == "aac" and (info.width, info.height) == (720, 1280)
    silent = await cut_video(await _clip(tmp_path / "mute.mp4", audio=False), 0.5, 1.5, tmp_path / "mute_cut.mp4")
    assert not (await probe(silent)).has_audio
    clamped = await cut_video(src, 3.0, 99.0, tmp_path / "tail.mp4")
    assert (await probe(clamped)).duration == pytest.approx(1.0, abs=0.07)


async def test_cut_rejects_bad_ranges(tmp_path: Path) -> None:
    src = await _clip(tmp_path / "src.mp4", seconds=2.0)
    for start, end in ((1.0, 1.0), (-1.0, 1.0), (5.0, 6.0)):
        with pytest.raises(ValidationFailed):
            await cut_video(src, start, end, tmp_path / "x.mp4")


@pytest.mark.parametrize("motion", MOTIONS)
async def test_image_to_video_motion(tmp_path: Path, motion: str) -> None:
    img = tmp_path / "product.png"
    arr = np.zeros((900, 900, 3), dtype=np.uint8)
    arr[::30, :, :] = 255
    arr[:, ::30, 1] = 200
    Image.fromarray(arr).save(img)
    out = await image_to_video(img, tmp_path / f"{motion}.mp4", seconds=2.0, motion=motion)
    info = await probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.fps == pytest.approx(30.0)
    assert info.duration == pytest.approx(2.0, abs=0.05) and info.video_codec == "h264" and not info.has_audio
    first = np.asarray(Image.open(io.BytesIO(await extract_frame(out, 0.0))).convert("L"), dtype=np.float64)
    last = np.asarray(Image.open(io.BytesIO(await extract_frame(out, 1.9))).convert("L"), dtype=np.float64)
    assert np.abs(first - last).mean() > 1.0  # the camera actually moves
    assert first[:150].std() < 3.0  # square photo letterboxed: top area is plain background, image not cropped


async def test_image_to_video_validates(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        motion_kenburns("spin")
    bogus = tmp_path / "x.png"
    bogus.write_bytes(b"nope")
    with pytest.raises(ValidationFailed):
        await image_to_video(bogus, tmp_path / "o.mp4")
    with pytest.raises(ValidationFailed):
        await image_to_video(bogus, tmp_path / "o.mp4", seconds=30.0)


async def test_video_thumbnail_is_jpeg(tmp_path: Path) -> None:
    src = await _clip(tmp_path / "src.mp4", seconds=2.0)
    jpg = await video_thumbnail(src, at=5.0)
    with Image.open(io.BytesIO(jpg)) as im:
        assert im.format == "JPEG" and max(im.size) == 640
