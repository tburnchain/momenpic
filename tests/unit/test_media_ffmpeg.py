"""Async ffmpeg/ffprobe wrappers (probe, loudness, frame extraction, error capture)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from acvm.media.ffmpeg import (
    MediaError,
    extract_frame,
    loudness_stats,
    measure_loudness,
    probe,
    run,
    sha256_file,
)

pytestmark = pytest.mark.render


async def _make_av(path: Path, *, amp: float = 0.25, seconds: float = 4.0) -> Path:
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
               "-f", "lavfi", "-i", f"testsrc2=s=1080x1920:r=30:d={seconds}",
               "-f", "lavfi", "-i", f"aevalsrc={amp}*sin(2*PI*997*t):s=48000:d={seconds}",
               "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ar", "48000", str(path)])
    return path


async def test_probe_reports_geometry_rate_and_codecs(tmp_path: Path) -> None:
    p = await _make_av(tmp_path / "a.mp4")
    info = await probe(p)
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == pytest.approx(30.0)
    assert info.video_codec == "h264" and info.pix_fmt == "yuv420p"
    assert info.audio_codec == "aac" and info.sample_rate == 48000
    assert info.duration == pytest.approx(4.0, abs=0.1)
    assert info.has_audio and info.has_video


async def test_loudness_tracks_signal_level(tmp_path: Path) -> None:
    loud = await _make_av(tmp_path / "loud.mp4", amp=0.5)
    quiet = await _make_av(tmp_path / "quiet.mp4", amp=0.25)
    li, qi = await measure_loudness(loud), await measure_loudness(quiet)
    assert li - qi == pytest.approx(6.02, abs=0.3)
    stats = await loudness_stats(loud)
    assert stats.true_peak < 0.0 and stats.integrated == pytest.approx(li)


async def test_extract_frame_returns_png_of_frame_size(tmp_path: Path) -> None:
    p = await _make_av(tmp_path / "f.mp4")
    png = await extract_frame(p, 1.5)
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (1080, 1920)
    assert await extract_frame(p, 1.5) == png  # deterministic decode


async def test_failures_carry_stderr_tail(tmp_path: Path) -> None:
    with pytest.raises(MediaError) as ei:
        await probe(tmp_path / "missing.mp4")
    assert ei.value.stderr_tail
    with pytest.raises(MediaError):
        await run(["definitely-not-a-binary-acvm", "-version"])
    res = await run(["ffmpeg", "-hide_banner", "-v", "error", "-i", str(tmp_path / "x.mp4")], check=False)
    assert res.returncode != 0 and res.stderr_text


async def test_timeout_is_retryable(tmp_path: Path) -> None:
    with pytest.raises(MediaError) as ei:
        await run(["ffmpeg", "-hide_banner", "-v", "error", "-re", "-f", "lavfi", "-i", "anullsrc=d=30",
                   "-f", "null", "-"], time_limit=0.5)
    assert ei.value.retryable


def test_sha256_file(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"acvm" * 1000)
    assert sha256_file(p) == hashlib.sha256(b"acvm" * 1000).hexdigest()


async def test_thread_fallback_path_runs_and_enforces_limits(tmp_path: Path) -> None:
    """Selector loops on Windows cannot spawn subprocesses; the worker-thread fallback must behave the same."""
    from acvm.media.ffmpeg import _run_in_thread

    ok = await _run_in_thread(["ffmpeg", "-hide_banner", "-version"], cwd=tmp_path, time_limit=30, stdin=None,
                              check=True)
    assert ok.returncode == 0 and b"ffmpeg version" in ok.stdout
    with pytest.raises(MediaError) as ei:
        await _run_in_thread(["ffmpeg", "-hide_banner", "-v", "error", "-re", "-f", "lavfi", "-i", "anullsrc=d=30",
                              "-f", "null", "-"], cwd=None, time_limit=0.5, stdin=None, check=True)
    assert ei.value.retryable
    with pytest.raises(MediaError):
        await _run_in_thread(["ffmpeg", "-v", "error", "-i", str(tmp_path / "missing.mp4")], cwd=None, time_limit=30,
                             stdin=None, check=True)
