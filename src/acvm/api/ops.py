"""Operations API — what an operator needs to run the business day to day:

    /system/status       readiness checklist (integrations, secrets presence, toolchain, G0 gate)
    /system/checklist    §8.3 legal/policy confirmations (owner)
    /system/secrets/{n}  write-only credential entry → SecretStore (Vault in prod); values never returned;
                         DELETE removes a console-entered value. AI keys are verified and activated on save.
    /system/integrations AI providers (Claude · Gemini · GPT Image · Higgsfield · ElevenLabs): state, what each
                         automates, live re-check, pause/resume, ElevenLabs voice re-mapping
    /workspace           budget settings and today's spend
    /accounts/{id}/credentials   write-only platform token entry
    /publications        board/calendar listing · /publications/{id}/takedown (saga compensation)
    /contents/{id}       full content dossier (script, claims, every gate check, publications)
    /alerts              risk feed (blocks, holds, cooldowns, invalid clicks, budget)
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from acvm import integrations as ai
from acvm.api.auth import ctx_of, require
from acvm.context import AppContext
from acvm.core.enums import AccountStatus, Platform, PublicationState
from acvm.db import tables as t
from acvm.infra.secrets import DevFileSecretStore
from acvm.security.audit import audit
from acvm.security.rbac import Perm, Principal

router = APIRouter(prefix="/api/v1")

LEGAL_CHECKLIST: dict[str, str] = {
    "partners_policy": "쿠팡 파트너스 운영정책 확인 (자동화 범위·링크 허브·subId)",
    "ad_law": "표시광고법·추천보증 심사지침(2024-12-01 개정) 문구·위치 확인",
    "ai_act": "인공지능기본법 생성형 AI 표시 의무 적용 여부 법률 검토",
    "platform_terms": "각 플랫폼 약관(브랜디드 콘텐츠·제휴 링크·자동 게시) 확인",
    "tax_entity": "수수료 수령 주체(개인 vs 법인)와 사업소득 신고 체계 확정",
}

SECRET_SLOTS: dict[str, tuple[str, str]] = {
    # name → (settings-path resolver key, label)
    "coupang_access_key": ("coupang.access_key_ref", "쿠팡 파트너스 Access Key"),
    "coupang_secret_key": ("coupang.secret_key_ref", "쿠팡 파트너스 Secret Key"),
    "link_signing_key": ("security.link_signing_key_ref", "제휴 링크 서명 키 (HMAC)"),
    "click_ip_salt": ("security.click_ip_salt_ref", "클릭 IP 해시 솔트"),
    "voyage_api_key": ("embeddings.api_key_ref", "Voyage 임베딩 키"),
    "r2_access_key": ("objects.access_key_ref", "Cloudflare R2 Access Key"),
    "r2_secret_key": ("objects.secret_key_ref", "Cloudflare R2 Secret Key"),
    **ai.INTEGRATION_SLOTS,  # Claude · Gemini · OpenAI (GPT Image) · Higgsfield (id + secret) · ElevenLabs
}


def _ws(p: Principal) -> uuid.UUID:
    assert p.workspace_id is not None
    return uuid.UUID(p.workspace_id)


def _ref(ctx: AppContext, spec: str) -> str:
    if "." not in spec:
        return spec
    section, field = spec.split(".", 1)
    return str(getattr(getattr(ctx.settings, section), field))


async def _has_secret(ctx: AppContext, ref: str) -> bool:
    try:
        return bool(await ctx.secrets.get(ref))
    except Exception:
        return False


def _persistent(ctx: AppContext) -> bool:
    return ctx.settings.security.secret_backend == "vault" or isinstance(ctx.secrets, DevFileSecretStore)


def _check(cid: str, label: str, status: Literal["ok", "warn", "fail"], detail: str, fix: str = "") -> dict[str, str]:
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


# ───────────────────────── system status ─────────────────────────


@router.get("/system/status")
async def system_status(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    s = ctx.settings
    checks: list[dict[str, str]] = []
    now = ctx.clock.now()

    try:
        async with ctx.db.session() as sess:
            await sess.execute(text("SELECT 1"))
        if ctx.db.dialect == "sqlite":
            checks.append(_check("db", "데이터베이스", "warn", "SQLite (개발·단일 서버용)",
                                 "운영은 ACVM_DATABASE_URL=postgresql+asyncpg://… (PostgreSQL 16 + pgvector)"))
        else:
            checks.append(_check("db", "데이터베이스", "ok", f"{ctx.db.dialect} 연결 정상"))
    except Exception as exc:
        checks.append(_check("db", "데이터베이스", "fail", f"연결 실패: {type(exc).__name__}", "DB 상태와 접속 정보를 확인하세요"))

    redis = ctx.extras.get("redis")
    if redis is None:
        checks.append(_check("redis", "Redis (쿼터·이벤트)", "warn", "메모리 쿼터 사용 — 프로세스가 여러 개면 한도가 공유되지 않음",
                             "ACVM_REDIS_URL 설정"))
    else:
        try:
            await redis.ping()
            checks.append(_check("redis", "Redis (쿼터·이벤트)", "ok", "연결 정상"))
        except Exception as exc:
            checks.append(_check("redis", "Redis (쿼터·이벤트)", "fail", f"연결 실패: {type(exc).__name__}"))

    checks.append(_check("orchestrator", "오케스트레이터", "ok" if s.orchestrator == "temporal" else "warn",
                         "Temporal (내구 실행)" if s.orchestrator == "temporal" else "로컬 실행 — API 프로세스 재시작 시 진행 중 작업 중단",
                         "" if s.orchestrator == "temporal" else "ACVM_ORCHESTRATOR=temporal + worker 실행"))

    link_key = await _has_secret(ctx, s.security.link_signing_key_ref)
    checks.append(_check("link_key", "제휴 링크 서명 키", "ok" if link_key else "fail",
                         "설정됨" if link_key else "없음 — 링크 생성·게시가 차단됩니다",
                         "" if link_key else "설정 › 비밀값 › 제휴 링크 서명 키 등록 (32바이트 이상 임의값)"))

    if s.coupang.sandbox:
        checks.append(_check("coupang", "쿠팡 파트너스 API", "warn", "샌드박스 — 실제 상품·실제 수수료 아님",
                             "키 등록 후 ACVM_COUPANG__SANDBOX=false"))
    else:
        keys = all([await _has_secret(ctx, s.coupang.access_key_ref), await _has_secret(ctx, s.coupang.secret_key_ref)])
        partner = bool(s.coupang.partner_id)
        status: Literal["ok", "warn", "fail"] = "ok" if keys and partner else "fail"
        checks.append(_check("coupang", "쿠팡 파트너스 API", status,
                             "실서비스 연결" if status == "ok" else ("키 미등록" if not keys else "파트너 ID(AF…) 미설정"),
                             "" if status == "ok" else "Access/Secret Key 등록 + ACVM_COUPANG__PARTNER_ID"))

    checks.append(_check("platforms", "소셜 플랫폼 게시", "warn" if s.platforms.dry_run else "ok",
                         "드라이런 — 실제 게시하지 않음" if s.platforms.dry_run else "공식 API로 실제 게시",
                         "계정 토큰 등록 후 ACVM_PLATFORMS__DRY_RUN=false" if s.platforms.dry_run else ""))
    if not s.platforms.tiktok_audited:
        checks.append(_check("tiktok", "TikTok 앱 감사", "warn", "미감사 — 비공개(SELF_ONLY) 게시만 가능",
                             "TikTok Content Posting API 감사 통과 후 ACVM_PLATFORMS__TIKTOK_AUDITED=true"))

    llm_label = "AI 작성·검수 (Claude / Gemini)"
    if ctx.llm is not None:
        prov = str(getattr(ctx.llm, "provider", "anthropic"))
        detail = {"anthropic": f"Claude {s.llm.model_medium} 작성 / {s.llm.model_large} 검수",
                  "gemini": f"Gemini {s.gemini.model_medium} 작성·검수 (Claude 키 미등록)",
                  "anthropic+gemini": f"Claude {s.llm.model_medium} 작성·검수 · 장애 시 Gemini 자동 대체"}.get(prov, prov)
        checks.append(_check("llm", llm_label, "ok", detail))
    elif s.llm.backend in ("anthropic", "gemini"):
        checks.append(_check("llm", llm_label, "fail", "API 키 없음", "운영 준비 › AI 연동 › 키 등록"))
    else:
        checks.append(_check("llm", llm_label, "warn", "오프라인 작성기 — 근거 기반 템플릿 대본",
                             "운영 준비 › AI 연동 › Claude 또는 Gemini 키 등록 (저장 즉시 전환)"))
    for st in await ai.statuses(ctx):
        if st["name"] in ("anthropic", "gemini") and st["state"] in ("active", "missing", "unverified"):
            continue  # summarised by the llm check above
        if st["state"] == "active":
            checks.append(_check(f"ai_{st['name']}", st["label"], "ok", st["detail"]))
        elif st["state"] in ("error", "degraded"):
            checks.append(_check(f"ai_{st['name']}", st["label"], "warn", st["detail"],
                                 "운영 준비 › AI 연동 › 키 다시 등록 또는 연결 확인"))

    ff = shutil.which(s.render.ffmpeg)
    checks.append(_check("ffmpeg", "렌더러 (FFmpeg)", "ok" if ff else "fail", ff or "ffmpeg 없음 — 쇼츠 제작 불가",
                         "" if ff else "ffmpeg 설치 또는 ACVM_RENDER__FFMPEG"))
    try:
        from acvm.media.fonts import resolve_font

        font = resolve_font(s.render.font_file, s.render.font_family)
        checks.append(_check("font", "한글 폰트", "ok", str(getattr(font, "family", "") or getattr(font, "path", ""))))
    except Exception as exc:
        checks.append(_check("font", "한글 폰트", "fail", f"{type(exc).__name__}: {str(exc)[:80]}", "fonts-noto-cjk 설치"))

    public_ok = not s.objects.public_base_url.startswith(("http://localhost", "http://127."))
    checks.append(_check("media_url", "미디어 공개 URL", "ok" if public_ok else ("fail" if not s.platforms.dry_run else "warn"),
                         s.objects.public_base_url,
                         "" if public_ok else "Instagram·TikTok은 공개 HTTPS URL에서 영상을 가져갑니다 — R2 커스텀 도메인 설정"))

    checks.append(_check("auth", "콘솔 인증", "ok" if s.security.auth_mode == "cloudflare_access" else "warn",
                         "Cloudflare Access SSO" if s.security.auth_mode == "cloudflare_access" else "개발 토큰 — 외부 공개 금지",
                         "" if s.security.auth_mode == "cloudflare_access" else "ACVM_SECURITY__AUTH_MODE=cloudflare_access"))
    if s.security.secret_backend == "vault":
        checks.append(_check("secrets", "비밀값 저장소", "ok", "HashiCorp Vault"))
    elif isinstance(ctx.secrets, DevFileSecretStore):
        checks.append(_check("secrets", "비밀값 저장소", "warn", f"서버 로컬 파일 ({getattr(ctx.secrets, 'path', 'secrets.json')}) "
                             "— 단일 서버 전용, 권한 600",
                             "운영: ACVM_SECURITY__SECRET_BACKEND=vault"))
    else:
        checks.append(_check("secrets", "비밀값 저장소", "warn", "환경변수/메모리 — 콘솔 입력값은 재시작 시 사라짐",
                             "ACVM_SECURITY__SECRET_BACKEND=vault"))

    ws_id = _ws(p)
    async with ctx.uow() as repos:
        accounts = await repos.accounts.list(ws_id)
        legal = {k: bool((await repos.kv.get_stale(f"checklist:{ws_id}:{k}") or {}).get("done")) for k in LEGAL_CHECKLIST}
        spent = await repos.costs.workspace_total(ws_id, ctx.day_start_kst())
        budget = await ctx.daily_budget(repos, ws_id)
        breakers = (await repos.session.execute(select(t.KVCacheRow).where(t.KVCacheRow.key.like("coupang:breaker:%")))).scalars().all()
    acc_rows = []
    for a in accounts:
        configured = await _has_secret(ctx, a.token_ref)
        used = await ctx.quota.window_usage(f"{a.id}:{a.platform.value}", 86_400, now)
        acc_rows.append({"id": str(a.id), "platform": a.platform.value, "handle": a.handle, "niche": a.niche,
                         "status": a.status.value, "credential": "configured" if configured
                         else ("not_required" if a.platform is Platform.KAKAO or s.platforms.dry_run else "missing"),
                         "used_24h": used, "daily_cap": a.daily_cap})
    missing = [a for a in acc_rows if a["credential"] == "missing"]
    if missing:
        checks.append(_check("tokens", "계정 토큰", "fail", f"{len(missing)}개 계정 토큰 없음 — 해당 계정 게시 불가",
                             "계정 화면 › 토큰 등록"))
    g0_items = [
        {"id": "coupang_api", "label": "쿠팡 파트너스 실 API 연결", "done": not s.coupang.sandbox and
         next((c["status"] == "ok" for c in checks if c["id"] == "coupang"), False)},
        *({"id": k, "label": v, "done": legal[k]} for k, v in LEGAL_CHECKLIST.items()),
    ]
    return {
        "mode": {"env": s.env, "orchestrator": s.orchestrator, "coupang_sandbox": s.coupang.sandbox,
                 "platform_dry_run": s.platforms.dry_run, "llm_backend": s.llm.backend,
                 "auth_mode": s.security.auth_mode, "tiktok_audited": s.platforms.tiktok_audited},
        "checks": checks,
        "summary": {k: sum(1 for c in checks if c["status"] == k) for k in ("ok", "warn", "fail")},
        "g0": {"passed": all(i["done"] for i in g0_items), "items": g0_items},
        "budget": {"today_spent_krw": round(spent, 1), "daily_budget_krw": budget},
        "accounts": acc_rows,
        "breakers": [{"name": b.key.removeprefix("coupang:breaker:"), **{k: v for k, v in (b.value or {}).items()
                                                                          if k in ("state", "opened_at", "failures")}}
                     for b in breakers],
        "secrets": [{"name": n, "label": lbl, "configured": await _has_secret(ctx, _ref(ctx, spec))}
                    for n, (spec, lbl) in SECRET_SLOTS.items()],
    }


class ChecklistIn(BaseModel):
    item: str
    done: bool


@router.post("/system/checklist")
async def set_checklist(body: ChecklistIn, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    if body.item not in LEGAL_CHECKLIST:
        raise HTTPException(422, "unknown checklist item")
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        await repos.kv.put(f"checklist:{_ws(p)}:{body.item}", {"done": body.done, "by": p.subject,
                                                               "at": ctx.clock.now().isoformat()},
                           timedelta(days=3650), ctx.clock.now())
        await audit(repos, ctx.clock, p.subject, "checklist.set", body.item, done=body.done)
    return {"item": body.item, "done": body.done}


class SecretIn(BaseModel):
    value: str = Field(min_length=8, max_length=8192)


@router.put("/system/secrets/{name}")
async def put_secret(name: str, body: SecretIn, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    """Write-only. The value goes straight to the SecretStore and is never echoed, logged or audited."""
    if name not in SECRET_SLOTS:
        raise HTTPException(404, "unknown secret slot")
    ctx = ctx_of(request)
    await ctx.secrets.put(_ref(ctx, SECRET_SLOTS[name][0]), body.value.strip())
    integration = await ai.on_secret_changed(ctx, name)  # verify + switch on (LLM chain, TTS, image/video) now
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "secret.set", name,
                    **({"integration": integration["name"], "state": integration["state"]} if integration else {}))
    return {"name": name, "configured": True, "persistent": _persistent(ctx), "integration": integration}


@router.delete("/system/secrets/{name}")
async def delete_secret(name: str, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    """Remove a console-entered secret; whatever depended on it switches off (falls back) immediately."""
    if name not in SECRET_SLOTS:
        raise HTTPException(404, "unknown secret slot")
    ctx = ctx_of(request)
    ref = _ref(ctx, SECRET_SLOTS[name][0])
    removed = await ctx.secrets.delete(ref)
    if not removed and await _has_secret(ctx, ref):
        raise HTTPException(409, "환경변수(또는 외부 설정)로 지정된 값이라 콘솔에서 지울 수 없습니다")
    integration = await ai.on_secret_changed(ctx, name)
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "secret.delete", name)
    return {"name": name, "configured": False, "removed": removed, "integration": integration}


# ───────────────────────── AI integrations ─────────────────────────


def _integration(name: str) -> str:
    if name not in ai.INTEGRATIONS:
        raise HTTPException(404, "unknown integration")
    return name


@router.get("/system/integrations")
async def list_integrations(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    llm = ctx.llm
    return {"items": await ai.statuses(ctx),
            "llm": {"provider": getattr(llm, "provider", "anthropic") if llm is not None else "offline"}}


@router.post("/system/integrations/{name}/verify")
async def verify_integration(name: str, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    """Re-check the key live (no billable call) and re-apply it."""
    ctx = ctx_of(request)
    slot = ai.INTEGRATIONS[_integration(name)].slots[0]
    result = await ai.on_secret_changed(ctx, slot)
    assert result is not None
    return result


class IntegrationPatch(BaseModel):
    enabled: bool


@router.put("/system/integrations/{name}")
async def patch_integration(name: str, body: IntegrationPatch, request: Request,
                            p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    """Pause/resume automatic use without deleting the key."""
    ctx = ctx_of(request)
    result = await ai.set_enabled(ctx, _integration(name), body.enabled, by=p.subject)
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "integration.enabled", name, enabled=body.enabled)
    return result


@router.post("/system/integrations/elevenlabs/voices")
async def remap_elevenlabs_voices(request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    """Re-map the fast catalog voices to the account's ElevenLabs voices (e.g. after adding voices there)."""
    ctx = ctx_of(request)
    if not await ai.is_active(ctx, "elevenlabs"):
        raise HTTPException(409, "ElevenLabs 키가 등록·확인되지 않았습니다")
    try:
        result = await ai.remap_voices(ctx)
    except Exception as exc:
        raise HTTPException(502, f"ElevenLabs 음성 목록을 가져오지 못했습니다: {type(exc).__name__}") from exc
    async with ctx.uow() as repos:
        await audit(repos, ctx.clock, p.subject, "integration.voices_mapped", "elevenlabs", mapped=result["mapped"])
    return {**result, "integration": await ai.status(ctx, "elevenlabs")}


