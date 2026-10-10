"""Deterministic Pillow cards (§4 M5 generated cards; same spec → identical PNG bytes)."""

from __future__ import annotations

import hashlib
import io

import numpy as np
from PIL import Image

from acvm.media.cards import (
    DEFAULT_CTA,
    benefit_card,
    card_background_hex,
    cta_card,
    hook_card,
    product_card,
    render_card,
    scene_card,
)

SPECS = [
    hook_card("과자 봉지 아직도 이렇게 막아두세요?", seed="product:1"),
    scene_card("하루만 지나도 눅눅해져서 결국 버리게 되죠", seed="product:1"),
    benefit_card("이런 점이 좋아요", ["20개입 넉넉한 구성", "길이 11cm", "리뷰 3,812개"], seed="product:1"),
    cta_card(subtitle="과자 밀봉 클립 20개입", seed="product:1"),
    product_card("과자 밀봉 클립 20개입 대용량 세트", 12_900, seed="product:1"),
]


def _img(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def test_cards_are_byte_identical_for_identical_specs() -> None:
    for spec in SPECS:
        a, b = render_card(spec), render_card(spec)
        assert a == b
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_cards_are_1080x1920_png_without_metadata() -> None:
    for spec in SPECS:
        png = render_card(spec)
        assert png.startswith(b"\x89PNG")
        with Image.open(io.BytesIO(png)) as im:
            assert im.size == (1080, 1920)
            assert im.mode == "RGB"
            assert not {k for k in im.info if k not in ("dpi",)} - {"gamma", "srgb"}


def test_top_band_region_is_calm() -> None:
    """The top 220 px is one flat colour (the locked disclosure band covers it for 3 s)."""
    for spec in SPECS:
        top = _img(render_card(spec))[:220]
        assert top.reshape(-1, 3).std(axis=0).max() < 0.5


def test_text_is_drawn_and_differs_by_content() -> None:
    a = render_card(hook_card("첫 번째 훅", seed="s"))
    b = render_card(hook_card("두 번째 훅", seed="s"))
    assert a != b
    arr = _img(a)
    assert arr[300:1300].std() > 5  # glyphs present in the text box


def test_palette_is_seed_deterministic() -> None:
    assert card_background_hex("product:1") == card_background_hex("product:1")
    assert {card_background_hex(f"product:{i}") for i in range(40)} != {card_background_hex("product:1")}


def test_cta_default_headline_and_text_stays_clear_of_ui_zones() -> None:
    spec = cta_card(seed="x")
    assert spec.title == DEFAULT_CTA
    arr = _img(render_card(spec)).astype(np.int16)
    bg_right = arr[:, -100:]  # right action-rail zone (≥ 0.13 × width): only background gradient
    row_std = bg_right.std(axis=1).max()
    assert row_std < 2.0
