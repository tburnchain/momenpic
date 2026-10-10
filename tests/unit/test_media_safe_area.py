"""Per-platform safe areas → caption margins / on-screen boxes (§4 M6 플랫폼 변형)."""

from __future__ import annotations

import pytest

from acvm.media.safe_area import (
    DISCLOSURE_BAND_PX,
    SAFE_AREAS,
    caption_margin_v,
    caption_margins_lr,
    card_text_box,
    layout_for,
    layout_from_dict,
    onscreen_box,
    safe_area_for,
    union,
)


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "youtube", "facebook", "threads"])
def test_every_vertical_platform_has_a_sane_safe_area(platform: str) -> None:
    sa = SAFE_AREAS[platform]
    x, y, w, h = sa.rect()
    assert 0 <= x < 200 and 0 <= y < 300
    assert w > 700 and h > 1200
    assert caption_margin_v(sa) > round(sa.bottom * 1920)


def test_union_is_most_conservative() -> None:
    u = union(None)
    for sa in SAFE_AREAS.values():
        assert u.top >= sa.top and u.bottom >= sa.bottom and u.left >= sa.left and u.right >= sa.right
    two = union(["threads", "instagram"])
    assert two.bottom == SAFE_AREAS["instagram"].bottom
    assert safe_area_for("kakao") == union(None)
    assert safe_area_for("tiktok") is SAFE_AREAS["tiktok"]


def test_captions_clear_bottom_ui_and_action_rail() -> None:
    for sa in SAFE_AREAS.values():
        ml, mr = caption_margins_lr(sa)
        assert mr >= round(sa.right * 1080)
        assert ml >= round(sa.left * 1080)
    assert caption_margin_v(SAFE_AREAS["tiktok"]) > caption_margin_v(SAFE_AREAS["threads"])


def test_onscreen_and_card_boxes_stay_below_disclosure_band_and_above_captions() -> None:
    for sa in [*SAFE_AREAS.values(), union(None)]:
        x, y, w, h = onscreen_box(sa)
        assert y >= DISCLOSURE_BAND_PX
        assert y + h < 1920 - caption_margin_v(sa)
        assert x + w <= 1080 - round(sa.right * 1080)
        _cx, cy, _cw, ch = card_text_box(sa)
        assert cy >= DISCLOSURE_BAND_PX and cy + ch < 1920 - caption_margin_v(sa)


def test_layout_roundtrip_and_difference() -> None:
    master = layout_for(union(["instagram", "threads"]))
    threads = layout_for(SAFE_AREAS["threads"])
    assert layout_from_dict(master.as_dict()) == master
    assert threads.differs_from(master)
    assert not layout_for(SAFE_AREAS["instagram"]).differs_from(layout_for(SAFE_AREAS["instagram"]))
