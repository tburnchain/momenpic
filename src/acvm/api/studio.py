"""/api/v1/studio — Shorts Studio (reference: the 5-step manual generator, automated end-to-end)."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from acvm.api.auth import ctx_of, require
from acvm.context import AppContext
from acvm.core.enums import AssetSource, ContentKind, HookType, Platform
from acvm.core.errors import ACVMError
from acvm.domain.models import Content
from acvm.modules.m4_script.templates import PERSONAS, TEMPLATES
from acvm.orchestration.studio import Studio
from acvm.policy.lint import is_covered
from acvm.security.audit import audit
from acvm.security.rbac import Perm, Principal

router = APIRouter(prefix="/api/v1/studio")
MAX_UPLOAD = 300 * 1024 * 1024
UPLOAD_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "image/png": "png",
                "image/jpeg": "jpg", "image/webp": "webp"}


def _ws(p: Principal) -> uuid.UUID:
    assert p.workspace_id is not None
    return uuid.UUID(p.workspace_id)


def studio_of(request: Request) -> Studio:
    st = getattr(request.app.state, "studio", None)
    if st is None:
        st = request.app.state.studio = Studio(ctx_of(request), request.app.state.gateway.pipeline)
    return st


async def _signed(ctx: AppContext, uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        return await ctx.objects.signed_url(uri, ctx.settings.objects.signed_url_ttl_seconds)
    except Exception:
        return None


# ───────────────────────── options ─────────────────────────


def _as_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "as_dict"):
        return dict(obj.as_dict())
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump(mode="json"))
    return dict(obj)


@router.get("/options")
async def options(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    voices: list[dict[str, Any]] = []
    styles: list[dict[str, Any]] = []
    positions: list[str] = ["bottom", "middle", "upper"]
    tts: dict[str, Any] = {"provider": "unknown", "real_voice": False, "note": ""}
    try:
        from acvm.media import voices as v

        voices = [{k: val for k, val in _as_dict(x).items() if k != "provider_ids"} for x in getattr(v, "VOICE_CATALOG", ())]
    except Exception:
        voices = []
    try:
        from acvm.media import caption_styles as cs

        styles = [{"id": s.id, "label": s.label, "description": getattr(s, "description", "")} for s in cs.CAPTION_STYLES]
        positions = list(cs.CAPTION_POSITIONS)
    except Exception:
        styles = [{"id": "classic", "label": "기본", "description": "흰 글자 + 검정 테두리"}]
    try:
        from acvm.modules.m6_render.tts import tts_status

        tts = await tts_status(ctx)
    except Exception as exc:
        tts = {"provider": "unavailable", "real_voice": False, "note": f"{type(exc).__name__}"}
    async with ctx.uow() as repos:
        accounts = await repos.accounts.list(_ws(p))
    from acvm import integrations

    active = await integrations.active_summary(ctx)
    return {
        "voices": voices, "caption_styles": styles, "caption_positions": positions, "tts": tts,
        "integrations": {
            **active,
            "llm": getattr(ctx.llm, "provider", "anthropic") if ctx.llm is not None else "offline",
            "ai_scenes": active["openai_image"],
            "ai_motion": active["higgsfield"],
            "stt": bool(await integrations.stt_engine(ctx)),
            "note": _integration_note(active),
        },
        "personas": [{"name": n, "voice_hint": per.voice_hint, "tone": per.tone} for n, per in PERSONAS.items()],
        "templates": [{"id": t.id, "kind": t.kind.value, "description": t.description, "duration": t.duration}
                      for t in TEMPLATES.values()],
        "hook_types": [h.value for h in HookType],
        "accounts": [{"id": str(a.id), "platform": a.platform.value, "handle": a.handle, "niche": a.niche,
                      "status": a.status.value} for a in accounts],
        "disclosure": {"locked": True, "text": ctx.settings.gate.video_disclosure,
                       "seconds": ctx.settings.gate.disclosure_overlay_seconds,
                       "note": "광고 표기는 법적 의무라 끌 수 없습니다 (표시광고법·추천보증 심사지침, 설계서 §8 D-03)"},
        "limits": {"sources": 5, "script_chars": 500, "watermark_chars": 24, "min_seconds": ctx.settings.render.min_seconds,
                   "max_seconds": ctx.settings.render.max_seconds,
                   "syllables_per_second": ctx.settings.render.target_speech_sps,
                   "min_speech_sps": ctx.settings.render.min_speech_sps,
                   "note": "쇼츠는 20~25초 상품 설명 — 빠르고 간결한 보이스만 사용합니다"},
    }


def _integration_note(active: dict[str, bool]) -> str:
    parts = []
    if active.get("openai_image"):
        parts.append("문제 장면(비상품 비트)에 GPT Image 배경 자동 생성" + (" + Higgsfield 모션" if active.get("higgsfield")
                                                                  else ""))
    if active.get("elevenlabs"):
        parts.append("ElevenLabs 실제 음성")
    if active.get("anthropic") or active.get("gemini"):
        parts.append("AI 대본 작성·검수(" + "·".join(n for n, k in (("Claude", "anthropic"), ("Gemini", "gemini"))
                                                   if active.get(k)) + ")")
    return " · ".join(parts) or "AI 키 미등록 — 운영 준비 › AI 연동에서 키를 등록하면 자동으로 켜집니다"


@router.get("/voices/{voice_id}/preview")
async def voice_preview(voice_id: str, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> Response:
    try:
        from acvm.media.voices import synthesize_preview
    except ImportError as exc:
        raise HTTPException(503, "voice preview not available yet") from exc
    try:
        data, mime = await synthesize_preview(ctx_of(request), voice_id)
    except ACVMError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/caption-styles/{style_id}/preview.png")
async def style_preview(style_id: str, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> Response:
    try:
        from acvm.media.caption_styles import render_style_preview
        from acvm.media.fonts import resolve_font
    except ImportError as exc:
        raise HTTPException(503, "caption style previews not available yet") from exc
    s = ctx_of(request).settings.render
    try:
        png = render_style_preview(style_id, font=resolve_font(s.font_file, s.font_family))
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, "unknown caption style") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


# ───────────────────────── products ─────────────────────────


@router.get("/products")
async def products(request: Request, category: str | None = None, q: str | None = None, refresh: bool = False,
                   limit: int = Query(30, le=100), p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    if refresh and not p.can(Perm.COMMAND):
        raise HTTPException(403, "refresh uses the Coupang API budget — operator role required")
    items = await studio_of(request).products(_ws(p), category_id=category, q=q, limit=limit, refresh=refresh)
    return {"items": items}


# ───────────────────────── drafts ─────────────────────────


class DraftIn(BaseModel):
    product_id: int
    kind: ContentKind = ContentKind.SHORT
    persona: str | None = None
    account_id: uuid.UUID | None = None
    platform: Platform | None = None
    template_id: str | None = None
    hook_type: HookType | None = None


def _with_duration(ctx: AppContext, lint: dict[str, Any], narrations: list[str]) -> dict[str, Any]:
    """Spoken-length check at the fast delivery rate: > max is an error (blocks generation), < min a warning."""
    from acvm.media.korean import estimate_syllables

    spec = ctx.settings.render
    syllables = sum(estimate_syllables(w) for t in narrations for w in t.split())
    est = round(syllables / spec.target_speech_sps + 1.2, 1)  # + CTA hold
    issues = list(lint.get("issues", []))
    if est > spec.max_seconds:
        issues.append({"sentence": "", "kind": "too_long", "severity": "error", "term": "", "suggestion": "",
                       "message": f"예상 {est:g}초 — {spec.max_seconds:g}초 이내로 줄이세요 (빠른 속도 {spec.target_speech_sps:g}음절/초 기준)"})
    elif est < spec.min_seconds:
        issues.append({"sentence": "", "kind": "too_short", "severity": "warning", "term": "", "suggestion": "",
                       "message": f"예상 {est:g}초 — {spec.min_seconds:g}초 미만은 CTA 화면으로 채워집니다"})
    ok = not any(i.get("severity") == "error" for i in issues)
    return {**lint, "issues": issues, "ok": ok,
            "duration": {"estimated_seconds": est, "syllables": syllables, "min_seconds": spec.min_seconds,
                         "max_seconds": spec.max_seconds, "syllables_per_second": spec.target_speech_sps}}


async def draft_view(request: Request, c: Content, lint: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = ctx_of(request)
    st = studio_of(request)
    async with ctx.uow() as repos:
        product = await repos.products.get(c.product_id)
        check = await repos.compliance.latest(c.id)
        pubs = await repos.publications.by_content(c.id)
        account = await repos.accounts.get(c.target_account_id) if c.target_account_id else None
        overrides = {str(k): str(v) for k, v in (c.render_options.get("beat_assets") or {}).items()}
        assets = await repos.assets.get_many([uuid.UUID(v) for v in overrides.values()]) if overrides else {}
    if lint is None:
        sc = c.script
        text = "\n".join([*(b.narration for b in sc.beats), *(b.on_screen_text for b in sc.beats), sc.body, sc.caption])
        lint = (await st.lint(c.product_id, text)).as_dict()
    # show each claim once: lint's auto-grounded claims that restate a registered claim (incl. on-screen
    # fragments like "평점은 4.5점") are already covered
    known = [cl.text for cl in c.claims]
    lint = {**lint, "claims": [x for x in lint.get("claims", []) if not is_covered(str(x.get("text", "")), known)]}
    if c.kind is ContentKind.SHORT:
        lint = _with_duration(ctx, lint, [b.narration for b in c.script.beats])
    beats = []
    for i, b in enumerate(c.script.beats):
        a = assets.get(uuid.UUID(overrides[str(i)])) if str(i) in overrides else None
        beats.append({"index": i, "role": b.role, "start": b.start_sec, "end": b.end_sec, "narration": b.narration,
                      "on_screen_text": b.on_screen_text, "visual": b.visual, "claim_ids": b.claim_ids,
                      "asset": {"id": str(a.id), "kind": a.kind, "url": await _signed(ctx, a.uri),
                                "rights_source": a.rights_source.value} if a else None})
    r = c.render
    return {
        "id": str(c.id), "status": c.status.value, "kind": c.kind.value, "persona": c.persona,
        "template_id": c.template_id, "hook_type": c.script.hook_type.value, "run_id": str(c.created_by_run) if c.created_by_run else None,
        "product": product.model_dump(mode="json") if product else None,
        "account": {"id": str(account.id), "platform": account.platform.value, "handle": account.handle} if account else None,
        "hooks": c.script.hooks, "chosen_hook": c.script.chosen_hook, "beats": beats, "caption": c.script.caption,
        "body": c.script.body, "hashtags": c.script.hashtags, "title": c.script.title, "disclosure": c.script.disclosure,
        "duration_target": c.script.duration_sec, "claims": [cl.model_dump(mode="json") for cl in c.claims],
        "render_options": c.render_options,
        "ai_visuals": await _ai_visuals_view(ctx, c),
        "render": {**(await st.render_state(c.id)),
                   **({"media_url": await _signed(ctx, r.media_uri), "thumbnail_url": await _signed(ctx, r.thumbnail_uri),
                       "duration": r.duration_sec, "voice_id": r.voice_id, "loudness_lufs": r.loudness_lufs,
                       "state": "done"} if r else {})},
        "lint": lint,
        "gate": {"verdict": check.verdict.value, "stage": check.stage, "decided_at": check.decided_at.isoformat(),
                 "findings": [f.model_dump(mode="json") for f in check.findings]} if check else None,
        "publications": [{"id": str(x.id), "platform": x.platform.value, "state": x.state.value,
                          "scheduled_at": x.scheduled_at.isoformat()} for x in pubs],
    }


async def _ai_visuals_view(ctx: AppContext, c: Content) -> dict[str, Any]:
    """What the AI scene step did for this draft (beats, providers, errors) — read-only, for the Studio."""
    from acvm import integrations

    async with ctx.uow() as repos:
        row = await repos.kv.get_stale(f"ai_visuals:{c.id}") or {}
    return {"enabled": await integrations.is_active(ctx, "openai_image"),
            "motion": await integrations.is_active(ctx, "higgsfield"),
            "beats": {k: {kk: v.get(kk) for kk in ("image", "video", "provider", "video_provider", "video_error")}
                      for k, v in (row.get("beats") or {}).items()},
            "errors": list(row.get("errors") or [])[:5], "at": row.get("at")}


@router.post("/drafts")
async def create_draft(body: DraftIn, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    c = await studio_of(request).create_draft(_ws(p), actor=p.subject, product_id=body.product_id, kind=body.kind,
                                              persona=body.persona, account_id=body.account_id, platform=body.platform,
                                              template_id=body.template_id, hook_type=body.hook_type)
    return await draft_view(request, c)


@router.get("/drafts/{content_id}")
async def get_draft(content_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    return await draft_view(request, await studio_of(request).get(content_id, _ws(p)))


class BeatEdit(BaseModel):
    narration: str | None = Field(default=None, max_length=400)
    on_screen_text: str | None = Field(default=None, max_length=120)


class DraftEdit(BaseModel):
    hooks: list[str] | None = Field(default=None, max_length=3)
    chosen_hook: int | None = Field(default=None, ge=0, le=2)
    beats: list[BeatEdit | None] | None = None
    caption: str | None = Field(default=None, max_length=2000)
    body: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=100)
    hashtags: list[str] | None = Field(default=None, max_length=10)
    render_options: dict[str, Any] | None = None


@router.put("/drafts/{content_id}")
async def update_draft(content_id: uuid.UUID, body: DraftEdit, request: Request,
                       p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    edits = body.model_dump(exclude_none=True)
    if "beats" in edits:
        edits["beats"] = [b if b is None else {k: v for k, v in b.items() if v is not None} for b in edits["beats"]]
    c, lint = await studio_of(request).update(content_id, _ws(p), edits, p.subject)
    return await draft_view(request, c, lint.as_dict())


class LintIn(BaseModel):
    product_id: int
    text: str = Field(max_length=6000)
    narrations: list[str] | None = Field(default=None, max_length=12)  # short: also check the 20–25 s window


@router.post("/lint")
async def lint(body: LintIn, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    result = (await studio_of(request).lint(body.product_id, body.text)).as_dict()
    if body.narrations is not None:
        result = _with_duration(ctx_of(request), result, body.narrations)
    return result


@router.post("/drafts/{content_id}/render", status_code=202)
async def render(content_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    st = studio_of(request)
    c = await st.get(content_id, _ws(p))
    if c.kind is not ContentKind.SHORT:
        raise HTTPException(422, "글 콘텐츠는 렌더가 필요 없습니다")
    st.assert_fits(c)  # 20–25 s at the fast delivery rate — refuse now, not as a failed background render
    state = await st.render_state(content_id)
    if state.get("state") != "rendering":
        st.render_in_background(content_id, _ws(p))
    return {"state": "rendering"}


class TargetIn(BaseModel):
    account_id: uuid.UUID


class PublishIn(BaseModel):
    targets: list[TargetIn] | None = None


@router.post("/drafts/{content_id}/publish")
async def publish(content_id: uuid.UUID, body: PublishIn, request: Request,
                  p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    targets = [t.model_dump(mode="json") for t in body.targets] if body.targets else None
    return await studio_of(request).publish(content_id, _ws(p), actor=p.subject, targets=targets)


class AutoIn(BaseModel):
    product_id: int
    kind: ContentKind = ContentKind.SHORT
    persona: str | None = None
    targets: list[TargetIn] | None = None


@router.post("/auto")
async def auto(body: AutoIn, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    targets = [t.model_dump(mode="json") for t in body.targets] if body.targets else None
    return await studio_of(request).auto(_ws(p), actor=p.subject, product_id=body.product_id, kind=body.kind,
                                         persona=body.persona, targets=targets)


# ───────────────────────── asset library (① 영상소스, 툴) ─────────────────────────


def _asset_dict(a: Any, url: str | None) -> dict[str, Any]:
    return {"id": str(a.id), "kind": a.kind, "rights_source": a.rights_source.value, "license": a.license,
            "product_id": a.product_id, "tags": a.tags, "attribution": a.attribution, "url": url,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None}


@router.get("/assets")
async def list_assets(request: Request, product_id: int | None = None, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    from sqlalchemy import select

    from acvm.db import tables as t

    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        stmt = select(t.AssetRow).where(t.AssetRow.kind.in_(("image", "video")))
        rows = (await repos.session.execute(stmt.order_by(t.AssetRow.created_at.desc()).limit(500))).scalars().all()
    from acvm.domain.models import Asset

    items = []
    for r in rows:
        a = Asset.model_validate(r)
        if a.rights_source is AssetSource.GENERATED_CARD:
            continue
        if product_id is not None and a.product_id not in (None, product_id):
            continue
        items.append(_asset_dict(a, await _signed(ctx, a.uri)))
    return {"items": items[:200]}


@router.post("/assets")
async def upload_asset(request: Request, rights: Literal["own_footage"] = Query(...),
                       attest: bool = Query(..., description="권리 보유 확인 (직접 촬영·구매 샘플 촬영본)"),
                       product_id: int | None = None, p: Principal = Depends(require(Perm.MANAGE_ASSETS))) -> dict[str, Any]:
    """Raw-body upload (Content-Type = file MIME, X-Filename = URI-encoded name). Only the operator's own
    footage is accepted (§8.2: no downloading or re-uploading other people's media)."""
    if not attest:
        raise HTTPException(422, "권리 보유 확인이 필요합니다 — 타인 영상·이미지는 업로드할 수 없습니다 (설계서 §8.2)")
    mime = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if mime not in UPLOAD_TYPES:
        raise HTTPException(415, f"지원하지 않는 형식: {mime or '알 수 없음'} (mp4·mov·webm·png·jpg·webp)")
    filename = unquote(request.headers.get("X-Filename", f"upload.{UPLOAD_TYPES[mime]}"))[:200]
    ctx = ctx_of(request)
    from acvm.modules.m5_asset.service import AssetService

    with tempfile.TemporaryDirectory(prefix="acvm_upload_") as td:
        path = Path(td) / f"src.{UPLOAD_TYPES[mime]}"
        size = 0
        with path.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "파일이 너무 큽니다 (최대 300MB)")
                fh.write(chunk)
        if size == 0:
            raise HTTPException(422, "빈 파일입니다")
        try:
            asset = await AssetService(ctx).ingest_upload(_ws(p), path, filename, mime, rights=rights, product_id=product_id,
                                                          tags=("studio",), attested_by=p.subject)
        except ACVMError as exc:
            raise HTTPException(422, str(exc)) from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "asset.upload", str(asset.id), mime=mime, size=size, rights=rights)
    return _asset_dict(asset, await _signed(ctx, asset.uri))


class CutIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class I2VIn(BaseModel):
    motion: Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"] = "zoom_in"
    seconds: float = Field(default=4.0, ge=1.0, le=15.0)
    # camera: deterministic virtual camera (any licensed image, incl. product photos)
    # ai: Higgsfield motion — AI-generated scene images only (a product photo must never be re-synthesised)
    engine: Literal["camera", "ai"] = "camera"


async def _asset_or_404(ctx: AppContext, asset_id: uuid.UUID) -> Any:
    async with ctx.uow() as repos:
        a = await repos.assets.get(asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    return a


@router.post("/assets/{asset_id}/cut")
async def cut(asset_id: uuid.UUID, body: CutIn, request: Request, p: Principal = Depends(require(Perm.MANAGE_ASSETS))) -> dict[str, Any]:
    if body.end <= body.start:
        raise HTTPException(422, "끝 시간은 시작 시간보다 커야 합니다")
    ctx = ctx_of(request)
    a = await _asset_or_404(ctx, asset_id)
    if a.kind != "video":
        raise HTTPException(422, "영상만 자를 수 있습니다")
    from acvm.media.edit import cut_video
    from acvm.modules.m5_asset.service import AssetService

    src = await ctx.objects.local_path(a.uri)
    with tempfile.TemporaryDirectory(prefix="acvm_cut_") as td:
        out = Path(td) / "cut.mp4"
        await cut_video(src, body.start, body.end, out, ffmpeg=ctx.settings.render.ffmpeg)
        derived = await AssetService(ctx).derive_asset(a, out, "video", op="cut")
    return _asset_dict(derived, await _signed(ctx, derived.uri))


@router.post("/assets/{asset_id}/i2v")
async def image_to_video(asset_id: uuid.UUID, body: I2VIn, request: Request,
                         p: Principal = Depends(require(Perm.MANAGE_ASSETS))) -> dict[str, Any]:
    ctx = ctx_of(request)
    a = await _asset_or_404(ctx, asset_id)
    if a.kind != "image":
        raise HTTPException(422, "이미지만 영상으로 변환할 수 있습니다")
    from acvm.media.edit import image_to_video as i2v
    from acvm.modules.m5_asset.service import AssetService

    src = await ctx.objects.local_path(a.uri)
    if body.engine == "ai":
        return await _ai_motion(ctx, a, src, body, p)
    with tempfile.TemporaryDirectory(prefix="acvm_i2v_") as td:
        out = Path(td) / "motion.mp4"
        await i2v(src, out, seconds=body.seconds, motion=body.motion, ffmpeg=ctx.settings.render.ffmpeg)
        derived = await AssetService(ctx).derive_asset(a, out, "video", op=f"i2v_{body.motion}")
    return _asset_dict(derived, await _signed(ctx, derived.uri))


_MOTION_PROMPTS = {
    "zoom_in": "slow, smooth camera push-in", "zoom_out": "slow, smooth camera pull-back",
    "pan_left": "slow camera pan to the left", "pan_right": "slow camera pan to the right",
    "pan_up": "slow camera tilt up",
}


async def _ai_motion(ctx: AppContext, a: Any, src: Path, body: I2VIn, p: Principal) -> dict[str, Any]:
    """Higgsfield motion for an AI scene still. Product photos and own footage stay on the camera engine."""
    from acvm import integrations
    from acvm.core.errors import BudgetExceeded
    from acvm.modules.m5_asset.ai_visuals import CLIP_NEGATIVE
    from acvm.modules.m5_asset.service import AssetService

    if a.rights_source is not AssetSource.AI_GENERATED_BACKGROUND:
        raise HTTPException(403, "AI 모션은 AI 생성 장면 이미지에만 쓸 수 있습니다 — 상품 사진·직접 촬영본은 카메라 모션을 사용하세요")
    gen = await integrations.video_gen(ctx)
    if gen is None:
        raise HTTPException(409, "Higgsfield 키가 등록·확인되지 않았습니다 (운영 준비 › AI 연동)")
    cost = ctx.settings.higgsfield.cost_per_clip_krw
    prompt = (f"Subtle natural ambient motion with a {_MOTION_PROMPTS[body.motion]}. Keep the scene exactly as it is: "
              "no new objects, people, faces, text or logos, no scene cut.")
    data = await asyncio.to_thread(src.read_bytes)
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    async with ctx.uow() as repos:  # budget first: refuse before spending credits
        spent = await repos.costs.workspace_total(_ws(p), ctx.day_start_kst())
        if spent + cost > await ctx.daily_budget(repos, _ws(p)):
            raise HTTPException(409, "오늘 예산 한도에 도달했습니다 (설정 › 일 예산)")
    try:
        clip = await gen.image_to_video(data, mime, prompt, seconds=max(5, min(10, round(body.seconds))),
                                        negative_prompt=CLIP_NEGATIVE)
    except ACVMError as exc:
        raise HTTPException(502, f"AI 모션 생성 실패: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="acvm_aimotion_") as td:
        out = Path(td) / "motion.mp4"
        await asyncio.to_thread(out.write_bytes, clip.data)
        derived = await AssetService(ctx).derive_asset(a, out, "video", op=f"{clip.provider}_i2v")
    try:
        async with ctx.uow() as repos:
            await ctx.charge(repos, workspace_id=_ws(p), run_id=None, stage="ai.video", amount_krw=float(
                clip.usage.get("krw", cost)), key=f"studio:ai.video:{derived.id}",
                detail={"provider": clip.provider, "model": clip.model, "asset_id": str(derived.id)})
            await audit(repos, ctx.clock, p.subject, "asset.ai_motion", str(derived.id), parent=str(a.id),
                        provider=clip.provider)
    except BudgetExceeded:
        pass  # pre-checked above; a concurrent spend raced us — the clip exists, keep it
    return _asset_dict(derived, await _signed(ctx, derived.uri))


@router.post("/assets/{asset_id}/transcribe")
async def transcribe(asset_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.MANAGE_ASSETS))) -> dict[str, Any]:
    """대본 추출 — only for the operator's own footage (rights ledger: own_footage). Extracting scripts from other
    people's videos would be the copy-and-reproduce pattern the design forbids (§0 #1, §8.2)."""
    ctx = ctx_of(request)
    a = await _asset_or_404(ctx, asset_id)
    if a.rights_source is not AssetSource.OWN_FOOTAGE or a.kind != "video":
        raise HTTPException(403, "대본 추출은 권리 원장에 '직접 촬영'으로 등록된 영상에만 사용할 수 있습니다")
    try:
        from acvm.media.stt import STTUnavailable
        from acvm.media.stt import transcribe as stt
    except ImportError as exc:
        raise HTTPException(503, "STT 모듈이 아직 설치되지 않았습니다") from exc
    from acvm import integrations

    engine = await integrations.stt_engine(ctx)  # local faster-whisper, else Gemini when its key is active
    if engine is None:
        raise HTTPException(501, "음성 인식 엔진이 없습니다 — 운영 준비 › AI 연동에서 Gemini 키를 등록하면 바로 쓸 수 있습니다 "
                                 "(또는 서버에 faster-whisper 설치)")
    try:
        tr = await stt(await ctx.objects.local_path(a.uri), engine=engine)
    except STTUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    except ACVMError as exc:
        raise HTTPException(502, f"대본 추출 실패: {exc}") from exc
    return {"asset_id": str(a.id), "text": tr.text, "language": getattr(tr, "language", "ko"),
            "engine": getattr(tr, "engine", ""), "words": [list(w) if not isinstance(w, dict) else w for w in tr.words][:2000]}


# ───────────────────────── TTS engine (④ 보이스) ─────────────────────────

TTSChoice = Literal["auto", "elevenlabs", "sapi", "yap-mock", "acvm-tone"]


class TTSIn(BaseModel):
    provider: TTSChoice


@router.put("/tts")
async def set_tts(body: TTSIn, request: Request, p: Principal = Depends(require(Perm.MANAGE_ASSETS))) -> dict[str, Any]:
    """Pick the narration engine. ``auto`` = best available (ElevenLabs key → Windows SAPI → fallbacks)."""
    from datetime import timedelta

    from acvm.modules.m6_render.tts import tts_status

    ctx = ctx_of(request)
    if body.provider == "auto":
        ctx.extras.pop("tts.provider", None)
    else:
        ctx.extras["tts.provider"] = body.provider
    async with ctx.uow() as repos:
        await repos.kv.put("studio:tts_provider", {"provider": body.provider}, timedelta(days=3650), ctx.clock.now())
        await audit(repos, ctx.clock, p.subject, "studio.tts_provider", body.provider)
    return {"selected": body.provider, **(await tts_status(ctx))}
