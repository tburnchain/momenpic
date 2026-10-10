"""Studio render options: validation and fingerprint semantics (re-render only when something changed)."""

from __future__ import annotations

import uuid

import pytest

from acvm.core.errors import ValidationFailed
from acvm.media.render_options import OPTION_KEYS, RenderOptions, parse_render_options


def test_defaults_and_full_parse() -> None:
    assert parse_render_options(None) == RenderOptions()
    aid = uuid.uuid4()
    o = parse_render_options({"voice_id": "ko-M-40s-punchy", "caption_style": "pink_pop", "caption_position": "upper",
                              "captions_enabled": False, "watermark": " @kitchen_pick\n", "beat_assets": {"2": str(aid)}},
                             n_beats=6)
    assert o.voice_id == "ko-M-40s-punchy" and o.caption_style == "pink_pop" and o.caption_position == "upper"
    assert o.captions_enabled is False and o.watermark == "@kitchen_pick"
    assert o.beat_asset_ids() == {2: aid}
    r = o.resolved(voice_id="ko-M-40s-punchy")
    assert r["voice_overridden"] is True and r["beat_assets"] == {"2": str(aid)}
    assert {"voice_id", "caption_style", "caption_position", "captions_enabled", "watermark",
                           "beat_assets"} == OPTION_KEYS


@pytest.mark.parametrize("raw", [
    {"voice_id": "celebrity-clone"},
    {"caption_style": "comic-sans"},
    {"caption_position": "left"},
    {"captions_enabled": "maybe"},
    {"watermark": 42},
    {"beat_assets": ["x"]},
    {"beat_assets": {"two": str(uuid.uuid4())}},
    {"beat_assets": {"1": "not-a-uuid"}},
    {"beat_assets": {"9": str(uuid.uuid4())}},
    {"disclosure": False},
])
def test_invalid_options_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        parse_render_options(raw, n_beats=6)


def test_fingerprint_tracks_only_meaningful_changes() -> None:
    base = parse_render_options({}).fingerprint(voice_id="ko-F-30s-brisk")
    explicit = parse_render_options({"caption_style": "classic", "caption_position": "bottom", "captions_enabled": True,
                                     "watermark": "", "voice_id": "ko-F-30s-brisk", "beat_assets": {}})
    assert explicit.fingerprint(voice_id="ko-F-30s-brisk") == base  # explicit defaults == unset
    assert parse_render_options({}).fingerprint(voice_id="ko-M-30s-brisk") != base  # persona voice changed
    for change in ({"caption_style": "minimal"}, {"caption_position": "middle"}, {"captions_enabled": False},
                   {"watermark": "@a"}, {"beat_assets": {"0": str(uuid.uuid4())}}):
        assert parse_render_options(change).fingerprint(voice_id="ko-F-30s-brisk") != base, change
