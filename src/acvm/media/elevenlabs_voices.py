"""ElevenLabs account setup: key check and automatic mapping of the fast catalog voices to account voices.

When the operator registers an ElevenLabs key, every studio voice (④ 보이스) needs a concrete ElevenLabs voice.
:func:`match_voices` assigns them deterministically from the account's voice list:

* gender must match when the voice declares one; age band (20s → young, 30s → young/middle-aged, 40s → middle-aged);
* Korean-verified voices first;
* **fast short-form only** (shorts are 20–25 s pitches): conversational/social/advertising/energetic voices are
  preferred, narration/meditation/ASMR/whisper/"calm" voices are pushed to the bottom;
* distinct voices per catalog entry while the pool allows.

The result is written to the ``elevenlabs/voice_map`` secret (JSON ``{catalog_id: voice_id, "default": …}``); the
voice *names* (not secret) are kept in KV for the console.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from acvm.media.voices import VOICE_CATALOG, VoiceProfile

BASE = "https://api.elevenlabs.io"
_FAST_HINTS = ("conversational", "social", "advert", "energetic", "upbeat", "lively", "bright", "young", "excited",
               "confident", "friendly", "cheerful", "dynamic", "punchy", "fast")
_SLOW_HINTS = ("narrat", "meditat", "asmr", "whisper", "calm", "sooth", "slow", "relax", "sleep", "audiobook",
               "storytell", "gentle", "deep")
_AGE = {"20s": ("young",), "30s": ("young", "middle_aged", "middle aged"), "40s": ("middle_aged", "middle aged")}


def _blob(v: dict[str, Any]) -> str:
    labels = v.get("labels") or {}
    parts = [str(v.get("name", "")), str(v.get("description", "") or ""),
             *(f"{k} {val}" for k, val in labels.items())]
    return " ".join(parts).lower()


def _korean(v: dict[str, Any]) -> bool:
    langs = v.get("verified_languages") or []
    if any(str(x.get("language", "")).lower() in ("ko", "korean") for x in langs if isinstance(x, dict)):
        return True
    return "korean" in _blob(v)


def score(profile: VoiceProfile, v: dict[str, Any]) -> float:
    """Higher is better; ``-inf`` when the voice's declared gender contradicts the catalog voice."""
    labels = {str(k).lower(): str(val).lower() for k, val in (v.get("labels") or {}).items()}
    gender = labels.get("gender", "")
    want = "female" if profile.gender == "F" else "male"
    if gender and gender != want:
        return float("-inf")
    s = 2.0 if gender == want else 0.0
    age = labels.get("age", "")
    if age:
        s += 1.5 if age in _AGE.get(profile.age_band, ()) else -0.5
    if _korean(v):
        s += 3.0
    blob = _blob(v)
    s += 0.8 * sum(h in blob for h in _FAST_HINTS)
    s -= 2.5 * sum(h in blob for h in _SLOW_HINTS)
    if str(v.get("category", "")) in ("cloned", "professional", "generated"):
        s += 0.5  # the operator's own (licensed) voices first
    return s


def match_voices(voices: Sequence[dict[str, Any]],
                 catalog: Sequence[VoiceProfile] = VOICE_CATALOG) -> dict[str, dict[str, str]]:
    """catalog id → {voice_id, name}. Deterministic (ties break on voice_id)."""
    pool = [v for v in voices if v.get("voice_id")]
    if not pool:
        return {}
    used: set[str] = set()
    out: dict[str, dict[str, str]] = {}
    for profile in catalog:
        ranked = sorted(pool, key=lambda v: (-score(profile, v), str(v["voice_id"])))
        ranked = [v for v in ranked if score(profile, v) != float("-inf")] or ranked
        fresh = [v for v in ranked if str(v["voice_id"]) not in used]
        pick = (fresh or ranked)[0]
        used.add(str(pick["voice_id"]))
        out[profile.id] = {"voice_id": str(pick["voice_id"]), "name": str(pick.get("name", ""))}
    return out


async def list_voices(api_key: str, *, http: httpx.AsyncClient | None = None, max_pages: int = 3) -> list[dict[str, Any]]:
    """The account's voices via ``GET /v2/voices`` (paginated)."""
    client = http or httpx.AsyncClient(timeout=30.0)
    voices: list[dict[str, Any]] = []
    token: str | None = None
    try:
        for _ in range(max_pages):
            params: dict[str, Any] = {"page_size": 100}
            if token:
                params["next_page_token"] = token
            resp = await client.get(f"{BASE}/v2/voices", headers={"xi-api-key": api_key}, params=params)
            if resp.status_code >= 400:
                from acvm.core.errors import UpstreamError

                raise UpstreamError(f"ElevenLabs voices HTTP {resp.status_code}", status=resp.status_code,
                                    retryable=resp.status_code == 429 or resp.status_code >= 500)
            body = resp.json()
            voices += list(body.get("voices", []))
            token = body.get("next_page_token")
            if not body.get("has_more") or not token:
                break
    finally:
        if http is None:
            await client.aclose()
    return voices


async def check_elevenlabs(api_key: str, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(f"{BASE}/v2/voices", headers={"xi-api-key": api_key}, params={"page_size": 1})
        if resp.status_code != 200:
            if resp.status_code == 401:
                return {"ok": False, "detail": "키가 유효하지 않거나 voices 읽기 권한이 없습니다"}
            return {"ok": False, "detail": f"ElevenLabs 응답 {resp.status_code}"}
        detail = "키 확인됨"
        sub = await client.get(f"{BASE}/v1/user/subscription", headers={"xi-api-key": api_key})
        if sub.status_code == 200:
            b = sub.json()
            used, limit = int(b.get("character_count", 0) or 0), int(b.get("character_limit", 0) or 0)
            if limit:
                detail += f" · 이번 달 {used:,}/{limit:,}자 사용"
        return {"ok": True, "detail": detail}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"연결 실패: {type(exc).__name__}"}
    finally:
        if http is None:
            await client.aclose()
