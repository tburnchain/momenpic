"""D-03 frame verification of the disclosure band (§8.1: 실패 시 BLOCK)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from acvm.config import GatePolicy
from acvm.media.ass import disclosure_only_document, make_disclosure_overlay
from acvm.media.cards import hook_card, render_card
from acvm.media.ffmpeg import run
from acvm.media.fonts import libass_text_width, materialize_fonts_dir, resolve_font
from acvm.media.verify import verify_disclosure_overlay

pytestmark = pytest.mark.render


def _overlay(text: str | None = None, seconds: float = 3.0) -> dict[str, Any]:
    font = resolve_font()
    return make_disclosure_overlay(text or GatePolicy().video_disclosure, seconds=seconds, font_family=font.family,
                                   font_file=str(font.path),
                                   measure=lambda t, s: (libass_text_width(font, t, s), float(s)))


async def _video(tmp: Path, name: str, *, burn: dict[str, Any] | None, seconds: float = 4.0,
                 card_text: str = "과자 봉지 아직도 이렇게 막아두세요?") -> Path:
    """A card video like a real scene; optionally with the disclosure ASS burned in."""
    font = resolve_font()
    work = tmp / name
    work.mkdir()
    (work / "card.png").write_bytes(render_card(hook_card(card_text, seed="verify")))
    vf = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
    if burn is not None:
        (work / "d.ass").write_text(disclosure_only_document(burn, width=1080, height=1920, font_family=font.family),
                                    encoding="utf-8")
        materialize_fonts_dir(font, work / "fonts")
        vf += ",ass=filename=d.ass:fontsdir=fonts"
    await run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-loop", "1", "-framerate", "30",
               "-i", "card.png", "-t", str(seconds), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p", "out.mp4"], cwd=work)
    return work / "out.mp4"


async def test_band_present_passes(tmp_path: Path) -> None:
    ov = _overlay()
    video = await _video(tmp_path, "ok", burn=ov)
    check = await verify_disclosure_overlay(video, ov)
    assert check.ok, check.details
    assert check.score > 0.95
    assert [f["t"] for f in check.details["frames"]] == [0.3, 1.5, 2.7]
    assert check.details["reference_opacity"]["mad"] < 1.0  # band design is background-independent


async def test_video_without_band_fails(tmp_path: Path) -> None:
    ov = _overlay()
    video = await _video(tmp_path, "none", burn=None)
    check = await verify_disclosure_overlay(video, ov)
    assert not check.ok
    assert check.score < 0.5
    assert any("not matched" in r for r in check.details["reasons"])


async def test_band_with_other_text_fails(tmp_path: Path) -> None:
    claimed = _overlay()
    burned = _overlay("협찬 · 제품을 제공받아 작성했습니다")
    video = await _video(tmp_path, "other", burn=burned)
    assert not (await verify_disclosure_overlay(video, claimed)).ok


async def test_band_removed_before_three_seconds_fails(tmp_path: Path) -> None:
    claimed = _overlay()
    short = copy.deepcopy(claimed)
    short["end"] = 1.0
    video = await _video(tmp_path, "short", burn=short)
    check = await verify_disclosure_overlay(video, claimed)
    assert not check.ok
    passed = {f["t"]: f["pass"] for f in check.details["frames"]}
    assert passed[0.3] and not passed[2.7]


async def test_metadata_without_ad_label_or_too_short_fails(tmp_path: Path) -> None:
    ov = _overlay()
    video = await _video(tmp_path, "meta", burn=ov)
    no_label = dict(ov, text="쿠팡 파트너스 활동으로 수수료를 받습니다")
    assert not (await verify_disclosure_overlay(video, no_label)).ok
    too_short = dict(ov, end=2.0)
    res = await verify_disclosure_overlay(video, too_short)
    assert not res.ok and any("ends at" in r for r in res.details["reasons"])
    unlocked = dict(ov, locked=False)
    assert not (await verify_disclosure_overlay(video, unlocked)).ok


async def test_unreadable_video_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"not a video")
    check = await verify_disclosure_overlay(bogus, _overlay())
    assert not check.ok and check.score == 0.0
