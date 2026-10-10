"""Thin async wrappers around ffmpeg / ffprobe (§4 M6, §9.1 "FFmpeg … JSON 타임라인 → 결정적 렌더").

Every invocation goes through :func:`run`, which uses ``asyncio.create_subprocess_exec`` (never a shell),
enforces a wall-clock timeout and turns a non-zero exit into :class:`MediaError` carrying the stderr tail,
so a failed render is diagnosable from the log line alone. The helpers here are shared by M6 (render),
M7 (D-03 frame verification) and the console (thumbnails); they hold no state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acvm.core.errors import ACVMError

STDERR_TAIL = 1200


class MediaError(ACVMError):
    """An ffmpeg/ffprobe invocation failed. Deterministic failures are not retryable; timeouts are."""

    code = "media_error"

    def __init__(self, message: str, *, stderr_tail: str = "", retryable: bool = False, **context: object) -> None:
        super().__init__(message, stderr_tail=stderr_tail, **context)
        self.stderr_tail = stderr_tail
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def _tail(data: bytes, limit: int = STDERR_TAIL) -> str:
    return data.decode("utf-8", errors="replace")[-limit:].strip()


def _exe_name(args: Sequence[str | Path]) -> str:
    return Path(str(args[0])).name if args else "?"


async def run(args: Sequence[str | Path], *, cwd: Path | str | None = None, time_limit: float = 600.0,
              stdin: bytes | None = None, check: bool = True) -> RunResult:
    """Run an executable asynchronously and capture stdout/stderr.

    ``args[0]`` is the binary (``ffmpeg``/``ffprobe`` or an absolute path). Raises :class:`MediaError`
    on a missing binary, a timeout (retryable) or — when ``check`` — a non-zero exit code.
    """
    argv = [str(a) for a in args]
    name = _exe_name(argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd is not None else None,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as exc:
        raise MediaError(f"{name} not found on PATH", binary=name) from exc
    except NotImplementedError:
        # Selector event loops on Windows cannot spawn subprocesses; fall back to a worker thread.
        return await _run_in_thread(argv, cwd=cwd, time_limit=time_limit, stdin=stdin, check=check)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=time_limit)
    except TimeoutError as exc:
        raise MediaError(f"{name} timed out after {time_limit:.0f}s", retryable=True, binary=name) from exc
    finally:
        # never leave an orphaned ffmpeg behind (timeout, cancellation of a sibling task, any error)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
    result = RunResult(proc.returncode if proc.returncode is not None else -1, out, err)
    if check and result.returncode != 0:
        tail = _tail(err)
        raise MediaError(f"{name} failed (exit {result.returncode}): {tail[-400:]}", stderr_tail=tail,
                         returncode=result.returncode, binary=name)
    return result


async def _run_in_thread(argv: list[str], *, cwd: Path | str | None, time_limit: float, stdin: bytes | None,
                         check: bool) -> RunResult:
    name = _exe_name(argv)

    def _call() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(argv, cwd=str(cwd) if cwd is not None else None, input=stdin,  # noqa: S603
                              capture_output=True, timeout=time_limit, check=False)

    try:
        cp = await asyncio.to_thread(_call)
    except FileNotFoundError as exc:
        raise MediaError(f"{name} not found on PATH", binary=name) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{name} timed out after {time_limit:.0f}s", retryable=True, binary=name) from exc
    result = RunResult(cp.returncode, cp.stdout, cp.stderr)
    if check and result.returncode != 0:
        tail = _tail(cp.stderr)
        raise MediaError(f"{name} failed (exit {result.returncode}): {tail[-400:]}", stderr_tail=tail,
                         returncode=result.returncode, binary=name)
    return result


def sibling_ffprobe(ffmpeg: str) -> str:
    """Derive the ffprobe path that ships next to a given ffmpeg binary (falls back to PATH lookup)."""
    p = Path(ffmpeg)
    if p.parent != Path() and p.parent.exists():
        cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return str(cand)
    return "ffprobe"


# ───────────────────────────── probe ─────────────────────────────


def _ratio(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Facts about a media file that the §4 M6 output contract is checked against."""

    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    pix_fmt: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0
    size_bytes: int = 0
    format_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)


