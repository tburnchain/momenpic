"""External AI integrations — register a key once, everything that can use it switches on by itself.

==========  =====================================================================================================
Claude      writer · critic · command parsing · gate claim judge · R-03 keyframe vision
Gemini      the same LLM roles when no Claude key exists, automatic stand-in when Claude fails, STT for own footage
GPT Image   scene backgrounds for *non-product* beats (problem/amplify) — the product is never generated (§4 M5)
Higgsfield  animates those AI scene stills into short motion clips; Studio tool "AI 모션" for AI scenes only
ElevenLabs  real Korean TTS: the TTS cache is dropped and the 9 fast catalog voices are mapped to account voices
==========  =====================================================================================================

Flow on ``PUT /system/secrets/{slot}`` (and on delete): :func:`on_secret_changed` → drop cached clients → verify the
key live (cheap, non-billable endpoint) → re-resolve the LLM chain / drop the TTS cache / map ElevenLabs voices →
report what is now automatic. No restart. Each integration can be paused from the console without deleting its key
(``integrations:{name}:enabled``); a key that failed verification is treated as inactive until it is re-checked.

Secrets never leave the SecretStore: verification results are keyed by a short BLAKE2 fingerprint of the value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

from acvm.core.logging import get_logger

if TYPE_CHECKING:
    from acvm.context import AppContext
    from acvm.infra.gemini import GeminiLLM
    from acvm.ports import ImageGen, VideoGen

log = get_logger(__name__)

# slot name → (settings-path resolver spec or literal ref, label) — merged into ops.SECRET_SLOTS
INTEGRATION_SLOTS: dict[str, tuple[str, str]] = {
    "anthropic_api_key": ("anthropic/api_key", "Claude API 키"),
    "gemini_api_key": ("gemini.key_ref", "Google Gemini API 키"),
    "openai_api_key": ("openai_image.key_ref", "OpenAI API 키 (GPT Image)"),
    "higgsfield_key_id": ("higgsfield.key_id_ref", "Higgsfield API Key ID"),
    "higgsfield_key_secret": ("higgsfield.key_secret_ref", "Higgsfield API Key Secret"),
    "elevenlabs_api_key": ("elevenlabs/api_key", "ElevenLabs API 키"),
}
VOICE_MAP_REF = "elevenlabs/voice_map"
_LONG = timedelta(days=3650)
_CLIENTS = "integrations.clients"


@dataclass(frozen=True, slots=True)
class IntegrationSpec:
    name: str
    label: str
    slots: tuple[str, ...]
    automates: tuple[str, ...]
    key_url: str
    guardrails: tuple[str, ...] = ()


INTEGRATIONS: dict[str, IntegrationSpec] = {
    "anthropic": IntegrationSpec(
        "anthropic", "Claude (대본·검수)", ("anthropic_api_key",),
        ("대본 작성(Writer)·검수(Critic)를 Claude가 수행", "자연어 명령 해석", "게이트 근거 판정·키프레임 비전 검사(R-03)"),
        "https://console.anthropic.com/settings/keys"),
    "gemini": IntegrationSpec(
        "gemini", "Google Gemini", ("gemini_api_key",),
        ("Claude 키가 없으면 대본 작성·검수·명령 해석·게이트 판정을 Gemini가 수행",
         "Claude 장애·한도 초과 시 Gemini로 자동 대체", "직접 촬영 영상 대본 추출(STT)"),
        "https://aistudio.google.com/apikey",
        ("거절(안전 판단)된 요청은 다른 모델로 우회하지 않습니다",)),
    "openai_image": IntegrationSpec(
        "openai_image", "OpenAI GPT Image", ("openai_api_key",),
        ("문제 상황 장면(비상품 비트) 배경 이미지를 대본에 맞춰 자동 생성", "생성 이미지는 권리 원장에 'AI 생성 배경'으로 기록",
         "영상에 'AI 생성 장면' 표시 자동 삽입"),
        "https://platform.openai.com/api-keys",
        ("상품 이미지는 절대 생성·변형하지 않습니다(상품 비트는 실제 상품 사진만)", "실존 인물 얼굴·브랜드·문구는 생성하지 않습니다")),
    "higgsfield": IntegrationSpec(
        "higgsfield", "Higgsfield 영상", ("higgsfield_key_id", "higgsfield_key_secret"),
        ("AI 장면 이미지를 5초 모션 영상으로 자동 변환해 해당 비트에 배치", "스튜디오 툴 › 이미지→영상 'AI 모션' (AI 장면 이미지 전용)"),
        "https://console.higgsfield.ai",
        ("상품 사진은 AI 모션에 쓰지 않습니다(카메라 모션만 허용)",)),
    "elevenlabs": IntegrationSpec(
        "elevenlabs", "ElevenLabs 음성", ("elevenlabs_api_key",),
        ("TTS가 즉시 ElevenLabs 실제 음성으로 전환", "빠른 보이스 9종을 계정 음성에 자동 매핑(느린 내레이션형 음성 제외)",
         "미리듣기 음성 자동 갱신"),
        "https://elevenlabs.io/app/settings/api-keys"),
}


# ───────────────────────── secret plumbing ─────────────────────────


def slot_ref(ctx: AppContext, slot: str) -> str:
    spec = INTEGRATION_SLOTS[slot][0]
    if "." not in spec:
        return spec
    section, field = spec.split(".", 1)
    return str(getattr(getattr(ctx.settings, section), field))


def integration_for_slot(slot: str) -> str | None:
    return next((n for n, s in INTEGRATIONS.items() if slot in s.slots), None)


async def _secret(ctx: AppContext, ref: str) -> str | None:
    try:
        value = await ctx.secrets.get(ref)
    except Exception:
        return None
    return value.strip() or None


async def _values(ctx: AppContext, name: str) -> list[str] | None:
    vals = [await _secret(ctx, slot_ref(ctx, s)) for s in INTEGRATIONS[name].slots]
    return None if any(v is None for v in vals) else [v for v in vals if v is not None]


def _fingerprint(values: Sequence[str]) -> str:
    return hashlib.blake2b("\x1f".join(values).encode(), digest_size=6).hexdigest()


async def _kv(ctx: AppContext, key: str) -> dict[str, Any] | None:
    async with ctx.uow() as repos:
        return await repos.kv.get_stale(key)


async def _kv_put(ctx: AppContext, key: str, value: dict[str, Any]) -> None:
    async with ctx.uow() as repos:
        await repos.kv.put(key, value, _LONG, ctx.clock.now())


async def is_enabled(ctx: AppContext, name: str) -> bool:
    row = await _kv(ctx, f"integrations:{name}:enabled")
    return bool(row.get("enabled", True)) if row else True


async def set_enabled(ctx: AppContext, name: str, enabled: bool, *, by: str = "") -> dict[str, Any]:
    if name not in INTEGRATIONS:
        raise KeyError(name)
    await _kv_put(ctx, f"integrations:{name}:enabled", {"enabled": enabled, "by": by,
                                                        "at": ctx.clock.now().isoformat()})
    await _apply(ctx, name)
    return await status(ctx, name)


async def _last_check(ctx: AppContext, name: str, fp: str) -> dict[str, Any] | None:
    row = await _kv(ctx, f"integrations:{name}:check")
    return row if row and row.get("fingerprint") == fp else None


async def is_active(ctx: AppContext, name: str) -> bool:
    """Key present, not paused, and not known-bad (a transient check failure does not deactivate)."""
    vals = await _values(ctx, name)
    if vals is None or not await is_enabled(ctx, name):
        return False
    chk = await _last_check(ctx, name, _fingerprint(vals))
    return not (chk is not None and not chk.get("ok") and not chk.get("transient"))


# ───────────────────────── clients ─────────────────────────


def _cached(ctx: AppContext, name: str, fp: str) -> Any:
    hit = ctx.extras.setdefault(_CLIENTS, {}).get(name)
    return hit[1] if hit and hit[0] == fp else None


def _cache(ctx: AppContext, name: str, fp: str, client: Any) -> Any:
    ctx.extras.setdefault(_CLIENTS, {})[name] = (fp, client)
    return client


async def image_gen(ctx: AppContext) -> ImageGen | None:
    if "image_gen" in ctx.extras:  # tests / custom wiring
        return ctx.extras["image_gen"]  # type: ignore[no-any-return]
    if not await is_active(ctx, "openai_image"):
        return None
    vals = await _values(ctx, "openai_image")
    assert vals is not None
    fp = _fingerprint(vals)
    hit = _cached(ctx, "openai_image", fp)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    from acvm.infra.openai_image import OpenAIImageGen

    return _cache(ctx, "openai_image", fp, OpenAIImageGen(ctx.settings.openai_image, api_key=vals[0]))  # type: ignore[no-any-return]


async def video_gen(ctx: AppContext) -> VideoGen | None:
    if "video_gen" in ctx.extras:
        return ctx.extras["video_gen"]  # type: ignore[no-any-return]
    if not await is_active(ctx, "higgsfield"):
        return None
    vals = await _values(ctx, "higgsfield")
    assert vals is not None
    fp = _fingerprint(vals)
    hit = _cached(ctx, "higgsfield", fp)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    from acvm.infra.higgsfield import HiggsfieldVideoGen

    return _cache(ctx, "higgsfield", fp, HiggsfieldVideoGen(ctx.settings.higgsfield, key_id=vals[0],  # type: ignore[no-any-return]
                                                             key_secret=vals[1]))


async def gemini_llm(ctx: AppContext) -> GeminiLLM | None:
    if not await is_active(ctx, "gemini"):
        return None
    vals = await _values(ctx, "gemini")
    assert vals is not None
    from acvm.infra.gemini import GeminiLLM

    return GeminiLLM(ctx.settings.gemini, api_key=vals[0])


async def stt_engine(ctx: AppContext) -> Any:
    """Local faster-whisper when installed (free, word timings), else Gemini when its key is active."""
    from acvm.media.stt import default_engine

    local = default_engine()
    if local is not None:
        return local
    llm = await gemini_llm(ctx)
    if llm is None:
        return None
    from acvm.infra.gemini import GeminiSTT

    return GeminiSTT(llm)


async def _llm_blocked(ctx: AppContext, name: str) -> bool:
    """Paused by the operator, or its console key failed verification (env-provided keys are never blocked)."""
    if not await is_enabled(ctx, name):
        return True
    vals = await _values(ctx, name)
    if vals is None:
        return False
    chk = await _last_check(ctx, name, _fingerprint(vals))
    return chk is not None and not chk.get("ok") and not chk.get("transient")


async def refresh_llm(ctx: AppContext) -> None:
    """Re-resolve ``ctx.llm`` from the current keys and pause switches (Claude → Gemini chain)."""
    if ctx.settings.llm.backend == "offline":
        return
    from acvm.context import resolve_llm

    disabled = {n for n in ("anthropic", "gemini") if await _llm_blocked(ctx, n)}
    ctx.llm = await resolve_llm(ctx.settings, ctx.secrets, disabled=disabled)
    ctx.extras.pop("integrations.llm_fp", None)


def _drop_tts(ctx: AppContext) -> None:
    ctx.extras.pop("m6.tts", None)


# ───────────────────────── verification ─────────────────────────


def _transport(ctx: AppContext, http: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient | None, bool]:
    """(client, offline): an injected client (tests: ``ctx.extras["integrations.http"]``) or the real network;
    test contexts without an injected client never touch the network."""
    client = http or ctx.extras.get("integrations.http")
    return client, client is None and ctx.settings.env == "test"


async def _probe(ctx: AppContext, name: str, vals: list[str], http: httpx.AsyncClient | None) -> dict[str, Any]:
    s = ctx.settings
    if name == "anthropic":
        return await _check_anthropic(vals[0], http)
    if name == "gemini":
        from acvm.infra.gemini import check_gemini

        return await check_gemini(s.gemini, vals[0], http=http)
    if name == "openai_image":
        from acvm.infra.openai_image import check_openai_image

        return await check_openai_image(s.openai_image, vals[0], http=http)
    if name == "higgsfield":
        from acvm.infra.higgsfield import check_higgsfield

        return await check_higgsfield(s.higgsfield, vals[0], vals[1], http=http)
    if name == "elevenlabs":
        from acvm.media.elevenlabs_voices import check_elevenlabs

        return await check_elevenlabs(vals[0], http=http)
    raise KeyError(name)


async def _check_anthropic(key: str, http: httpx.AsyncClient | None) -> dict[str, Any]:
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get("https://api.anthropic.com/v1/models", params={"limit": 1},
                                headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"연결 실패: {type(exc).__name__}"}
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code == 200:
        return {"ok": True, "detail": "키 확인됨"}
    if resp.status_code in (401, 403):
        return {"ok": False, "detail": "키가 유효하지 않습니다 (console.anthropic.com › API Keys 확인)"}
    return {"ok": False, "detail": f"Anthropic 응답 {resp.status_code}"}


async def verify(ctx: AppContext, name: str, *, http: httpx.AsyncClient | None = None,
                 limit_s: float = 25.0) -> dict[str, Any]:
    """Live key check (no billable generation). Stored per key fingerprint; never stores the key."""
    vals = await _values(ctx, name)
    if vals is None:
        return {"ok": False, "detail": "키 미등록", "transient": False}
    client, offline = _transport(ctx, http)
    try:
        res = ({"ok": True, "detail": "테스트 환경 — 연결 확인 생략"} if offline
               else await asyncio.wait_for(_probe(ctx, name, vals, client), limit_s))
    except TimeoutError:
        res = {"ok": False, "detail": "확인 시간 초과"}
    transient = not res.get("ok") and ("연결 실패" in str(res.get("detail")) or "시간 초과" in str(res.get("detail"))
                                       or "응답 5" in str(res.get("detail")))
    row = {"ok": bool(res.get("ok")), "detail": str(res.get("detail", ""))[:300], "transient": transient,
           "fingerprint": _fingerprint(vals), "at": ctx.clock.now().isoformat()}
    await _kv_put(ctx, f"integrations:{name}:check", row)
    log.info("integration_verified", integration=name, ok=row["ok"], transient=transient)
    return row


# ───────────────────────── ElevenLabs voice map ─────────────────────────


async def remap_voices(ctx: AppContext, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Map every fast catalog voice to an account voice and store the map (secret) + names (KV)."""
    from acvm.media.elevenlabs_voices import list_voices, match_voices

    vals = await _values(ctx, "elevenlabs")
    if vals is None:
        return {"mapped": 0, "voices": {}}
    client, offline = _transport(ctx, http)
    if offline:
        return {"mapped": 0, "voices": {}, "detail": "테스트 환경 — 음성 목록 조회 생략"}
    voices = await list_voices(vals[0], http=client)
    mapping = match_voices(voices)
    if not mapping:
        return {"mapped": 0, "voices": {}, "detail": "계정에 사용할 수 있는 음성이 없습니다"}
    vmap = {k: v["voice_id"] for k, v in mapping.items()}
    vmap["default"] = next(iter(vmap.values()))
    await ctx.secrets.put(VOICE_MAP_REF, json.dumps(vmap, ensure_ascii=False))
    await _kv_put(ctx, "integrations:elevenlabs:voices", {"voices": mapping, "at": ctx.clock.now().isoformat(),
                                                          "pool": len(voices)})
    _drop_tts(ctx)
    log.info("elevenlabs_voices_mapped", mapped=len(mapping), pool=len(voices))
    return {"mapped": len(mapping), "voices": mapping}


