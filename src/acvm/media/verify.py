"""Frame-level proof that the disclosure overlay is on screen (§8.1 D-03: "영상 첫 3초 화면에 '광고' 표시 오버레이가
있는가 — 렌더 메타데이터 + 프레임 OCR → 실패 시 BLOCK").

Instead of trusting OCR on small Korean glyphs, the check is a *reference match*: the overlay's exact
ASS events (:func:`acvm.media.ass.disclosure_only_document`) are rendered with libass over two different
solid backgrounds, which (a) proves the overlay design is opaque — both references must agree — and (b)
yields the exact pixels the band must show. Frames are decoded from the delivered file at several instants
inside ``[start, min(end, 3 s))`` and the band region is compared with the reference using normalised
cross-correlation, mean absolute difference and gradient (edge) correlation on luma. A missing, shifted,
re-worded, translucent or truncated band fails; compression noise does not. The check passes only when
**every** sampled frame matches and the overlay text contains "광고".

If ``tesseract`` is on PATH an OCR read of the band is added to ``details`` as corroborating evidence; it
never relaxes the pixel check.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image

from acvm.media.ass import disclosure_only_document
from acvm.media.ffmpeg import MediaError, extract_frame, probe, run, sibling_ffprobe
from acvm.media.fonts import FontInfo, materialize_fonts_dir, resolve_font

AD_LABEL = "광고"
DEFAULT_SAMPLE_OFFSETS = (0.3, 1.5, 2.7)


@dataclass(frozen=True, slots=True)
class DisclosureCheck:
    """Result of :func:`verify_disclosure_overlay`. ``score`` is the worst per-frame NCC (0‥1)."""

    ok: bool
    score: float
    details: dict[str, Any] = field(default_factory=dict)


def _gray(png: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(png)) as im:
        gray = im.convert("L")
    arr = np.frombuffer(gray.tobytes(), dtype=np.uint8).reshape(gray.height, gray.width)
    return cast(np.ndarray, arr.astype(np.float64))


def _crop(arr: np.ndarray, box: tuple[int, int, int, int], inset: int) -> np.ndarray:
    x, y, w, h = box
    x0, y0 = max(0, x + inset), max(0, y + inset)
    x1, y1 = min(arr.shape[1], x + w - inset), min(arr.shape[0], y + h - inset)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("overlay box is empty after inset")
    return arr[y0:y1, x0:x1]


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    da, db = a - a.mean(), b - b.mean()
    den = float(np.sqrt((da * da).sum() * (db * db).sum()))
    if den < 1e-9:
        return 0.0
    return float((da * db).sum() / den)


def _edges(a: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[:, 1:-1] = a[:, 2:] - a[:, :-2]
    gy[1:-1, :] = a[2:, :] - a[:-2, :]
    return cast(np.ndarray, np.hypot(gx, gy))


def compare_band(frame: np.ndarray, reference: np.ndarray) -> dict[str, float]:
    """Similarity metrics between a decoded band crop and its reference rendering (both luma, 0‥255)."""
    if frame.shape != reference.shape:
        return {"ncc": 0.0, "mad": 255.0, "edge_ncc": 0.0}
    return {
        "ncc": round(_ncc(frame, reference), 4),
        "mad": round(float(np.abs(frame - reference).mean()), 3),
        "edge_ncc": round(_ncc(_edges(frame), _edges(reference)), 4),
    }


async def render_reference_band(overlay: Mapping[str, Any], *, width: int, height: int, font: FontInfo,
                                background: str, ffmpeg: str = "ffmpeg", workdir: Path | None = None) -> bytes:
    """Render only the locked disclosure events over a solid ``background`` (``0xRRGGBB``) → PNG bytes."""
    family = font.family
    doc = disclosure_only_document(overlay, width=width, height=height, font_family=family)
    own_tmp = workdir is None
    wd = Path(tempfile.mkdtemp(prefix="acvm_d03_")) if workdir is None else workdir
    try:
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "disclosure.ass").write_text(doc, encoding="utf-8")
        materialize_fonts_dir(font, wd / "fonts")
        res = await run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                         f"color=c={background}:s={width}x{height}:d=1:r=30,format=yuv420p",
                         "-vf", "ass=filename=disclosure.ass:fontsdir=fonts", "-frames:v", "1",
                         "-f", "image2pipe", "-c:v", "png", "-"], cwd=wd, time_limit=60)
        if not res.stdout.startswith(b"\x89PNG"):
            raise MediaError("reference band rendering produced no frame", stderr_tail=res.stderr_text[-400:])
        return res.stdout
    finally:
        if own_tmp:
            shutil.rmtree(wd, ignore_errors=True)


def _font_for(overlay: Mapping[str, Any], font_file: str | None) -> FontInfo:
    candidate = font_file or str(overlay.get("font_file") or "")
    if candidate and Path(candidate).is_file():
        return resolve_font(candidate)
    return resolve_font()


async def _ocr(png: bytes, box: tuple[int, int, int, int]) -> dict[str, Any] | None:
    exe = shutil.which("tesseract")
    if exe is None:
        return None
    with Image.open(io.BytesIO(png)) as im:
        x, y, w, h = box
        crop = im.convert("L").crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
    try:
        res = await run([exe, "stdin", "stdout", "-l", "kor+eng", "--psm", "6"], stdin=buf.getvalue(), time_limit=60,
                        check=False)
    except MediaError:
        return {"available": True, "error": "tesseract failed"}
    text = res.stdout.decode("utf-8", errors="replace").strip()
    return {"available": True, "text": text, "has_ad_label": AD_LABEL in text.replace(" ", "")}


async def verify_disclosure_overlay(video_path: Path, overlay: dict[str, Any], *, ffmpeg: str = "ffmpeg",
                                    ffprobe: str | None = None, min_seconds: float = 3.0,
                                    font_file: str | None = None, ncc_min: float = 0.80, mad_max: float = 16.0,
                                    sample_offsets: tuple[float, ...] = DEFAULT_SAMPLE_OFFSETS,
                                    inset: int = 4) -> DisclosureCheck:
    """Verify that ``video_path`` shows the locked disclosure band described by ``overlay`` (D-03).

    ``overlay`` is ``RenderMeta.disclosure_overlay`` (keys: text, start, end, box=[x,y,w,h], bg, fg, font_size,
    lines, text_pos, font_family, locked). Never raises for a bad video — any failure is reported as
    ``ok=False`` with a reason, because the gate must BLOCK rather than crash.
    """
    details: dict[str, Any] = {"rule": "D-03", "method": "reference-band-match"}
    text = str(overlay.get("text", ""))
    details["text_has_ad_label"] = AD_LABEL in text
    reasons: list[str] = []
    if not details["text_has_ad_label"]:
        reasons.append("overlay text lacks '광고'")
    try:
        start, end = float(overlay.get("start", 0.0)), float(overlay.get("end", 0.0))
        box = tuple(int(v) for v in overlay["box"])
        if len(box) != 4:
            raise ValueError("box must be [x, y, w, h]")
    except (KeyError, TypeError, ValueError) as exc:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, f"malformed overlay: {exc}"]})
    bx: tuple[int, int, int, int] = (box[0], box[1], box[2], box[3])
    details.update(start=start, end=end, box=list(bx))
    if start > 0.05:
        reasons.append(f"overlay starts late ({start:.2f}s)")
    if end < min_seconds - 0.05:
        reasons.append(f"overlay ends at {end:.2f}s < {min_seconds:.1f}s")
    if not overlay.get("locked", False):
        reasons.append("overlay not template-locked")
    try:
        info = await probe(video_path, ffprobe=ffprobe or sibling_ffprobe(ffmpeg))
    except MediaError as exc:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, f"unreadable video: {exc}"]})
    details["video"] = {"width": info.width, "height": info.height, "duration": round(info.duration, 3)}
    if not info.has_video:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, "no video stream"]})
    if bx[0] < 0 or bx[1] < 0 or bx[0] + bx[2] > info.width or bx[1] + bx[3] > info.height:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, "box outside frame"]})
    window_end = min(end, min_seconds, info.duration)
    times = sorted({round(min(max(start + o, start), window_end - 0.05), 3) for o in sample_offsets
                    if start + o < window_end})
    if not times:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, "no sample instants in window"]})
    try:
        font = _font_for(overlay, font_file)
        details["font_family"] = font.family
        if overlay.get("font_family") and str(overlay["font_family"]) != font.family:
            details["font_mismatch"] = {"overlay": overlay["font_family"], "resolved": font.family}
        with tempfile.TemporaryDirectory(prefix="acvm_d03_", ignore_cleanup_errors=True) as td:
            refs = await asyncio.gather(
                render_reference_band(overlay, width=info.width, height=info.height, font=font, background="0x000000",
                                      ffmpeg=ffmpeg, workdir=Path(td) / "black"),
                render_reference_band(overlay, width=info.width, height=info.height, font=font, background="0xF0F0F0",
                                      ffmpeg=ffmpeg, workdir=Path(td) / "white"))
        frames = await asyncio.gather(*(extract_frame(video_path, t, ffmpeg=ffmpeg) for t in times))
    except (MediaError, OSError, ValueError) as exc:
        return DisclosureCheck(False, 0.0, {**details, "reasons": [*reasons, f"verification error: {exc}"]})
    ref_a, ref_b = (_crop(_gray(r), bx, inset) for r in refs)
    opacity = compare_band(ref_a, ref_b)
    details["reference_opacity"] = opacity
    if opacity["mad"] > 1.5:
        reasons.append("overlay design is not opaque (reference differs across backgrounds)")
    per_frame: list[dict[str, Any]] = []
    for t, png in zip(times, frames, strict=True):
        try:
            crop = _crop(_gray(png), bx, inset)
        except ValueError:
            crop = np.zeros((1, 1))
        m = compare_band(crop, ref_a)
        m["t"] = t
        m["pass"] = bool(m["ncc"] >= ncc_min and m["mad"] <= mad_max)
        per_frame.append(m)
    details["frames"] = per_frame
    failing = [f["t"] for f in per_frame if not f["pass"]]
    if failing:
        reasons.append(f"band not matched at t={failing}")
    ocr = await _ocr(frames[0], bx)
    details["ocr"] = ocr if ocr is not None else {"available": False}
    score = float(min(f["ncc"] for f in per_frame)) if per_frame else 0.0
    details["reasons"] = reasons
    return DisclosureCheck(ok=not reasons, score=max(0.0, score), details=details)