# ───────────────────────── workspace ─────────────────────────


@router.get("/workspace")
async def get_workspace(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        ws = await repos.workspaces.get(_ws(p))
        spent = await repos.costs.workspace_total(_ws(p), ctx.day_start_kst())
        month = await repos.costs.workspace_total(_ws(p), ctx.day_start_kst() - timedelta(days=30))
    if ws is None:
        raise HTTPException(404, "workspace not found")
    return {"id": str(ws.id), "name": ws.name, "plan": ws.plan, "daily_budget_krw": ws.daily_budget_krw,
            "today_spent_krw": round(spent, 1), "last30d_spent_krw": round(month, 1)}


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    daily_budget_krw: float | None = Field(default=None, ge=0, le=10_000_000)


@router.patch("/workspace")
async def patch_workspace(body: WorkspacePatch, request: Request, p: Principal = Depends(require(Perm.ADMIN))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        row = await repos.session.get(t.WorkspaceRow, _ws(p))
        if row is None:
            raise HTTPException(404, "workspace not found")
        if body.name is not None:
            row.name = body.name
        if body.daily_budget_krw is not None:
            row.daily_budget_krw = body.daily_budget_krw
        await audit(repos, ctx.clock, p.subject, "workspace.update", str(row.id), **body.model_dump(exclude_none=True))
    return await get_workspace(request, p)


# ───────────────────────── account credentials ─────────────────────────


class CredentialIn(BaseModel):
    token: str = Field(min_length=10, max_length=8192, description="Access token, or the JSON token bundle")
    platform_user_id: str | None = None


@router.put("/accounts/{account_id}/credentials")
async def put_credentials(account_id: uuid.UUID, body: CredentialIn, request: Request,
                          p: Principal = Depends(require(Perm.MANAGE_ACCOUNTS))) -> dict[str, Any]:
    """Write-only token entry (§10: tokens live only in the SecretStore; the DB keeps the reference)."""
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        row = await repos.session.get(t.ChannelAccountRow, account_id)
        if row is None or row.workspace_id != _ws(p):
            raise HTTPException(404, "account not found")
        token_ref = row.token_ref
        if body.platform_user_id:
            row.platform_user_id = body.platform_user_id
        await audit(repos, ctx.clock, p.subject, "account.credentials.set", str(account_id))
    await ctx.secrets.put(token_ref, body.token.strip())
    return {"account_id": str(account_id), "configured": True, "persistent": _persistent(ctx)}


# ───────────────────────── publications ─────────────────────────


def _permalink(pub: t.PublicationRow) -> str | None:
    pid = pub.platform_post_id or ""
    if pid.startswith(("https://", "http://")):
        return pid
    if not pid:
        return None
    return {"youtube": f"https://youtube.com/shorts/{pid}", "facebook": f"https://www.facebook.com/{pid}"}.get(pub.platform)


@router.get("/publications")
async def list_publications(request: Request, state: str | None = None, platform: Platform | None = None,
                            account_id: uuid.UUID | None = None, since: datetime | None = None,
                            until: datetime | None = None, limit: int = Query(200, le=1000),
                            p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    q = (select(t.PublicationRow, t.ContentRow, t.ChannelAccountRow, t.AffiliateLinkRow, t.ProductRow)
         .join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id)
         .join(t.ChannelAccountRow, t.ChannelAccountRow.id == t.PublicationRow.channel_account_id)
         .join(t.AffiliateLinkRow, t.AffiliateLinkRow.id == t.PublicationRow.link_id)
         .join(t.ProductRow, t.ProductRow.id == t.ContentRow.product_id)
         .where(t.ContentRow.workspace_id == _ws(p)))
    if state:
        q = q.where(t.PublicationRow.state.in_([x.strip().upper() for x in state.split(",")]))
    if platform:
        q = q.where(t.PublicationRow.platform == platform.value)
    if account_id:
        q = q.where(t.PublicationRow.channel_account_id == account_id)
    if since:
        q = q.where(t.PublicationRow.scheduled_at >= since)
    if until:
        q = q.where(t.PublicationRow.scheduled_at < until)
    q = q.order_by(t.PublicationRow.scheduled_at.desc()).limit(limit)
    async with ctx.uow() as repos:
        rows = (await repos.session.execute(q)).all()
    items = []
    for pub, content, acc, link, product in rows:
        metrics = await ctx.perf.latest_metrics(pub.id) if pub.state == "PUBLISHED" else {}
        items.append({
            "id": str(pub.id), "platform": pub.platform, "state": pub.state, "account_handle": acc.handle,
            "account_id": str(acc.id), "content_id": str(content.id), "kind": content.kind,
            "hook": (content.script or {}).get("hooks", [""])[(content.script or {}).get("chosen_hook", 0)]
            if (content.script or {}).get("hooks") else "",
            "product": {"id": product.id, "name": product.name, "price": product.price, "image_url": product.image_url},
            "scheduled_at": pub.scheduled_at.isoformat(), "published_at": pub.published_at.isoformat() if pub.published_at else None,
            "permalink": _permalink(pub), "sub_id": link.sub_id, "link_placement": pub.link_placement,
            "caption": pub.caption, "error": pub.error, "attempts": pub.attempts,
            "metrics": {k: metrics.get(k) for k in ("views", "link_clicks", "likes", "shares") if k in metrics},
        })
    return {"items": items}


class TakedownIn(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


@router.post("/publications/{publication_id}/takedown")
async def takedown(publication_id: uuid.UUID, body: TakedownIn, request: Request,
                   p: Principal = Depends(require(Perm.REVIEW_HOLD))) -> dict[str, Any]:
    pub = await request.app.state.gateway.pipeline.dist.take_down(publication_id, body.reason)
    async with ctx_of(request).uow() as repos:
        await audit(repos, ctx_of(request).clock, p.subject, "publication.takedown", str(publication_id), reason=body.reason)
    return {"publication_id": str(pub.id), "state": pub.state.value, "error": pub.error}


# ───────────────────────── content dossier ─────────────────────────


@router.get("/contents/{content_id}")
async def content_detail(content_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        c = await repos.contents.get(content_id)
        if c is None or c.workspace_id != _ws(p):
            raise HTTPException(404, "content not found")
        product = await repos.products.get(c.product_id)
        checks = (await repos.session.execute(select(t.ComplianceCheckRow).where(t.ComplianceCheckRow.content_id == content_id)
                                              .order_by(t.ComplianceCheckRow.decided_at))).scalars().all()
        pubs = await repos.publications.by_content(content_id)
        review = await repos.compliance.review_for_content(content_id)
        cost = await repos.costs.content_total(content_id)
    r = c.render

    async def signed(uri: str | None) -> str | None:
        if not uri:
            return None
        try:
            return await ctx.objects.signed_url(uri, ctx.settings.objects.signed_url_ttl_seconds)
        except Exception:
            return None

    return {
        "id": str(c.id), "kind": c.kind.value, "status": c.status.value, "persona": c.persona, "template_id": c.template_id,
        "hook_variant": c.hook_variant, "similarity_max": c.similarity_max, "created_at": c.created_at.isoformat(),
        "run_id": str(c.created_by_run) if c.created_by_run else None,
        "parent_content_id": str(c.parent_content_id) if c.parent_content_id else None,
        "product": product.model_dump(mode="json") if product else None,
        "script": c.script.model_dump(mode="json"), "claims": [x.model_dump(mode="json") for x in c.claims],
        "render": (r.model_dump(mode="json") | {"media_url": await signed(r.media_uri),
                                                "thumbnail_url": await signed(r.thumbnail_uri)}) if r else None,
        "checks": [{"id": str(ch.id), "stage": ch.stage, "verdict": ch.verdict, "rule_set_version": ch.rule_set_version,
                    "iteration": ch.iteration, "decided_at": ch.decided_at.isoformat(), "findings": ch.findings}
                   for ch in checks],
        "review": {"id": str(review.id), "status": review.status, "reasons": review.reasons, "decided_by": review.decided_by,
                   "note": review.note} if review else None,
        "publications": [{"id": str(x.id), "platform": x.platform.value, "state": x.state.value,
                          "scheduled_at": x.scheduled_at.isoformat(), "caption": x.caption} for x in pubs],
        "cost_krw": round(cost, 1),
    }


# ───────────────────────── alerts ─────────────────────────

_ALERT_ACTIONS = {
    "gate.block": ("high", "게이트 차단"), "preflight.block": ("high", "게시 직전 차단"), "gate.hold": ("medium", "HOLD 대기"),
    "invalid_click": ("high", "무효 클릭 의심"), "link.paused": ("high", "링크 일시 중지"),
    "account.cooldown": ("high", "계정 쿨다운"), "account_cooldown": ("high", "계정 쿨다운"),
    "publication_failed": ("medium", "게시 실패"), "token_refresh_failed": ("high", "토큰 갱신 실패"),
    "manual_takedown_required": ("medium", "수동 삭제 필요"), "budget.exceeded": ("medium", "예산 상한 도달"),
    "link.integrity_violation": ("high", "링크 무결성 위반"), "publish_refused_not_approved": ("high", "미승인 게시 시도 차단"),
    "publication_expired": ("medium", "컨테이너 만료"),
}


@router.get("/alerts")
async def alerts(request: Request, limit: int = Query(50, le=500), p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        entries = await repos.audit.recent(2000)
        accounts = await repos.accounts.list(_ws(p))
        holds = await repos.compliance.pending_reviews(_ws(p))
    items = []
    for e in entries:
        key = next((k for k in _ALERT_ACTIONS if k in e.action), None)
        if key is None:
            continue
        sev, label = _ALERT_ACTIONS[key]
        items.append({"at": e.at.isoformat(), "severity": sev, "kind": key, "label": label, "target": e.target,
                      "actor": e.actor, "detail": {k: v for k, v in e.payload.items() if k != "findings"} | (
                          {"findings": [f.get("message") for f in e.payload.get("findings", [])][:5]}
                          if "findings" in e.payload else {})})
        if len(items) >= limit:
            break
    cooling = [{"id": str(a.id), "platform": a.platform.value, "handle": a.handle, "status": a.status.value,
                "until": a.cooldown_until.isoformat() if a.cooldown_until else None}
               for a in accounts if a.status is not AccountStatus.ACTIVE]
    return {"items": items, "accounts_attention": cooling, "pending_holds": len(holds),
            "counts": {s: sum(1 for i in items if i["severity"] == s) for s in ("high", "medium")}}


__all__ = ["PublicationState", "router"]
