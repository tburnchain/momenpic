"""Korean-capable font resolution shared by the ASS burner (libass), the card generator (Pillow) and the
D-03 verifier, so all three draw glyphs from the *same* file (§4 M6 자막 스타일 프리셋, §8.1 D-03).

Resolution order (first hit wins):

1. ``RenderSpec.font_file`` (explicit operator choice; must exist).
2. Static Noto Sans CJK KR / Noto Sans KR faces in the usual system and user font directories.
3. Windows ``malgun.ttf`` (+ ``malgunbd.ttf`` for bold).
4. Linux ``/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`` (+ Bold).

Variable fonts whose default instance is lighter than Regular (e.g. ``NotoSansKR-VF.ttf`` defaults to
*Thin*) are skipped: libass renders the default instance and ignores the ``wght`` axis, which would make
captions and the disclosure band unreadably thin.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from acvm.core.errors import ConfigError

_NOTO_REGULAR = (
    "NotoSansCJKkr-Regular.otf", "NotoSansCJKKR-Regular.otf", "NotoSansKR-Regular.otf", "NotoSansKR-Regular.ttf",
    "NotoSansCJK-Regular.ttc", "NotoSansCJKkr-Medium.otf", "NotoSansKR-Medium.ttf", "NotoSansKR-Medium.otf",
)
_NOTO_BOLD = (
    "NotoSansCJKkr-Bold.otf", "NotoSansCJKKR-Bold.otf", "NotoSansKR-Bold.otf", "NotoSansKR-Bold.ttf",
    "NotoSansCJK-Bold.ttc",
)
_MALGUN_REGULAR = ("malgun.ttf",)
_MALGUN_BOLD = ("malgunbd.ttf",)
_LINUX_CJK = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
)


@dataclass(frozen=True, slots=True)
class FontInfo:
    """A resolved font: the regular face, an optional bold face and the family name libass matches on."""

    path: Path
    family: str
    bold_path: Path | None = None
    index: int = 0
    bold_index: int = 0

    def files(self) -> tuple[Path, ...]:
        return (self.path,) if self.bold_path is None or self.bold_path == self.path else (self.path, self.bold_path)

    @property
    def fingerprint(self) -> str:
        """Stable identifier of the font files (name + size) — part of render cache keys."""
        h = hashlib.blake2b(digest_size=8)
        for f in self.files():
            h.update(f.name.encode())
            h.update(str(f.stat().st_size).encode() if f.exists() else b"0")
        return h.hexdigest()


def font_search_dirs() -> list[Path]:
    """System and user font directories for Windows, Linux and macOS (existing ones only)."""
    dirs: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    dirs.append(Path("C:/Windows/Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    home = Path.home()
    dirs += [
        Path("/usr/share/fonts/opentype/noto"), Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/noto-cjk"),
        Path("/usr/share/fonts/google-noto-cjk"), Path("/usr/share/fonts/opentype"), Path("/usr/share/fonts/truetype"),
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts", home / ".local" / "share" / "fonts",
        Path("/Library/Fonts"), Path("/System/Library/Fonts"), home / "Library" / "Fonts",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d).lower()
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


def _find(names: Iterable[str], dirs: Sequence[Path]) -> Path | None:
    for d in dirs:
        for n in names:
            p = d / n
            if p.is_file():
                return p
    return None


def _face_index(path: Path, prefer: str = "KR") -> int:
    """For a .ttc collection pick the face whose family name contains ``prefer`` (Korean glyph forms)."""
    if path.suffix.lower() != ".ttc":
        return 0
    for i in range(16):
        try:
            fam = ImageFont.truetype(str(path), 12, index=i).getname()[0] or ""
        except OSError:
            break
        if prefer.lower() in fam.lower():
            return i
    return 0


def _family(path: Path, index: int = 0) -> str:
    try:
        fam = ImageFont.truetype(str(path), 12, index=index).getname()[0]
    except OSError as exc:
        raise ConfigError(f"unreadable font file {path.name}") from exc
    return (fam or path.stem).replace(",", " ").strip()


def _is_thin_variable(path: Path) -> bool:
    """True for variable fonts whose default instance is lighter than Regular (libass would render it)."""
    try:
        font = ImageFont.truetype(str(path), 12)
        axes = font.get_variation_axes()
    except (OSError, AttributeError):
        return False
    for ax in axes:
        name = ax.get("name", b"")
        name_s = name.decode(errors="ignore") if isinstance(name, bytes) else str(name)
        if name_s.lower() in ("weight", "wght") and float(ax.get("default") or 400) < 400:
            return True
    return False


def _make(path: Path, bold: Path | None) -> FontInfo:
    idx = _face_index(path)
    bidx = _face_index(bold) if bold else 0
    return FontInfo(path=path, family=_family(path, idx), bold_path=bold, index=idx, bold_index=bidx)


@lru_cache(maxsize=16)
def resolve_font(font_file: str | None = None, family_hint: str = "Noto Sans CJK KR") -> FontInfo:
    """Resolve the render font per the module docstring order. Raises ConfigError when nothing usable exists."""
    if font_file:
        p = Path(font_file).expanduser()
        if not p.is_file():
            raise ConfigError(f"configured render.font_file does not exist: {p}")
        bold = None
        for cand in (p.with_name(p.name.replace("Regular", "Bold")), p.with_name(p.stem + "bd" + p.suffix)):
            if cand != p and cand.is_file():
                bold = cand
                break
        return _make(p, bold)
    dirs = font_search_dirs()
    # Prefer a face that matches the configured family hint by file name (e.g. "Noto Sans CJK KR").
    hint_key = family_hint.replace(" ", "").lower()
    for d in dirs:
        for p in sorted(d.glob("*")):
            stem = p.stem.replace("-", "").replace("_", "").lower()
            if (p.suffix.lower() in (".ttf", ".otf", ".ttc") and hint_key and stem.startswith(hint_key)
                    and "regular" in stem and not _is_thin_variable(p)):
                return _make(p, _find([p.name.replace("Regular", "Bold")], [p.parent]))
    regular = _find(_NOTO_REGULAR, dirs)
    if regular is not None and not _is_thin_variable(regular):
        return _make(regular, _find(_NOTO_BOLD, dirs))
    regular = _find(_MALGUN_REGULAR, dirs)
    if regular is not None:
        return _make(regular, _find(_MALGUN_BOLD, dirs))
    for p in _LINUX_CJK:
        if p.is_file():
            bold = p.with_name(p.name.replace("Regular", "Bold"))
            return _make(p, bold if bold.is_file() else None)
    raise ConfigError("no Korean-capable font found; set render.font_file (Noto Sans CJK KR recommended)")


def resolve_from_spec(font_file: str | None, family: str) -> FontInfo:
    """Convenience for callers holding a ``RenderSpec``."""
    return resolve_font(font_file, family)


def materialize_fonts_dir(font: FontInfo, dest: Path | None = None) -> Path:
    """Copy the font files into a small private directory for libass ``fontsdir``.

    Pointing libass at a system font directory would make it load every installed font on each render;
    a two-file directory keeps startup ~instant and guarantees the face that gets matched. When ``dest`` is
    omitted a content-addressed cache under the system temp dir is used (safe under concurrency: files are
    written to a temp name and atomically renamed).
    """
    if dest is None:
        dest = Path(tempfile.gettempdir()) / "acvm-fonts" / font.fingerprint
    dest.mkdir(parents=True, exist_ok=True)
    for src in font.files():
        dst = dest / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            continue
        tmp = dest / f".{src.name}.{os.getpid()}.tmp"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    return dest


def pil_font(font: FontInfo, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Pillow font object for cards. Uses the bold face when available, else the bold named instance of a
    variable font, else the regular face."""
    if bold and font.bold_path is not None:
        return ImageFont.truetype(str(font.bold_path), size, index=font.bold_index)
    f = ImageFont.truetype(str(font.path), size, index=font.index)
    if bold:
        with contextlib.suppress(OSError, AttributeError, ValueError):
            f.set_variation_by_name("Bold")
    return f


def libass_text_width(font: FontInfo, text: str, ass_font_size: int, *, bold: bool = True) -> float:
    """Approximate rendered width (px) of ``text`` under libass at ASS ``Fontsize`` ``ass_font_size``.

    libass sizes a face so that ascender − descender equals the ASS font size, whereas Pillow sizes by em;
    the ratio of the two converts a Pillow measurement into libass pixels.
    """
    ref = 100
    f = pil_font(font, ref, bold=bold)
    asc, desc = f.getmetrics()
    return float(f.getlength(text)) * ass_font_size / max(1, asc + desc)
