"""Font resolver: explicit file wins, a Korean-capable face is always found, thin variable fonts are skipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from acvm.core.errors import ConfigError
from acvm.media.fonts import (
    _is_thin_variable,
    font_search_dirs,
    libass_text_width,
    materialize_fonts_dir,
    pil_font,
    resolve_font,
)


def test_resolves_a_korean_capable_font() -> None:
    f = resolve_font()
    assert f.path.is_file() and f.family
    assert not _is_thin_variable(f.path)
    font = pil_font(f, 40, bold=True)
    assert font.getlength("광고 · 쿠팡") > 40  # Hangul glyphs have real advances (not tofu-width zero)


def test_explicit_font_file_wins_and_missing_file_raises(tmp_path: Path) -> None:
    f = resolve_font()
    chosen = resolve_font(str(f.path))
    assert chosen.path == f.path
    with pytest.raises(ConfigError):
        resolve_font(str(tmp_path / "nope.ttf"))


def test_materialize_fonts_dir_copies_only_the_render_faces(tmp_path: Path) -> None:
    f = resolve_font()
    d = materialize_fonts_dir(f, tmp_path / "fonts")
    names = sorted(p.name for p in d.iterdir())
    assert names == sorted(p.name for p in f.files())
    assert materialize_fonts_dir(f, tmp_path / "fonts") == d  # idempotent


def test_libass_width_scales_linearly() -> None:
    f = resolve_font()
    a, b = libass_text_width(f, "광고 표시", 40), libass_text_width(f, "광고 표시", 80)
    assert b == pytest.approx(2 * a, rel=1e-6)


def test_search_dirs_exist() -> None:
    assert all(d.is_dir() for d in font_search_dirs())
