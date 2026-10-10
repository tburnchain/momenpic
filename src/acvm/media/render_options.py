"""Studio render options (``Content.render_options``) — parsing, validation and fingerprinting.

Keys (all optional; anything unset means "M6 decides automatically", exactly as without the Studio):

* ``voice_id`` — catalog voice (:data:`acvm.media.voices.VOICE_CATALOG`); unknown ids are rejected.
* ``caption_style`` — :data:`acvm.media.caption_styles.CAPTION_STYLES` id (default ``classic``).
* ``caption_position`` — ``bottom`` | ``middle`` | ``upper`` (default ``bottom``).
* ``captions_enabled`` — bool (default True; "자막 없이 생성").
* ``watermark`` — channel name shown small at the top-right (≤ 24 printable characters; longer is cut).
* ``beat_assets`` — ``{"<beat index>": "<ledger asset uuid>"}`` visual overrides (validated against the rights
  ledger by :func:`acvm.media.selection.load_beat_overrides`).

The disclosure overlay is deliberately *not* an option (§4 M6: 템플릿에서 제거 불가). The fingerprint covers the
resolved options (incl. the voice actually used) so a changed option re-renders and an unchanged one does not.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from acvm.core.errors import ValidationFailed
from acvm.core.ids import stable_hash
from acvm.media.caption_styles import (
    CAPTION_POSITIONS,
    DEFAULT_CAPTION_POSITION,
    DEFAULT_CAPTION_STYLE,
    caption_style,
    sanitize_watermark,
)
from acvm.media.voices import get_voice

OPTION_KEYS = frozenset({"voice_id", "caption_style", "caption_position", "captions_enabled", "watermark",
                         "beat_assets"})


@dataclass(frozen=True, slots=True)
class RenderOptions:
    voice_id: str | None = None
    caption_style: str = DEFAULT_CAPTION_STYLE
    caption_position: str = DEFAULT_CAPTION_POSITION
    captions_enabled: bool = True
    watermark: str = ""
    beat_assets: tuple[tuple[int, str], ...] = ()  # sorted (beat index, asset uuid)

    def beat_asset_ids(self) -> dict[int, uuid.UUID]:
        return {i: uuid.UUID(a) for i, a in self.beat_assets}

    def resolved(self, *, voice_id: str) -> dict[str, Any]:
        """JSON-able, fully resolved view (recorded as ``timeline["style"]``)."""
        return {"voice_id": voice_id, "voice_overridden": self.voice_id is not None,
                "caption_style": self.caption_style, "caption_position": self.caption_position,
                "captions_enabled": self.captions_enabled, "watermark": self.watermark,
                "beat_assets": {str(i): a for i, a in self.beat_assets}}

    def fingerprint(self, *, voice_id: str) -> str:
        r = self.resolved(voice_id=voice_id)
        r.pop("voice_overridden")
        return stable_hash(r, length=16)


def _bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0"):
        return v.strip().lower() in ("true", "1")
    raise ValidationFailed(f"render option {key} must be a boolean")


def parse_render_options(raw: Mapping[str, Any] | None, *, n_beats: int | None = None) -> RenderOptions:
    """Validate ``Content.render_options``. Raises ValidationFailed with a precise reason."""
    data = dict(raw or {})
    unknown = sorted(set(data) - OPTION_KEYS)
    if unknown:
        raise ValidationFailed("unknown render options", keys=unknown, allowed=sorted(OPTION_KEYS))
    voice = data.get("voice_id")
    voice_id = None
    if voice not in (None, ""):
        voice_id = get_voice(str(voice)).id
    style_id = data.get("caption_style") or DEFAULT_CAPTION_STYLE
    try:
        style_id = caption_style(str(style_id)).id
    except KeyError:
        raise ValidationFailed("unknown caption style", caption_style=style_id) from None
    position = data.get("caption_position") or DEFAULT_CAPTION_POSITION
    if position not in CAPTION_POSITIONS:
        raise ValidationFailed("unknown caption position", caption_position=position, allowed=list(CAPTION_POSITIONS))
    enabled = _bool(data["captions_enabled"], "captions_enabled") if data.get("captions_enabled") is not None else True
    mark = data.get("watermark")
    if mark is not None and not isinstance(mark, str):
        raise ValidationFailed("render option watermark must be a string")
    beats_raw = data.get("beat_assets") or {}
    if not isinstance(beats_raw, Mapping):
        raise ValidationFailed("render option beat_assets must be an object {beat index: asset id}")
    pairs: list[tuple[int, str]] = []
    for k, v in beats_raw.items():
        try:
            idx = int(str(k))
            aid = str(uuid.UUID(str(v)))
        except (TypeError, ValueError):
            raise ValidationFailed("beat_assets entries must map a beat index to an asset uuid",
                                   beat=str(k)) from None
        if idx < 0 or (n_beats is not None and idx >= n_beats):
            raise ValidationFailed("beat_assets index out of range", beat=idx, beats=n_beats)
        pairs.append((idx, aid))
    return RenderOptions(voice_id=voice_id, caption_style=style_id, caption_position=str(position),
                         captions_enabled=enabled, watermark=sanitize_watermark(mark), beat_assets=tuple(sorted(pairs)))