async def probe(path: Path | str, *, ffprobe: str = "ffprobe", time_limit: float = 60.0) -> MediaInfo:
    """ffprobe → duration / geometry / frame rate / codecs / audio layout."""
    res = await run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                    time_limit=time_limit)
    try:
        data: dict[str, Any] = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned invalid JSON", stderr_tail=res.stderr_text[-400:]) from exc
    streams: list[dict[str, Any]] = data.get("streams", [])
    fmt: dict[str, Any] = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _ratio(str(fmt.get("duration", "0")))
    if duration <= 0 and v is not None:
        duration = _ratio(str(v.get("duration", "0")))
    fps = 0.0
    if v is not None:
        fps = _ratio(v.get("avg_frame_rate")) or _ratio(v.get("r_frame_rate"))
    return MediaInfo(
        duration=duration,
        width=int(v.get("width", 0)) if v else 0,
        height=int(v.get("height", 0)) if v else 0,
        fps=round(fps, 3),
        video_codec=str(v.get("codec_name", "")) if v else "",
        pix_fmt=str(v.get("pix_fmt", "")) if v else "",
        audio_codec=str(a.get("codec_name", "")) if a else "",
        sample_rate=int(a.get("sample_rate", 0) or 0) if a else 0,
        channels=int(a.get("channels", 0) or 0) if a else 0,
        bit_rate=int(fmt.get("bit_rate", 0) or 0),
        size_bytes=int(fmt.get("size", 0) or 0),
        format_name=str(fmt.get("format_name", "")),
        raw=data,
    )


# ───────────────────────────── loudness ─────────────────────────────


@dataclass(frozen=True, slots=True)
class LoudnessStats:
    """EBU R128 summary (integrated LUFS, true peak dBTP, loudness range LU)."""

    integrated: float
    true_peak: float
    lra: float


_EBU_I = re.compile(r"Integrated loudness:\s*I:\s*(-?[\d.]+|-inf)\s*LUFS")
_EBU_LRA = re.compile(r"Loudness range:\s*LRA:\s*(-?[\d.]+)\s*LU")
_EBU_TP = re.compile(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS")


def _num(token: str) -> float:
    return -math.inf if token == "-inf" else float(token)


async def loudness_stats(path: Path | str, *, ffmpeg: str = "ffmpeg", time_limit: float = 300.0) -> LoudnessStats:
    """Measure with the ``ebur128`` filter (true-peak enabled) on the first audio stream."""
    res = await run([ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-map", "0:a:0",
                     "-af", "ebur128=peak=true", "-f", "null", "-"], time_limit=time_limit)
    text = res.stderr_text
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    mi, ml, mt = _EBU_I.search(summary), _EBU_LRA.search(summary), _EBU_TP.search(summary)
    if mi is None:
        raise MediaError("ebur128 summary not found", stderr_tail=text[-600:])
    return LoudnessStats(integrated=_num(mi.group(1)), lra=float(ml.group(1)) if ml else 0.0,
                         true_peak=_num(mt.group(1)) if mt else 0.0)


async def measure_loudness(path: Path | str, *, ffmpeg: str = "ffmpeg", time_limit: float = 300.0) -> float:
    """Integrated loudness (LUFS) of the first audio stream — the §4 M6 −14 LUFS contract metric."""
    return (await loudness_stats(path, ffmpeg=ffmpeg, time_limit=time_limit)).integrated


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


async def loudnorm_measure(path: Path | str, *, target_i: float, true_peak: float, lra: float = 11.0,
                           ffmpeg: str = "ffmpeg", time_limit: float = 300.0) -> dict[str, float]:
    """First pass of two-pass ``loudnorm``: returns input_i / input_tp / input_lra / input_thresh / offset."""
    res = await run([ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-af",
                     f"loudnorm=I={target_i}:TP={true_peak}:LRA={lra}:print_format=json", "-f", "null", "-"],
                    time_limit=time_limit)
    m = _LOUDNORM_JSON.search(res.stderr_text)
    if m is None:
        raise MediaError("loudnorm measurement JSON not found", stderr_tail=res.stderr_text[-600:])
    raw = json.loads(m.group(0))
    out: dict[str, float] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            out[key] = float(raw.get(key, 0.0))
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


# ───────────────────────────── frames & hashing ─────────────────────────────


async def extract_frame(path: Path | str, t: float, *, ffmpeg: str = "ffmpeg", time_limit: float = 60.0) -> bytes:
    """Decode the frame displayed at ``t`` seconds and return it as PNG bytes (frame-accurate seek)."""
    res = await run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}",
                     "-i", str(path), "-frames:v", "1", "-an", "-f", "image2pipe", "-c:v", "png", "-"],
                    time_limit=time_limit)
    if not res.stdout.startswith(b"\x89PNG"):
        raise MediaError(f"no frame decoded at t={t:.3f}s", stderr_tail=res.stderr_text[-400:])
    return res.stdout


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    """Hex SHA-256 of a file (streamed)."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
