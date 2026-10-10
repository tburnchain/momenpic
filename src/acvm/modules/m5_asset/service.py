"""M5 Asset Engine — visual sourcing and the rights ledger (§4 M5, §6.1 ``asset``, §8.1 R-01/R-03, §12 저작권 분쟁).

Responsibilities:

* **Ledger policy.** Every asset that can ever reach the renderer is registered here with its source,
  licence and expiry. ``register`` de-duplicates on SHA-256 (``AssetRow`` is unique on sha256+source) and
  refuses rows that would violate policy (missing licence, already expired, AI imagery tied to a product,
  Coupang images without a product, voices without a ``tts:`` URI).
* **Product imagery.** ``ensure_product_image`` downloads the Coupang product image (allow-listed hosts,
  size/time limits) or, in sandbox mode, creates a deterministic text placeholder card. The product is never
  generated or altered with AI (§4 M5).
* **Selection.** ``select_for`` returns one ledger visual per script beat by source priority via the shared
  :func:`acvm.media.selection.resolve_visuals` — the same function M6 uses, so both modules agree.
* **Voices.** ``voice_for`` maps a persona to a logical voice and records the synthetic voice and its
  provider licence in the ledger (R-03 "보이스 출처 확인").

The module reacts to ``script.ready`` (§6.3) through :meth:`AssetService.handle_script_ready`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from acvm.core.clock import to_kst
from acvm.core.enums import AssetSource, ContentKind
from acvm.core.errors import NotFound, PolicyViolation, ValidationFailed
from acvm.core.events import Event, ScriptReady
from acvm.core.logging import get_logger
from acvm.domain.models import Asset, Content, Product
from acvm.media.caption_styles import card_text_box_for
from acvm.media.cards import product_card
from acvm.media.ffmpeg import MediaError, probe, sha256_file
from acvm.media.fonts import FontInfo, resolve_font
from acvm.media.render_options import parse_render_options
from acvm.media.selection import GENERATED_LICENSE, load_beat_overrides, materialize_card, resolve_visuals, usable
from acvm.media.voices import get_voice, resolve_tts_provider, voice_asset, voice_id_for_persona
from acvm.modules.m5_asset.download import fetch_product_image

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

COUPANG_IMAGE_LICENSE = "coupang-partners-product-image"
UPLOAD_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "image/png": "png",
                "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp"}
UPLOAD_KIND = {"mp4": "video", "mov": "video", "webm": "video", "png": "image", "jpg": "image", "webp": "image"}
_UPLOAD_MIME = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "png": "image/png",
                "jpg": "image/jpeg", "webp": "image/webp"}
_PIL_FORMAT = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
MAX_UPLOAD_SECONDS = 180.0
_OP = re.compile(r"[a-z0-9_]{1,40}")
_EXT_BY_KIND = {"image": ".jpg", "video": ".mp4", "audio": ".wav", "font": ".ttf"}
_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                  ".mp4": "video/mp4", ".mov": "video/quicktime", ".wav": "audio/wav", ".ttf": "font/ttf",
                  ".otf": "font/otf"}


class AssetService:
    """§4 M5 Asset Engine bound to one :class:`~acvm.context.AppContext`."""

    def __init__(self, ctx: AppContext, *, http: httpx.AsyncClient | None = None) -> None:
        self.ctx = ctx
        self._http = http

    # ── helpers ──

    def _today(self) -> date:
        return to_kst(self.ctx.clock.now()).date()

    def _font(self) -> FontInfo:
        spec = self.ctx.settings.render
        return resolve_font(spec.font_file, spec.font_family)

    # ── ledger ──

    def check_policy(self, asset: Asset) -> None:
        """Raise when ``asset`` must not enter the rights ledger."""
        if not asset.license.strip():
            raise ValidationFailed("asset licence is required (rights ledger)", sha256=asset.sha256)
        if len(asset.sha256) != 64:
            raise ValidationFailed("asset sha256 must be a 64-char hex digest")
        if asset.expires_at is not None and asset.expires_at < self._today():
            raise ValidationFailed("asset licence already expired", expires_at=asset.expires_at.isoformat())
        src = asset.rights_source
        if src is AssetSource.COUPANG_PRODUCT_IMAGE and asset.product_id is None:
            raise ValidationFailed("Coupang product images must reference their product")
        if src is AssetSource.AI_GENERATED_BACKGROUND and (
                asset.product_id is not None or any(t.startswith("product:") for t in asset.tags)):
            raise PolicyViolation("AI-generated imagery may not depict or stand in for a product (§4 M5)")
        if asset.kind == "voice" and not asset.uri.startswith("tts:"):
            raise ValidationFailed("voice assets must use a tts:<provider>:<voice> URI")

    async def register(self, asset: Asset) -> Asset:
        """Policy-check and add ``asset`` to the ledger; returns the existing row on a SHA-256 duplicate."""
        self.check_policy(asset)
        if asset.created_at is None:
            asset = asset.model_copy(update={"created_at": self.ctx.clock.now()})
        async with self.ctx.uow() as repos:
            stored = await repos.assets.register(asset)
        if stored.id != asset.id:
            log.info("asset_dedup", sha256=asset.sha256, existing=str(stored.id))
        return stored

    async def ingest_bytes(self, data: bytes, *, kind: str, rights_source: AssetSource, license: str,
                           ext: str | None = None, product_id: int | None = None, tags: list[str] | None = None,
                           attribution: str = "", expires_at: date | None = None) -> Asset:
        """Store raw bytes content-addressed (``assets/{sha}{ext}``) and register them (own footage, stock…)."""
        sha = hashlib.sha256(data).hexdigest()
        suffix = (ext or _EXT_BY_KIND.get(kind, ".bin")).lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        probe = Asset(kind=kind, uri="pending:", sha256=sha, rights_source=rights_source,
                      license=license, expires_at=expires_at, product_id=product_id, attribution=attribution,
                      tags=list(tags or []))
        self.check_policy(probe)
        uri = await self.ctx.objects.put_bytes(data, f"assets/{sha}{suffix}",
                                               _CONTENT_TYPES.get(suffix, "application/octet-stream"))
        return await self.register(probe.model_copy(update={"uri": uri}))

    async def ingest_file(self, path: Path, *, kind: str, rights_source: AssetSource, license: str,
                          product_id: int | None = None, tags: list[str] | None = None, attribution: str = "",
                          expires_at: date | None = None) -> Asset:
        """File variant of :meth:`ingest_bytes` (B-roll library, stock downloads)."""
        data = await asyncio.to_thread(Path(path).read_bytes)
        return await self.ingest_bytes(data, kind=kind, rights_source=rights_source, license=license,
                                       ext=Path(path).suffix or None, product_id=product_id, tags=tags,
                                       attribution=attribution, expires_at=expires_at)

    # ── product imagery ──

    async def ensure_product_image(self, product: Product) -> Asset:
        """Ledger visual of the product itself: existing own footage / Coupang image, else download (prod) or
        a deterministic placeholder card (sandbox)."""
        today = self._today()
        async with self.ctx.uow() as repos:
            existing = [a for a in await repos.assets.for_product(product.id) if usable(a, today) and a.kind == "image"]
        for source in (AssetSource.OWN_FOOTAGE, AssetSource.COUPANG_PRODUCT_IMAGE):
            hit = sorted((a for a in existing if a.rights_source is source), key=lambda a: a.sha256)
            if hit:
                return hit[0]
        if self.ctx.settings.coupang.sandbox:
            placeholder = [a for a in existing if a.rights_source is AssetSource.GENERATED_CARD
                           and "card:product" in a.tags]
            if placeholder:
                return sorted(placeholder, key=lambda a: a.sha256)[0]
            card = product_card(product.name, product.price, seed=f"product:{product.id}")
            async with self.ctx.uow() as repos:
                asset = await materialize_card(card, repos=repos, objects=self.ctx.objects, now=self.ctx.clock.now(),
                                               product_id=product.id, font=self._font())
            log.info("product_placeholder_card", product_id=product.id, asset_id=str(asset.id))
            return asset
        if not product.image_url:
            raise NotFound("product has no image_url", product_id=product.id)
        data, fmt = await fetch_product_image(product.image_url, client=self._http)
        sha = hashlib.sha256(data).hexdigest()
        uri = await self.ctx.objects.put_bytes(data, f"assets/{sha}.jpg", "image/jpeg")
        asset = Asset(kind="image", uri=uri, sha256=sha, rights_source=AssetSource.COUPANG_PRODUCT_IMAGE,
                      license=COUPANG_IMAGE_LICENSE, product_id=product.id,
                      attribution=f"Coupang product image (productId {product.coupang_product_id}, {fmt})",
                      tags=[f"product:{product.id}", f"category:{product.category_id}", "product_image"],
                      created_at=self.ctx.clock.now())
        stored = await self.register(asset)
        log.info("product_image_registered", product_id=product.id, asset_id=str(stored.id), bytes=len(data))
        return stored

    # ── selection ──

    async def select_for(self, content: Content, product: Product) -> dict[int, Asset]:
        """One ledger visual per beat (beat index → Asset) by §4 M5 priority; text beats get generated cards."""
        if content.kind is not ContentKind.SHORT:
            raise ValidationFailed("visual selection applies to short-form content only", content_id=str(content.id))
        if not content.script.beats:
            raise ValidationFailed("script has no beats", content_id=str(content.id))
        opts = parse_render_options(content.render_options, n_beats=len(content.script.beats))
        today = self._today()
        async with self.ctx.uow() as repos:
            overrides = await load_beat_overrides(repos, content.script.beats, opts.beat_asset_ids(), today)
            return await resolve_visuals(repos, self.ctx.objects, beats=content.script.beats, product=product,
                                         today=today, now=self.ctx.clock.now(), seed=str(content.id),
                                         hook=content.script.hook, font=self._font(),
                                         card_box=card_text_box_for(opts.caption_position), overrides=overrides)

    async def enrich_ai_visuals(self, content: Content, product: Product) -> Content:
        """GPT Image / Higgsfield scene media for scene beats when those integrations are active (no-op otherwise).
        Never raises for provider problems — the beat keeps its card (see :mod:`.ai_visuals`)."""
        from acvm.modules.m5_asset.ai_visuals import AIVisualService

        try:
            return await AIVisualService(self.ctx, self).enrich(content, product)
        except Exception as exc:
            log.warning("ai_visuals_skipped", content_id=str(content.id), error=f"{type(exc).__name__}: {str(exc)[:160]}")
            return content

    # ── voices ──

    def voice_id_for(self, persona: str) -> str:
        return voice_id_for_persona(persona)

    async def voice_for(self, persona: str, *, provider: str | None = None, voice_id: str | None = None) -> Asset:
        """Ledger entry of the synthetic voice used for ``persona`` (R-03 voice provenance)."""
        prov = provider or await resolve_tts_provider(self.ctx)
        vid = get_voice(voice_id).id if voice_id else voice_id_for_persona(persona)
        asset = voice_asset(prov, vid, now=self.ctx.clock.now())
        return await self.register(asset)

    # ── Studio uploads & derived media ──

    async def ingest_upload(self, workspace_id: uuid.UUID, data: bytes | Path, filename: str, mime: str, *,
                            rights: Literal["own_footage"] = "own_footage", product_id: int | None = None,
                            tags: Sequence[str] = (), attested_by: str) -> Asset:
        """Register an operator upload (Studio ① 영상소스) as **own footage** in the rights ledger.

        Only the operator's own footage is accepted (§8.2: no re-uploading other people's media) and the uploader
        must attest it. Validates type (mp4/mov/webm/png/jpg/webp, by MIME, extension and content), size
        (≤ 300 MB) and video length (≤ 180 s); de-duplicates by SHA-256; stores at ``assets/own/{sha}.{ext}``.
        """
        if rights != "own_footage":
            raise PolicyViolation("studio uploads are limited to the operator's own footage (§8.2)", rights=rights)
        who = (attested_by or "").strip()
        if not who:
            raise ValidationFailed("rights attestation (attested_by) is required for uploads")
        ext = UPLOAD_TYPES.get(mime.split(";")[0].strip().lower())
        if ext is None:
            raise ValidationFailed("unsupported upload type (mp4, mov, webm, png, jpg, webp)", mime=mime)
        name_ext = Path(filename or "").suffix.lower().lstrip(".").replace("jpeg", "jpg")
        if name_ext and name_ext in UPLOAD_KIND and UPLOAD_KIND[name_ext] != UPLOAD_KIND[ext]:
            raise ValidationFailed("file extension does not match the declared type", filename=filename, mime=mime)
        kind = UPLOAD_KIND[ext]
        if product_id is not None:
            async with self.ctx.uow() as repos:
                if await repos.products.get(product_id) is None:
                    raise NotFound("product not found", product_id=product_id)
        with tempfile.TemporaryDirectory(prefix="acvm_upload_", ignore_cleanup_errors=True) as td:
            if isinstance(data, (bytes, bytearray)):
                path = Path(td) / f"upload.{ext}"
                await asyncio.to_thread(path.write_bytes, bytes(data))
            else:
                path = Path(data)
            size = (await asyncio.to_thread(path.stat)).st_size
            if size == 0:
                raise ValidationFailed("empty upload")
            if size > MAX_UPLOAD_BYTES:
                raise ValidationFailed("upload exceeds 300 MB", bytes=size)
            if kind == "video":
                try:
                    info = await probe(path, ffprobe=self.ctx.settings.render.ffprobe)
                except MediaError as exc:
                    raise ValidationFailed("upload is not a readable video") from exc
                if not info.has_video:
                    raise ValidationFailed("upload has no video stream")
                if info.duration > MAX_UPLOAD_SECONDS:
                    raise ValidationFailed("video is longer than 180 s", seconds=round(info.duration, 1))
            else:
                await asyncio.to_thread(_verify_image, path, ext)
            sha = await asyncio.to_thread(sha256_file, path)
            uri = await self.ctx.objects.put_file(path, f"assets/own/{sha}.{ext}", _UPLOAD_MIME[ext])
        all_tags = ["studio", "upload", f"workspace:{workspace_id}", *tags]
        if product_id is not None:
            all_tags.append(f"product:{product_id}")
        asset = Asset(kind="video" if kind == "video" else "image", uri=uri, sha256=sha,
                      rights_source=AssetSource.OWN_FOOTAGE, license=f"own footage, rights attested by {who}",
                      product_id=product_id, attribution=f"uploaded by {who}", tags=list(dict.fromkeys(all_tags)),
                      created_at=self.ctx.clock.now())
        stored = await self.register(asset)
        log.info("studio_upload_registered", asset_id=str(stored.id), kind=kind, bytes=size,
                 dedup=stored.id != asset.id)
        return stored

    async def derive_asset(self, parent: Asset, path: Path, kind: Literal["image", "video"], *, op: str) -> Asset:
        """Register a Studio edit (컷편집 / 이미지→영상) of ``parent``: same rights source, licence, expiry and product,
        tagged ``derived:{op}:{parent.id}``; stored at ``assets/derived/{sha}{ext}``."""
        if not _OP.fullmatch(op):
            raise ValidationFailed("derive op must be a short slug", op=op)
        if kind not in ("image", "video"):
            raise ValidationFailed("derived assets must be images or videos", kind=kind)
        async with self.ctx.uow() as repos:
            row = await repos.assets.get(parent.id)
        if row is None:
            raise PolicyViolation("R-01: parent asset is not in the rights ledger", asset_id=str(parent.id))
        if not usable(row, self._today()):
            raise PolicyViolation("R-01: parent asset is unlicensed, expired or not a visual", asset_id=str(row.id))
        src = Path(path)
        if kind == "video":
            info = await probe(src, ffprobe=self.ctx.settings.render.ffprobe)
            if not info.has_video:
                raise ValidationFailed("derived video has no video stream")
        else:
            await asyncio.to_thread(_verify_image, src, None)
        sha = await asyncio.to_thread(sha256_file, src)
        suffix = src.suffix.lower() or (".mp4" if kind == "video" else ".png")
        uri = await self.ctx.objects.put_file(src, f"assets/derived/{sha}{suffix}",
                                              _CONTENT_TYPES.get(suffix, "application/octet-stream"))
        tags = [t for t in row.tags if not t.startswith("derived:")] + [f"derived:{op}:{row.id}"]
        asset = Asset(kind=kind, uri=uri, sha256=sha, rights_source=row.rights_source, license=row.license,
                      expires_at=row.expires_at, product_id=row.product_id,
                      attribution=f"{row.attribution or row.uri} -> {op}"[:500], tags=list(dict.fromkeys(tags)),
                      created_at=self.ctx.clock.now())
        return await self.register(asset)

    # ── event entry point ──

    async def handle_script_ready(self, event: Event) -> dict[int, Asset]:
        """``script.ready`` consumer: make sure product imagery, beat visuals and the voice are in the ledger."""
        payload = event.parsed()
        if not isinstance(payload, ScriptReady):
            raise ValidationFailed("unexpected event type for M5", type=event.type)
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(payload.content_id)
            if content is None:
                raise NotFound("content not found", content_id=str(payload.content_id))
            product = await repos.products.get(content.product_id)
            if product is None:
                raise NotFound("product not found", product_id=content.product_id)
        if content.kind is not ContentKind.SHORT:
            return {}
        await self.ensure_product_image(product)
        visuals = await self.select_for(content, product)
        await self.voice_for(content.persona)
        return visuals


__all__ = ["COUPANG_IMAGE_LICENSE", "GENERATED_LICENSE", "AssetService"]


def _verify_image(path: Path, ext: str | None) -> None:
    """Content check for image uploads/derivatives: decodable, not a decompression bomb, matching format."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
            if w * h > 60_000_000:
                raise ValidationFailed("image too large", width=w, height=h)
            im.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationFailed("file is not a valid image") from exc
    if ext is not None and _PIL_FORMAT.get(ext) not in (None, fmt):
        raise ValidationFailed("image content does not match its declared type", declared=ext, actual=fmt)