# ───────────────────────── lifecycle ─────────────────────────


async def _apply(ctx: AppContext, name: str) -> list[str]:
    """Make the running process reflect the current key/switch state of ``name``."""
    ctx.extras.get(_CLIENTS, {}).pop(name, None)
    done: list[str] = []
    if name in ("anthropic", "gemini"):
        await refresh_llm(ctx)
        done.append(f"LLM: {getattr(ctx.llm, 'provider', 'offline') if ctx.llm is not None else 'offline'}")
    if name == "elevenlabs":
        _drop_tts(ctx)
        done.append("TTS 재선택")
    return done


async def on_secret_changed(ctx: AppContext, slot: str, *, http: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """Called after a slot is written or deleted: verify, activate, and report what is now automatic."""
    name = integration_for_slot(slot)
    if name is None:
        return None
    vals = await _values(ctx, name)
    check: dict[str, Any] | None = None
    if vals is not None:
        check = await verify(ctx, name, http=http)
    actions = await _apply(ctx, name)
    if name == "elevenlabs" and check and check.get("ok") and ctx.settings.elevenlabs.auto_voice_map:
        try:
            mapped = await remap_voices(ctx, http=http)
            actions.append(f"보이스 {mapped['mapped']}종 자동 매핑")
        except Exception as exc:  # the key works; mapping can be retried from the console
            log.warning("elevenlabs_voice_map_failed", error=type(exc).__name__)
            actions.append("보이스 자동 매핑 실패 — 콘솔에서 다시 매핑")
    st = await status(ctx, name)
    st["actions"] = actions
    return st


async def status(ctx: AppContext, name: str) -> dict[str, Any]:
    spec = INTEGRATIONS[name]
    slots = [{"name": s, "label": INTEGRATION_SLOTS[s][1],
              "configured": await _secret(ctx, slot_ref(ctx, s)) is not None} for s in spec.slots]
    vals = await _values(ctx, name)
    enabled = await is_enabled(ctx, name)
    chk = await _last_check(ctx, name, _fingerprint(vals)) if vals is not None else None
    if vals is None:
        state, detail = "missing", "키를 등록하면 아래 기능이 자동으로 켜집니다"
    elif not enabled:
        state, detail = "paused", "일시 중지됨 — 키는 보관 중"
    elif chk is None:
        state, detail = "unverified", "등록됨 · 연결 확인 전"
    elif chk.get("ok"):
        state, detail = "active", str(chk.get("detail", ""))
    else:
        state = "degraded" if chk.get("transient") else "error"
        detail = str(chk.get("detail", ""))
    s = ctx.settings
    model, unit = {
        "anthropic": (f"{s.llm.model_medium} / {s.llm.model_large}", "토큰 과금"),
        "gemini": (s.gemini.model_medium, f"입력 {s.gemini.input_krw_per_mtok:,.0f}원·출력 "
                                          f"{s.gemini.output_krw_per_mtok:,.0f}원 / 100만 토큰"),
        "openai_image": (s.openai_image.model, f"이미지 1장 약 {s.openai_image.cost_per_image_krw:,.0f}원"),
        "higgsfield": (s.higgsfield.i2v_model, f"{s.higgsfield.clip_seconds}초 클립 1개 약 "
                                                f"{s.higgsfield.cost_per_clip_krw:,.0f}원"),
        "elevenlabs": (s.elevenlabs.model_id, f"1,000자 약 {s.elevenlabs.krw_per_1k_chars:,.0f}원"),
    }[name]
    out: dict[str, Any] = {
        "name": name, "label": spec.label, "state": state, "detail": detail, "enabled": enabled,
        "configured": vals is not None, "active": state in ("active", "unverified", "degraded"),
        "checked_at": chk.get("at") if chk else None, "slots": slots, "automates": list(spec.automates),
        "guardrails": list(spec.guardrails), "model": model, "unit_cost": unit, "key_url": spec.key_url,
    }
    if name == "elevenlabs":
        row = await _kv(ctx, "integrations:elevenlabs:voices")
        out["voices"] = (row or {}).get("voices", {})
    return out


async def statuses(ctx: AppContext) -> list[dict[str, Any]]:
    return [await status(ctx, n) for n in INTEGRATIONS]


async def active_summary(ctx: AppContext) -> dict[str, bool]:
    return {n: await is_active(ctx, n) for n in INTEGRATIONS}


__all__ = [
    "INTEGRATIONS",
    "INTEGRATION_SLOTS",
    "IntegrationSpec",
    "active_summary",
    "gemini_llm",
    "image_gen",
    "integration_for_slot",
    "is_active",
    "is_enabled",
    "on_secret_changed",
    "refresh_llm",
    "remap_voices",
    "set_enabled",
    "status",
    "statuses",
    "stt_engine",
    "sync_llm",
    "verify",
    "video_gen",
]


async def sync_llm(ctx: AppContext, *, force: bool = False) -> None:
    """Cheap per-activity check that ``ctx.llm`` matches the current keys/switches — workers in other processes
    (Temporal) pick up a key registered in the console without a restart.

    The first call only records a baseline (whatever the composition root or a test wired stays); later calls
    re-resolve when the Claude/Gemini keys or pause switches changed. ``force`` re-resolves now (API startup).
    """
    if ctx.settings.llm.backend == "offline":
        return
    parts = [await _secret(ctx, "anthropic/api_key") or "", await _secret(ctx, ctx.settings.gemini.key_ref) or "",
             str(await is_enabled(ctx, "anthropic")), str(await is_enabled(ctx, "gemini"))]
    fp = _fingerprint(parts)
    prev = ctx.extras.get("integrations.llm_fp")
    if not force and (prev is None or prev == fp):
        ctx.extras["integrations.llm_fp"] = fp
        return
    await refresh_llm(ctx)
    ctx.extras["integrations.llm_fp"] = fp
