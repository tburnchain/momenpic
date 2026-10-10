"""M6 Render Engine service (§4 M6, §6.3 ``render.done``, §8.1 D-03/R-01, §9.4 비용 모델).

``RenderService.render(content_id)`` is idempotent per (content, render-options fingerprint): when the content
already carries render metadata whose media object still exists *and* whose stored timeline records the same
Studio options fingerprint (``Content.render_options``: voice, caption style/position, captions on/off,
watermark, beat asset overrides — see :mod:`acvm.media.render_options`), that metadata is returned untouched
(no re-render, no second ``render.done``). Otherwise it

1. validates the options (unknown voice/style/position → ValidationFailed), loads the content (short-form only)
   and its product, resolves operator beat overrides against the rights ledger (R-01; AI imagery never stands
   in for the product) and one **ledger** visual per remaining beat via the shared
   :func:`acvm.media.selection.resolve_visuals` (M6 never imports M5; generated cards are made with
   :mod:`acvm.media.cards`, clear of the chosen caption band, and registered in the ledger);
2. synthesises narration with the chosen/persona voice (:func:`~acvm.modules.m6_render.tts.build_tts`), builds
   the JSON timeline (style options recorded in ``timeline["style"]``; the template layer always adds the
   locked disclosure overlay, which no option can change), writes ASS and renders with FFmpeg;
3. probes the output, measures loudness, **verifies the disclosure band on decoded frames** (failure raises
   — a short without a provable "광고" band never leaves M6), grabs a thumbnail at ~3.5 s;
4. uploads ``media/{content_id}/{sha}.mp4`` / ``.jpg`` (+ the timeline JSON), persists ``RenderMeta`` and the
   ``content_asset`` rows (``beat:{i}``, ``voice``), charges the cost ledger (key ``{content_id}:render:{fp}``)
   and emits ``render.done`` with the
   idempotency key ``{content_id}:render:{sha}`` through the transactional outbox.

Renders are bounded by ``asyncio.Semaphore(settings.render.concurrency)`` shared per context and serialised
per content id.
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from acvm.core.clock import to_kst
from acvm.core.enums import AssetSource, ContentKind, ContentStatus, Platform
from acvm.core.errors import ACVMError, BudgetExceeded, NotFound, PolicyViolation, ValidationFailed
from acvm.core.events import Event, RenderDone, ScriptReady
from acvm.core.logging import get_logger
from acvm.core.metrics import RENDER_SECONDS, STAGE_DURATION_SECONDS
from acvm.domain.models import Asset, Content, Product, RenderMeta
from acvm.media.caption_styles import card_text_box_for
from acvm.media.cards import card_background_hex
from acvm.media.ffmpeg import extract_frame, measure_loudness, probe, sha256_file
from acvm.media.fonts import FontInfo, libass_text_width, resolve_font
from acvm.media.render_options import RenderOptions, parse_render_options
from acvm.media.selection import ledger_violations, load_beat_overrides, resolve_visuals
from acvm.media.verify import verify_disclosure_overlay
from acvm.media.voices import get_voice, voice_asset, voice_id_for_persona
from acvm.modules.m6_render.render import FFmpegRenderer, RenderArtifacts
from acvm.modules.m6_render.timeline import VisualRef, build_timeline, timeline_bytes
from acvm.modules.m6_render.tts import build_tts

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

THUMBNAIL_AT_SEC = 3.5
_SEM_KEY = "m6.render_semaphore"
_LOCKS_KEY = "m6.content_locks"


class DisclosureVerificationFailed(PolicyViolation):
    """The rendered file does not provably show the locked disclosure band (§8.1 D-03)."""

    code = "disclosure_verification_failed"


class RenderService:
    """§4 M6 Render Engine bound to one :class:`~acvm.context.AppContext`."""

    def __init__(self, ctx: AppContext, *, renderer: FFmpegRenderer | None = None) -> None:
        self.ctx = ctx
        self.spec = ctx.settings.render
        self.renderer = renderer or FFmpegRenderer(self.spec)

    # ── shared per-context primitives ──

    def _semaphore(self) -> asyncio.Semaphore:
        sem = self.ctx.extras.get(_SEM_KEY)
        if sem is None:
            sem = asyncio.Semaphore(max(1, self.spec.concurrency))
            self.ctx.extras[_SEM_KEY] = sem
        return sem

    def _lock_for(self, content_id: uuid.UUID) -> asyncio.Lock:
        locks: dict[uuid.UUID, asyncio.Lock] = self.ctx.extras.setdefault(_LOCKS_KEY, {})
        lock = locks.get(content_id)
        if lock is None:
            lock = locks[content_id] = asyncio.Lock()
        return lock

    def _font(self) -> FontInfo:
        return resolve_font(self.spec.font_file, self.spec.font_family)

    async def _object_exists(self, uri: str) -> bool:
        if not uri:
            return False
        try:
            path = await self.ctx.objects.local_path(uri)
        except Exception:  # missing/unreachable object simply means "render again"
            return False
        return path.is_file() and path.stat().st_size > 0

    # ── public API ──

    async def handle_script_ready(self, event: Event) -> RenderMeta | None:
        """``script.ready`` consumer: render short-form content; text posts are not rendered."""
        payload = event.parsed()
        if not isinstance(payload, ScriptReady):
            raise ValidationFailed("unexpected event type for M6", type=event.type)
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(payload.content_id)
        if content is None:
            raise NotFound("content not found", content_id=str(payload.content_id))
        if content.kind is not ContentKind.SHORT:
            return None
        return await self.render(payload.content_id)

    async def render(self, content_id: uuid.UUID, *, platforms: Sequence[str] | None = None) -> RenderMeta:
        """Render (or return the existing render of) ``content_id``. See module docstring."""
        async with self._lock_for(content_id):
            content, product, target_platform = await self._load(content_id)
            opts = parse_render_options(content.render_options, n_beats=len(content.script.beats))
            voice_id = get_voice(opts.voice_id).id if opts.voice_id else voice_id_for_persona(content.persona)
            fingerprint = opts.fingerprint(voice_id=voice_id)
            if content.render is not None and await self._object_exists(content.render.media_uri):
                stored = await self._stored_fingerprint(content)
                if stored == fingerprint:
                    log.info("render_reused", content_id=str(content_id), sha256=content.render.sha256)
                    return content.render
                log.info("render_options_changed", content_id=str(content_id), previous=stored, current=fingerprint)
            await self._precheck_budget(content)
            plats = [str(p) for p in platforms] if platforms else ([target_platform] if target_platform else [])
            async with self._semaphore():
                started = time.perf_counter()
                meta, visuals, voice = await self._render_new(content, product, plats, opts, voice_id, fingerprint)
                elapsed = time.perf_counter() - started
            RENDER_SECONDS.observe(elapsed)
            return await self._persist(content, meta, visuals, voice, elapsed, fingerprint)

    async def _stored_fingerprint(self, content: Content) -> str:
        """Options fingerprint recorded in the existing render's timeline (renders that predate Studio options
        count as default options with the persona voice)."""
        default = RenderOptions().fingerprint(voice_id=voice_id_for_persona(content.persona))
        assert content.render is not None
        uri = content.render.media_uri
        if not uri.endswith(".mp4"):
            return default
        try:
            raw = await self.ctx.objects.get_bytes(uri[: -len(".mp4")] + ".timeline.json")
            style = json.loads(raw).get("style") or {}
        except Exception:  # timeline missing/unreadable → treat as default options
            return default
        return str(style.get("options_fingerprint") or default)

    # ── steps ──

    async def _load(self, content_id: uuid.UUID) -> tuple[Content, Product, str | None]:
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(content_id)
            if content is None:
                raise NotFound("content not found", content_id=str(content_id))
            if content.kind is not ContentKind.SHORT:
                raise ValidationFailed("render applies to short-form content only", content_id=str(content_id),
                                       kind=content.kind.value)
            if not content.script.beats:
                raise ValidationFailed("short has no script beats", content_id=str(content_id))
            product = await repos.products.get(content.product_id)
            if product is None:
                raise NotFound("product not found", product_id=content.product_id)
            platform: str | None = None
            if content.target_account_id is not None:
                acc = await repos.accounts.get(content.target_account_id)
                if acc is not None and acc.platform is not Platform.KAKAO:
                    platform = acc.platform.value
        return content, product, platform

    def _estimated_cost(self, duration: float) -> float:
        c = self.ctx.settings.costs
        return c.tts_per_30s_krw * duration / 30.0 + c.render_per_short_krw + c.storage_per_short_krw

    async def _precheck_budget(self, content: Content) -> None:
        est_dur = min(self.spec.max_seconds, max(self.spec.min_seconds, content.script.duration_sec or 30.0))
        est = self._estimated_cost(est_dur)
        async with self.ctx.uow() as repos:
            spent = await repos.costs.workspace_total(content.workspace_id, self.ctx.day_start_kst())
        if spent + est > self.ctx.settings.workspace_daily_budget_krw + 1e-9:
            raise BudgetExceeded("workspace daily budget would be exceeded by this render", spent=spent,
                                 estimate=est, budget=self.ctx.settings.workspace_daily_budget_krw)

    async def _visuals(self, content: Content, product: Product, font: FontInfo,
                       opts: RenderOptions) -> dict[int, Asset]:
        today = to_kst(self.ctx.clock.now()).date()
        async with self.ctx.uow() as repos:
            overrides = await load_beat_overrides(repos, content.script.beats, opts.beat_asset_ids(), today)
            visuals = await resolve_visuals(repos, self.ctx.objects, beats=content.script.beats, product=product,
                                            today=today, now=self.ctx.clock.now(), seed=str(content.id),
                                            hook=content.script.hook, font=font,
                                            card_box=card_text_box_for(opts.caption_position), overrides=overrides)
            ledger = await repos.assets.get_many([a.id for a in visuals.values()])
        problems = ledger_violations(visuals, ledger, today)
        if problems:
            raise PolicyViolation("R-01: render inputs not in the rights ledger", problems=problems)
        return visuals

    async def _visual_refs(self, visuals: dict[int, Asset]) -> tuple[dict[int, VisualRef], dict[str, Path]]:
        refs: dict[int, VisualRef] = {}
        paths: dict[str, Path] = {}
        for i, a in visuals.items():
            local = await self.ctx.objects.local_path(a.uri)
            if not local.is_file():
                raise NotFound("ledger asset object missing from storage", asset_id=str(a.id), uri=a.uri)
            aspect: float | None = None
            if a.kind == "image":
                aspect = await asyncio.to_thread(_image_aspect, local)
            card = a.rights_source is AssetSource.GENERATED_CARD
            refs[i] = VisualRef(asset_id=str(a.id), uri=a.uri, kind="video" if a.kind == "video" else "image",
                                rights_source=a.rights_source.value, text_baked=card and "card:product" not in a.tags,
                                aspect=aspect, generated_card=card)
            paths[str(a.id)] = local
        return refs, paths

    async def _render_new(self, content: Content, product: Product, platforms: list[str], opts: RenderOptions,
                          voice_id: str, fingerprint: str) -> tuple[RenderMeta, dict[int, Asset], Asset]:
        font = self._font()
        visuals = await self._visuals(content, product, font, opts)
        refs, paths = await self._visual_refs(visuals)
        tts = await build_tts(self.ctx)
        beats = content.script.beats
        chunks = [b.narration.strip() for b in beats if b.narration.strip()]
        with tempfile.TemporaryDirectory(prefix="acvm_m6_", ignore_cleanup_errors=True) as td:
            work = Path(td)
            t_tts = time.perf_counter()
            narration = await tts.synthesize(chunks, work / "narration.wav", voice_id)
            STAGE_DURATION_SECONDS.labels(stage="tts").observe(time.perf_counter() - t_tts)
            timeline = build_timeline(
                content_id=str(content.id), template_id=content.template_id, beats=beats, words=narration.words,
                narration_duration=narration.duration_s, visuals=refs, spec=self.spec, gate=self.ctx.settings.gate,
                font_family=font.family, font_file=str(font.path), platforms=platforms,
                voice={"voice_id": voice_id, "provider": narration.provider},
                measure=lambda text, size: (libass_text_width(font, text, size), float(size)),
                style={**opts.resolved(voice_id=voice_id), "options_fingerprint": fingerprint},
                ai_label_text=self.ctx.settings.ai_visuals.label_text)
            bg = card_background_hex(f"product:{product.id}")
            arts = await self.renderer.render(timeline, narration_wav=narration.wav_path, asset_paths=paths,
                                              font=font, workdir=work / "render", background=bg)
            meta = await self._finish(content, timeline, arts, voice_id, narration.provider, visuals)
        voice = voice_asset(narration.provider, voice_id, now=self.ctx.clock.now())
        return meta, visuals, voice

    async def _verify(self, path: Path, overlay: dict[str, Any], font: FontInfo) -> dict[str, Any]:
        check = await verify_disclosure_overlay(path, overlay, ffmpeg=self.spec.ffmpeg, ffprobe=self.spec.ffprobe,
                                                min_seconds=self.ctx.settings.gate.disclosure_overlay_seconds,
                                                font_file=str(font.path))
        if not check.ok:
            raise DisclosureVerificationFailed("rendered video failed disclosure-band verification (D-03)",
                                               reasons=check.details.get("reasons", []), score=check.score)
        return {"ok": True, "score": round(check.score, 4), "method": check.details.get("method", ""),
                "frames": [f["t"] for f in check.details.get("frames", [])]}

    async def _finish(self, content: Content, timeline: dict[str, Any], arts: RenderArtifacts, voice_id: str,
                      provider: str, visuals: dict[int, Asset]) -> RenderMeta:
        spec = self.spec
        info = await probe(arts.master, ffprobe=spec.ffprobe)
        if (info.width, info.height) != (spec.width, spec.height) or info.video_codec != "h264":
            raise ACVMError("render output violates the 1080×1920 H.264 contract", width=info.width,
                            height=info.height, codec=info.video_codec)
        loudness = await measure_loudness(arts.master, ffmpeg=spec.ffmpeg)
        overlay = dict(timeline["overlays"]["disclosure"])
        font = self._font()
        overlay["verification"] = await self._verify(arts.master, overlay, font)
        sha = await asyncio.to_thread(sha256_file, arts.master)
        thumb_png = await extract_frame(arts.master, min(THUMBNAIL_AT_SEC, info.duration - 0.1), ffmpeg=spec.ffmpeg)
        thumb_jpg = await asyncio.to_thread(_to_jpeg, thumb_png)
        base = f"media/{content.id}/{sha}"
        media_uri = await self.ctx.objects.put_file(arts.master, f"{base}.mp4", "video/mp4")
        thumb_uri = await self.ctx.objects.put_bytes(thumb_jpg, f"{base}.jpg", "image/jpeg")
        await self.ctx.objects.put_bytes(timeline_bytes(timeline), f"{base}.timeline.json", "application/json")
        variants: dict[str, str] = {}
        for platform, path in arts.variants.items():
            await self._verify(path, dict(timeline["overlays"]["disclosure"]), font)
            vsha = await asyncio.to_thread(sha256_file, path)
            if vsha == sha:
                continue
            variants[platform] = await self.ctx.objects.put_file(path, f"media/{content.id}/{vsha}.{platform}.mp4",
                                                                 "video/mp4")
        asset_ids = list(dict.fromkeys(a.id for _, a in sorted(visuals.items())))
        return RenderMeta(
            media_uri=media_uri, thumbnail_uri=thumb_uri, duration_sec=round(info.duration, 3), width=info.width,
            height=info.height, fps=round(info.fps), loudness_lufs=round(loudness, 2), disclosure_overlay=overlay,
            asset_ids=asset_ids, voice_id=voice_id, voice_source=f"tts:{provider}:{voice_id}",
            template_id=content.template_id, sha256=sha, renderer=self.renderer.name, variants=variants)

    async def _persist(self, content: Content, meta: RenderMeta, visuals: dict[int, Asset], voice: Asset,
                       elapsed: float, fingerprint: str) -> RenderMeta:
        costs = self._estimated_cost(meta.duration_sec)
        async with self.ctx.uow() as repos:
            current = await repos.contents.get(content.id)
            if current is None:
                raise NotFound("content disappeared during render", content_id=str(content.id))
            if current.render is not None and current.render.sha256 == meta.sha256:
                return current.render
            voice_row = await repos.assets.register(voice)
            meta = meta.model_copy(update={"asset_ids": list(dict.fromkeys([*meta.asset_ids, voice_row.id]))})
            current.render = meta
            current.status = ContentStatus.RENDERED
            await repos.contents.save(current)
            for i, a in sorted(visuals.items()):
                await repos.contents.link_asset(content.id, a.id, f"beat:{i}")
            await repos.contents.link_asset(content.id, voice_row.id, "voice")
            await self.ctx.charge(repos, workspace_id=content.workspace_id, run_id=content.created_by_run,
                                  stage="render", amount_krw=round(costs, 4), key=f"{content.id}:render:{fingerprint}",
                                  content_id=content.id,
                                  detail={"duration_sec": meta.duration_sec, "sha256": meta.sha256,
                                          "renderer": meta.renderer, "voice_source": meta.voice_source,
                                          "options_fingerprint": fingerprint})
            await self.ctx.emit(repos, Event.of(
                RenderDone(content_id=content.id, media_uri=meta.media_uri, duration=meta.duration_sec),
                workspace_id=content.workspace_id, run_id=content.created_by_run,
                idempotency_key=f"{content.id}:render:{meta.sha256}"))
        log.info("render_done", content_id=str(content.id), sha256=meta.sha256, seconds=round(elapsed, 2),
                 duration=meta.duration_sec, lufs=meta.loudness_lufs, renderer=meta.renderer)
        return meta


def _image_aspect(path: Path) -> float | None:
    try:
        with Image.open(path) as im:
            w, h = im.size
    except OSError:
        return None
    return w / h if h else None


def _to_jpeg(png: bytes, quality: int = 88) -> bytes:
    with Image.open(io.BytesIO(png)) as im:
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()
