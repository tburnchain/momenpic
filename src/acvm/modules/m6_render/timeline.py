"""Renderer-neutral JSON timeline (§4 M6 "강제 정렬 → 자막 스타일·위치 프리셋 → JSON 타임라인 → FFmpeg 렌더",
§9.1 "JSON 타임라인 → 결정적 렌더").

:func:`build_timeline` turns script beats + TTS word timings + chosen ledger visuals into a plain, stable,
JSON-serialisable dict that fully determines the render:

* **Re-timing.** Beat boundaries move to where the narration actually is: each beat's scene starts just
  before its first spoken word (a short lead into the preceding pause). Beats without narration get a
  silent hold inserted into the audio (recorded as ``audio.segments``).
* **Length contract.** Total length is clamped to ``[RenderSpec.min_seconds, max_seconds]`` (20–25 s): a
  short narration is padded by holding the CTA scene; a long one is tightened with a tempo factor
  (≤ ``RenderSpec.max_tempo``, applied with ``atempo``) — beyond that the script is rejected.
* **Fast delivery.** Shorts never sound like slow narration: if the measured speaking rate is under
  ``RenderSpec.min_speech_sps`` the narration is sped up to ``target_speech_sps`` (same atempo path).
* **Visuals.** One scene per beat with a deterministic Ken Burns move seeded from the content id.
* **Text.** Caption cues (house rules, see :mod:`acvm.media.ass`), on-screen headlines for scenes whose
  visual does not already carry the text, and per-platform safe-area layouts (master + differing variants).
* **Disclosure.** The template layer *always* adds the locked disclosure overlay from :class:`GatePolicy`
  (text, 0 → 3 s, top band). No parameter can remove or shorten it (§4 M6 "템플릿에서 제거 불가").
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from acvm.config import GatePolicy, RenderSpec
from acvm.core.errors import ValidationFailed
from acvm.core.ids import canonical_json
from acvm.domain.models import ScriptBeat
from acvm.media.ass import CaptionRules, make_disclosure_overlay, split_cues, wrap_words
from acvm.media.caption_styles import onscreen_box_for
from acvm.media.safe_area import layout_for, safe_area_for, union
from acvm.media.selection import normalize_visual
from acvm.ports import WordTiming

TIMELINE_SCHEMA = "acvm.timeline/1"
MAX_TEMPO = 1.25
LEAD_IN_SEC = 0.12
MIN_SCENE_SEC = 0.5
MIN_TAIL_SEC = 0.6
TRAILING_PAUSE_SEC = 0.35
_LOCKED_KEYS = frozenset({"disclosure", "disclosure_overlay", "disclosure_text", "disclosure_seconds", "overlays"})


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """Render-side template parameters (the M4 template id selects one; unknown ids use the default)."""

    id: str
    cta_tail_sec: float = 1.2
    empty_beat_hold: tuple[float, float] = (1.0, 3.0)
    kenburns_zoom: tuple[float, float] = (1.06, 1.16)
    kenburns_pan: tuple[float, float] = (0.012, 0.03)
    card_zoom: tuple[float, float] = (1.0, 1.045)
    caption_rules: CaptionRules = field(default_factory=CaptionRules)


TEMPLATES: dict[str, TemplateSpec] = {
    "problem-solution-v3": TemplateSpec("problem-solution-v3"),
    "before-after-v1": TemplateSpec("before-after-v1", kenburns_zoom=(1.04, 1.12)),
    "price-twist-v1": TemplateSpec("price-twist-v1", cta_tail_sec=1.4),
    "listicle-v1": TemplateSpec("listicle-v1", kenburns_zoom=(1.05, 1.14)),
}
DEFAULT_TEMPLATE_ID = "problem-solution-v3"
_TUNABLE = {"cta_tail_sec", "kenburns_zoom", "kenburns_pan", "card_zoom"}


def template_spec(template_id: str, params: Mapping[str, Any] | None = None) -> tuple[TemplateSpec, list[str]]:
    """Resolve a template plus allowed overrides. Returns (spec, ignored_param_names); disclosure-related
    params are always ignored — the overlay is not a template parameter."""
    base = TEMPLATES.get(template_id) or replace(TEMPLATES[DEFAULT_TEMPLATE_ID], id=template_id)
    ignored: list[str] = []
    updates: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if k in _LOCKED_KEYS or k not in _TUNABLE:
            ignored.append(k)
            continue
        updates[k] = tuple(float(x) for x in v) if isinstance(v, (list, tuple)) else float(v)
    if "cta_tail_sec" in updates:
        updates["cta_tail_sec"] = min(3.0, max(MIN_TAIL_SEC, float(updates["cta_tail_sec"])))
    return (replace(base, **updates) if updates else base), sorted(ignored)


AI_SOURCE = "ai_generated_background"
AI_LABEL_TEXT = "AI 생성 장면"


@dataclass(frozen=True, slots=True)
class VisualRef:
    """The ledger visual chosen for one beat, as the timeline needs it."""

    asset_id: str
    uri: str
    kind: Literal["image", "video"]
    rights_source: str
    text_baked: bool = False  # the visual already shows this beat's text (hook/scene/benefit/CTA cards)
    aspect: float | None = None  # width / height when known
    generated_card: bool = False  # ACVM typographic card (gets the gentle card Ken Burns, never a pan)

    @property
    def fit(self) -> Literal["cover", "contain"]:
        if self.kind == "video" or self.aspect is None:
            return "cover"
        return "cover" if self.aspect <= 0.75 else "contain"


def _r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)


def assign_words(beats: Sequence[ScriptBeat], n_words: int) -> list[tuple[int, int]]:
    """Word index range [a, b) per beat. Exact when TTS returned one timing per 어절, else proportional."""
    expected = [len(b.narration.split()) for b in beats]
    ranges: list[tuple[int, int]] = []
    if sum(expected) == n_words:
        k = 0
        for c in expected:
            ranges.append((k, k + c))
            k += c
        return ranges
    chars = [len(b.narration.replace(" ", "")) for b in beats]
    total = max(1, sum(chars))
    acc = 0
    prev = 0
    for i, c in enumerate(chars):
        acc += c
        end = n_words if i == len(beats) - 1 else round(acc / total * n_words)
        end = max(prev + (1 if c and prev < n_words else 0), min(end, n_words))
        ranges.append((prev, end))
        prev = end
    return ranges


def _kenburns(seed: str, index: int, prev_type: str | None, *, card: bool,
              t: TemplateSpec) -> tuple[dict[str, float], str]:
    rng = random.Random(int.from_bytes(hashlib.blake2b(f"{seed}|kb|{index}".encode(), digest_size=8).digest(), "big"))
    if card:
        z = rng.uniform(*t.card_zoom)
        return {"s0": 1.0, "s1": _r(z, 4), "u0": 0.0, "u1": 0.0, "v0": 0.0, "v1": 0.0}, "zoom_in"
    types = ["zoom_in", "zoom_out", "pan_lr", "pan_rl"]
    if prev_type in types and rng.random() < 0.6:
        kind = {"zoom_in": "zoom_out", "zoom_out": "zoom_in", "pan_lr": "pan_rl", "pan_rl": "pan_lr"}[prev_type]
    else:
        kind = types[rng.randrange(4)]
    z = rng.uniform(*t.kenburns_zoom)
    pan = rng.uniform(*t.kenburns_pan)
    if kind == "zoom_in":
        kb = (1.0, z, 0.0, pan * 0.4, 0.0, -pan * 0.2)
    elif kind == "zoom_out":
        kb = (z, 1.0, pan * 0.4, 0.0, -pan * 0.2, 0.0)
    elif kind == "pan_lr":
        kb = (1.0 + pan * 2.4, 1.0 + pan * 2.4, -pan, pan, 0.0, 0.0)
    else:
        kb = (1.0 + pan * 2.4, 1.0 + pan * 2.4, pan, -pan, 0.0, 0.0)
    s0, s1, u0, u1, v0, v1 = kb
    # margin inequality s ≥ 1 + 2|u|, s ≥ 1 + 2|v| keeps the crop inside the image (no black edges)
    s0 = max(s0, 1 + 2 * abs(u0), 1 + 2 * abs(v0))
    s1 = max(s1, 1 + 2 * abs(u1), 1 + 2 * abs(v1))
    return {"s0": _r(s0, 4), "s1": _r(s1, 4), "u0": _r(u0, 4), "u1": _r(u1, 4), "v0": _r(v0, 4),
            "v1": _r(v1, 4)}, kind


def _onscreen_lines(text: str) -> list[str]:
    parts = [p.strip(" .") for p in text.replace("|", "·").split("·") if p.strip(" .")]
    if len(parts) >= 2:
        return [wrap_words(p, 14, 1)[0] for p in parts[:3]]
    return wrap_words(text.strip(), 12, 2)


PAUSE_CAP_S = 0.25  # a silence longer than this between words counts as only this much speaking time


def speaking_rate(words: Sequence[WordTiming], *, beat_starts: Collection[int] = (),
                  pause_cap: float = PAUSE_CAP_S) -> float:
    """Korean syllables per second of delivery (articulation rate).

    Speaking time = voiced word time + the silences between words of the same beat, each capped at ``pause_cap``.
    The cut between two beats (before a word index in ``beat_starts``) is a scene change, not slow talking; a
    drawled voice with long gaps inside its sentences still measures slow.
    """
    from acvm.media.korean import estimate_syllables

    if not words:
        return 0.0
    voiced = sum(max(0.0, w.end - w.start) for w in words)
    gaps = sum(min(pause_cap, max(0.0, words[k].start - words[k - 1].end))
               for k in range(1, len(words)) if k not in beat_starts)
    syllables = sum(estimate_syllables(w.text) for w in words)
    span = voiced + gaps
    return syllables / span if span > 0 else 0.0


def speech_rate_tempo(words: Sequence[WordTiming], spec: RenderSpec, *,
                      beat_starts: Collection[int] = ()) -> tuple[float, float]:
    """(tempo, measured rate): tempo > 1 lifts a delivery slower than ``min_speech_sps`` to ``target_speech_sps``."""
    measured = speaking_rate(words, beat_starts=beat_starts)
    floor = float(getattr(spec, "min_speech_sps", 0.0) or 0.0)
    target = max(floor, float(getattr(spec, "target_speech_sps", floor) or floor))
    if measured <= 0 or floor <= 0 or measured >= floor:
        return 1.0, measured
    return target / measured, measured


def build_timeline(*, content_id: str, template_id: str, beats: Sequence[ScriptBeat], words: Sequence[WordTiming],
                   narration_duration: float, visuals: Mapping[int, VisualRef], spec: RenderSpec, gate: GatePolicy,
                   font_family: str, font_file: str = "", platforms: Sequence[str] | None = None,
                   voice: Mapping[str, str] | None = None, template_params: Mapping[str, Any] | None = None,
                   measure: Callable[[str, int], tuple[float, float]] | None = None,
                   seed: str | None = None, style: Mapping[str, Any] | None = None,
                   ai_label_text: str = AI_LABEL_TEXT) -> dict[str, Any]:
    """Build the timeline dict (see module docstring). Raises ValidationFailed when the script cannot fit.

    Scenes showing generated imagery (``ai_generated_background``) get an on-screen ``ai_label_text`` for their whole
    duration (``timeline["ai_labels"]``) — like the disclosure band, it is not a style option.

    ``style`` is the resolved Studio options view (:meth:`acvm.media.render_options.RenderOptions.resolved` plus
    ``options_fingerprint``); it is recorded verbatim as ``timeline["style"]`` and decides where headlines go.
    It cannot influence the disclosure overlay.
    """
    if not beats:
        raise ValidationFailed("script has no beats")
    missing = [i for i in range(len(beats)) if i not in visuals]
    if missing:
        raise ValidationFailed("beats without a ledger visual", beats=missing)
    tmpl, ignored = template_spec(template_id, template_params)
    fps = spec.fps
    seed = seed or content_id
    ranges = assign_words(beats, len(words))
    narrated = [i for i, (a, b) in enumerate(ranges) if b > a]
    if not narrated:
        raise ValidationFailed("script has no narration")

    # ── tempo: fast-delivery floor, then length tightening ──
    lo_hold, hi_hold = tmpl.empty_beat_hold
    holds = {i: min(hi_hold, max(lo_hold, b.end_sec - b.start_sec)) for i, b in enumerate(beats) if i not in narrated}
    hold_total = sum(holds.values())
    speech_end_raw = max((w.end for w in words), default=0.0)
    max_tempo = max(1.0, float(getattr(spec, "max_tempo", MAX_TEMPO)))
    # (1) Shorts must sound fast and concise. Whatever the provider produced, the measured articulation rate
    #     (syllables over voiced time + capped in-beat pauses) is lifted to the target when it falls under the
    #     floor. A voice that cannot reach it even at max_tempo is a slow voice and is refused.
    rate_tempo, measured_sps = speech_rate_tempo(words, spec, beat_starts={ranges[i][0] for i in narrated})
    if rate_tempo > max_tempo:
        raise ValidationFailed("narration delivered too slowly for a fast short even with tempo correction",
                               measured_sps=_r(measured_sps), floor=spec.min_speech_sps, max_tempo=max_tempo)
    tempo = rate_tempo
    # (2) Fit the 20–25 s window.
    budget = spec.max_seconds - hold_total - MIN_TAIL_SEC - 0.05
    if speech_end_raw / tempo + hold_total + tmpl.cta_tail_sec > spec.max_seconds:
        tempo = max(tempo, speech_end_raw / max(budget, 1e-6))
        if tempo > max_tempo:
            raise ValidationFailed(f"narration too long for the {spec.max_seconds:g} s limit even at ×{max_tempo:g} "
                                   "tempo — shorten the script", speech_seconds=_r(speech_end_raw),
                                   limit=spec.max_seconds)
    tempo = max(1.0, math.ceil(tempo * 1000) / 1000)
    sw = [WordTiming(w.text, w.start / tempo, w.end / tempo) for w in words]
    speech_end = speech_end_raw / tempo
    audio_len = max(narration_duration / tempo, speech_end)

    # ── cut points in (tempo-scaled) source time ──
    cut: dict[int, float] = {}
    for n_i, i in enumerate(narrated):
        a = ranges[i][0]
        if n_i == 0:
            cut[i] = 0.0
            continue
        s, prev_end = sw[a].start, sw[a - 1].end
        cut[i] = max(prev_end, s - min(LEAD_IN_SEC, max(0.0, s - prev_end) * 0.5))
    insert_at: dict[int, float] = {}
    for j in holds:
        nxt = next((i for i in narrated if i > j), None)
        insert_at[j] = cut[nxt] if nxt is not None else min(audio_len, speech_end + TRAILING_PAUSE_SEC)

    def to_dst(src: float, *, inclusive: bool = True) -> float:
        return src + sum(h for j, h in holds.items() if (insert_at[j] <= src if inclusive else insert_at[j] < src))

    starts: list[float] = []
    for i in range(len(beats)):
        if i in cut:
            starts.append(to_dst(cut[i]))
        else:
            p = insert_at[i]
            before = sum(h for j, h in holds.items() if insert_at[j] < p or (insert_at[j] == p and j < i))
            starts.append(p + before)
    trailing = [j for j in holds if insert_at[j] >= speech_end]
    total = (max(starts[j] + holds[j] for j in trailing) if trailing
             else to_dst(speech_end) + tmpl.cta_tail_sec)
    total = max(total, spec.min_seconds)
    total_frames = math.ceil(total * fps - 1e-6)
    total_frames = min(total_frames, math.floor(spec.max_seconds * fps + 1e-6))

    # ── frame-snapped scenes ──
    min_frames = max(1, round(MIN_SCENE_SEC * fps))
    fstarts = [0]
    for i in range(1, len(beats)):
        f = max(round(starts[i] * fps), fstarts[-1] + min_frames)
        fstarts.append(min(f, total_frames - min_frames * (len(beats) - i)))
    fends = [*fstarts[1:], total_frames]
    scenes: list[dict[str, Any]] = []
    prev_kb: str | None = None
    for i, b in enumerate(beats):
        v = visuals[i]
        is_card = v.generated_card or v.text_baked
        kb, kb_type = (None, "none") if v.kind == "video" else _kenburns(seed, i, prev_kb, card=is_card, t=tmpl)
        if kb is not None and not is_card:
            prev_kb = kb_type
        a, z = ranges[i]
        scenes.append({
            "index": i, "role": b.role, "visual": normalize_visual(b), "start": _r(fstarts[i] / fps, 4),
            "end": _r(fends[i] / fps, 4), "frames": fends[i] - fstarts[i], "asset_id": v.asset_id, "asset_uri": v.uri,
            "kind": v.kind, "fit": v.fit, "rights_source": v.rights_source, "text_baked": v.text_baked,
            "kenburns": kb, "kenburns_type": kb_type, "words": [a, z],
        })

    # ── words, captions, on-screen text ──
    dst_words = [WordTiming(w.text, _r(to_dst(w.start)), _r(max(to_dst(w.start), to_dst(w.end, inclusive=False))))
                 for w in sw]
    hard = {ranges[i][0] for i in narrated}
    duration = total_frames / fps
    cues = split_cues(dst_words, tmpl.caption_rules, hard_breaks=hard, end_limit=duration)
    platform_list = [p for p in (platforms or []) if p in ("instagram", "tiktok", "youtube", "facebook", "threads")]
    master = layout_for(union(platform_list), spec.width, spec.height)
    style_rec: dict[str, Any] = {"caption_style": "classic", "caption_position": "bottom", "captions_enabled": True,
                                 "watermark": "", **dict(style or {})}
    position = str(style_rec.get("caption_position") or "bottom")
    onscreen: list[dict[str, Any]] = []
    for sc, b in zip(scenes, beats, strict=True):
        if sc["text_baked"] or not b.on_screen_text.strip():
            continue
        lines = _onscreen_lines(b.on_screen_text)
        if not lines:
            continue
        onscreen.append({"start": _r(sc["start"] + 0.15), "end": _r(max(sc["start"] + 0.5, sc["end"] - 0.1)),
                         "lines": lines, "box": list(onscreen_box_for(master, position, spec.height)),
                         "scene": sc["index"]})
    variants = {}
    for plat in platform_list:
        lp = layout_for(safe_area_for(plat), spec.width, spec.height)
        if lp.differs_from(master):
            variants[plat] = {"layout": lp.as_dict()}

    ai_labels = [{"scene": sc["index"], "start": sc["start"], "end": sc["end"], "text": ai_label_text}
                 for sc in scenes if str(sc["rights_source"]) == AI_SOURCE and ai_label_text.strip()]

    # ── template layer: the locked disclosure overlay (never optional) ──
    overlay = make_disclosure_overlay(gate.video_disclosure, seconds=max(gate.disclosure_overlay_seconds, 3.0),
                                      width=spec.width, height=spec.height, font_family=font_family,
                                      measure=measure, font_file=font_file)

    # ── audio placement ──
    points = sorted({p for p in insert_at.values() if 0.0 < p < audio_len})
    bounds = [0.0, *points, audio_len]
    segments = [{"src_start": _r(bounds[k], 4), "src_end": _r(bounds[k + 1], 4),
                 "dst_start": _r(to_dst(bounds[k]), 4)} for k in range(len(bounds) - 1)]

    return {
        "schema": TIMELINE_SCHEMA,
        "content_id": str(content_id),
        "template": {"id": tmpl.id, "cta_tail_sec": tmpl.cta_tail_sec, "ignored_params": ignored},
        "canvas": {"w": spec.width, "h": spec.height, "fps": fps},
        "duration": _r(duration, 4),
        "frames": total_frames,
        "audio": {"tempo": _r(tempo, 4), "source_duration": _r(narration_duration, 4), "segments": segments,
                  "speech_sps_measured": _r(measured_sps), "speech_sps_final": _r(measured_sps * tempo),
                  "target_lufs": spec.target_lufs, "true_peak": spec.true_peak},
        "voice": dict(voice or {}),
        "scenes": scenes,
        "words": [[w.text, w.start, w.end] for w in dst_words],
        "captions": [c.as_dict() for c in cues],
        "onscreen": onscreen,
        "overlays": {"disclosure": overlay},
        "layout": master.as_dict(),
        "variants": variants,
        "style": style_rec,
        **({"ai_labels": ai_labels} if ai_labels else {}),
    }


def timeline_bytes(timeline: Mapping[str, Any]) -> bytes:
    """Canonical (sorted-key, compact) JSON bytes — identical inputs give identical bytes."""
    return canonical_json(dict(timeline))


def assert_renderable(timeline: Mapping[str, Any], spec: RenderSpec) -> None:
    """Invariants the renderer relies on (raises ValidationFailed)."""
    overlay = (timeline.get("overlays") or {}).get("disclosure") or {}
    if not overlay.get("locked") or "광고" not in str(overlay.get("text", "")):
        raise ValidationFailed("timeline lacks the locked disclosure overlay")
    if float(overlay.get("start", 1.0)) > 0.0 or float(overlay.get("end", 0.0)) < 3.0:
        raise ValidationFailed("disclosure overlay must cover 0–3 s")
    dur = float(timeline["duration"])
    if not spec.min_seconds - 1e-6 <= dur <= spec.max_seconds + 1e-6:
        raise ValidationFailed("timeline duration outside 15–45 s", duration=dur)
    frames = [int(s["frames"]) for s in timeline["scenes"]]
    if sum(frames) != int(timeline["frames"]) or min(frames) <= 0:
        raise ValidationFailed("scene frames do not tile the timeline")
