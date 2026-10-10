"""OpenAI GPT Image adapter (``POST /v1/images/generations``) for the :class:`acvm.ports.ImageGen` port.

Used only for *scene* backgrounds of non-product beats (§4 M5: the product is never generated or altered). GPT Image
models always return base64 (``data[0].b64_json``). The key travels only in the Authorization header.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx

from acvm.config import OpenAIImageConfig
from acvm.core.errors import PolicyViolation, UpstreamError
from acvm.ports import GeneratedMedia


def _error(resp: httpx.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error") or {}
        return str(err.get("code") or err.get("type") or ""), str(err.get("message") or "")[:200]
    except Exception:
        return "", ""


class OpenAIImageGen:
    provider = "openai"

    def __init__(self, cfg: OpenAIImageConfig, *, api_key: str, http: httpx.AsyncClient | None = None,
                 sleep: Any = asyncio.sleep, max_retries: int = 2) -> None:
        self.cfg = cfg
        self.model = cfg.model
        self._key = api_key
        self._http = http
        self._sleep = sleep
        self.max_retries = max_retries

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self.cfg.base_url.rstrip('/')}/images/generations"
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        if self._http is not None:
            return await self._http.post(url, headers=headers, json=body, timeout=self.cfg.timeout_seconds)
        async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as http:
            return await http.post(url, headers=headers, json=body)

    async def generate(self, prompt: str, *, size: str | None = None) -> GeneratedMedia:
        body = {"model": self.cfg.model, "prompt": prompt, "size": size or self.cfg.size,
                "quality": self.cfg.quality, "n": 1, "output_format": "jpeg"}
        attempt = 0
        while True:
            try:
                resp = await self._post(body)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise UpstreamError("OpenAI image connection error", retryable=True) from exc
                attempt += 1
                await self._sleep(2.0 * attempt)
                continue
            if resp.status_code < 400:
                break
            code, msg = _error(resp)
            if resp.status_code == 400 and ("moderation" in code or "safety" in code or "content_policy" in code):
                raise PolicyViolation("OpenAI image request refused by the safety system", code=code)
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt >= self.max_retries:
                raise UpstreamError(f"OpenAI image HTTP {resp.status_code}{': ' + msg if msg else ''}",
                                    status=resp.status_code, retryable=retryable, code=code)
            attempt += 1
            await self._sleep(3.0 * attempt)
        data = resp.json()
        items = data.get("data") or []
        if not items or not items[0].get("b64_json"):
            raise UpstreamError("OpenAI image response had no image", retryable=True)
        raw = base64.b64decode(items[0]["b64_json"])
        usage = dict(data.get("usage") or {})
        usage["krw"] = self.cfg.cost_per_image_krw
        return GeneratedMedia(data=raw, mime="image/jpeg", provider=self.provider, model=self.cfg.model,
                              prompt=prompt, usage=usage)


async def check_openai_image(cfg: OpenAIImageConfig, api_key: str, *,
                             http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Key + model availability without generating (``GET /v1/models/{model}``)."""
    url = f"{cfg.base_url.rstrip('/')}/models/{cfg.model}"
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"연결 실패: {type(exc).__name__}"}
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code == 200:
        return {"ok": True, "detail": f"키 확인됨 · 모델 {cfg.model}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "키가 유효하지 않습니다 (platform.openai.com › API keys 확인)"}
    if resp.status_code == 403:
        return {"ok": False, "detail": "권한 없음 — GPT Image는 조직 인증(Organization Verification)이 필요할 수 있습니다"}
    if resp.status_code == 404:
        return {"ok": False, "detail": f"키는 유효하지만 모델 {cfg.model} 을(를) 쓸 수 없습니다 — "
                                       "ACVM_OPENAI_IMAGE__MODEL 확인 또는 조직 인증"}
    return {"ok": False, "detail": f"OpenAI 응답 {resp.status_code}"}
