"""Higgsfield image→video adapter (``api.higgsfield.ai``) for the :class:`acvm.ports.VideoGen` port.

Lifecycle (docs.higgsfield.ai): presigned upload (``POST /files/generate-upload-url`` → ``PUT upload_url``) →
submit to the model endpoint with an ``Idempotency-Key`` → poll ``status_url`` (2 s, ×1.5, ≤ 10 s) until a terminal
state (``completed`` · ``failed`` · ``nsfw`` · ``canceled``) → download ``video.url``.

Auth is ``Authorization: Key {key_id}:{key_secret}``; credentials are never logged. ACVM animates only AI-generated
scene stills with camera/ambient motion — never product images (§4 M5, design: I2V = camera motion only).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx

from acvm.config import HiggsfieldConfig
from acvm.core.errors import PolicyViolation, UpstreamError
from acvm.core.logging import get_logger
from acvm.ports import GeneratedMedia

log = get_logger(__name__)

TERMINAL = {"completed", "failed", "nsfw", "canceled"}
MAX_VIDEO_BYTES = 200 * 1024 * 1024
_RETRYABLE = {400, 423, 500, 502, 503, 504}  # 400 doubles as "concurrency reached" per Higgsfield's error guide


class HiggsfieldVideoGen:
    provider = "higgsfield"

    def __init__(self, cfg: HiggsfieldConfig, *, key_id: str, key_secret: str, http: httpx.AsyncClient | None = None,
                 sleep: Any = asyncio.sleep, clock: Any = time.monotonic) -> None:
        self.cfg = cfg
        self.model = cfg.i2v_model
        self._auth = f"Key {key_id}:{key_secret}"
        self._http = http
        self._sleep = sleep
        self._clock = clock

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": self._auth, "Accept": "application/json", **(extra or {})}

    async def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self.cfg.timeout_seconds, follow_redirects=True), True

    @staticmethod
    def _raise_for(resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = str(resp.json().get("detail", ""))[:200]
        except Exception:
            detail = ""
        if resp.status_code == 403:
            raise UpstreamError(f"Higgsfield {what}: 크레딧 부족", status=403, retryable=False)
        raise UpstreamError(f"Higgsfield {what} HTTP {resp.status_code}{': ' + detail if detail else ''}",
                            status=resp.status_code, retryable=resp.status_code in _RETRYABLE)

    async def upload(self, http: httpx.AsyncClient, data: bytes, mime: str) -> str:
        resp = await http.post(f"{self.cfg.base_url.rstrip('/')}/files/generate-upload-url",
                               headers=self._headers({"Content-Type": "application/json"}),
                               json={"content_type": mime})
        self._raise_for(resp, "upload-url")
        body = resp.json()
        put = await http.put(str(body["upload_url"]), content=data,
                             headers={str(k): str(v) for k, v in (body.get("upload_headers") or
                                                                 {"Content-Type": mime}).items()})
        if put.status_code >= 400:
            raise UpstreamError(f"Higgsfield upload PUT HTTP {put.status_code}", status=put.status_code,
                                retryable=put.status_code >= 500)
        return str(body["public_url"])

    async def _poll(self, http: httpx.AsyncClient, status_url: str) -> dict[str, Any]:
        deadline = self._clock() + self.cfg.poll_timeout_seconds
        delay = 2.0
        while True:
            resp = await http.get(status_url, headers=self._headers())
            if resp.status_code >= 500:
                pass  # transient: keep polling with backoff
            else:
                self._raise_for(resp, "status")
                body: dict[str, Any] = resp.json()
                if str(body.get("status")) in TERMINAL:
                    return body
            if self._clock() + delay > deadline:
                raise UpstreamError("Higgsfield generation timed out", retryable=True, status_url=status_url)
            await self._sleep(delay)
            delay = min(delay * 1.5, 10.0)

    async def image_to_video(self, image: bytes, mime: str, prompt: str, *, seconds: int | None = None,
                             negative_prompt: str = "", idempotency_key: str = "") -> GeneratedMedia:
        http, own = await self._client()
        try:
            image_url = await self.upload(http, image, mime)
            payload: dict[str, Any] = {"image_url": image_url, "prompt": prompt,
                                       "duration": int(seconds or self.cfg.clip_seconds),
                                       "cfg_scale": self.cfg.cfg_scale}
            if negative_prompt:
                payload["negative_prompt"] = negative_prompt
            resp = await http.post(f"{self.cfg.base_url.rstrip('/')}/{self.cfg.i2v_model.strip('/')}",
                                   headers=self._headers({"Content-Type": "application/json",
                                                          "Idempotency-Key": idempotency_key or str(uuid.uuid4())}),
                                   json=payload)
            self._raise_for(resp, "submit")
            sub = resp.json()
            status_url = str(sub.get("status_url") or
                             f"{self.cfg.base_url.rstrip('/')}/requests/{sub['request_id']}/status")
            done = sub if str(sub.get("status")) in TERMINAL else await self._poll(http, status_url)
            state = str(done.get("status"))
            if state == "nsfw":
                raise PolicyViolation("Higgsfield refused the input (nsfw filter)")
            if state != "completed":
                raise UpstreamError(f"Higgsfield generation {state}: {str(done.get('error', ''))[:160]}",
                                    retryable=state == "failed")
            url = str((done.get("video") or {}).get("url") or "")
            if not url:
                raise UpstreamError("Higgsfield completed without a video URL", retryable=True)
            video = await http.get(url, timeout=self.cfg.timeout_seconds * 3)
            if video.status_code >= 400:
                raise UpstreamError(f"Higgsfield video download HTTP {video.status_code}", retryable=True)
            if len(video.content) > MAX_VIDEO_BYTES:
                raise UpstreamError("Higgsfield video too large", retryable=False)
        finally:
            if own:
                await http.aclose()
        return GeneratedMedia(data=video.content, mime="video/mp4", provider=self.provider, model=self.cfg.i2v_model,
                              prompt=prompt, usage={"request_id": str(done.get("request_id", "")),
                                                    "seconds": payload["duration"],
                                                    "krw": self.cfg.cost_per_clip_krw})


async def check_higgsfield(cfg: HiggsfieldConfig, key_id: str, key_secret: str, *,
                           http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Credentials check without spending credits: an unknown request id answers 404 to a valid key, 401 otherwise."""
    url = f"{cfg.base_url.rstrip('/')}/requests/{uuid.uuid4()}/status"
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(url, headers={"Authorization": f"Key {key_id}:{key_secret}"})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"연결 실패: {type(exc).__name__}"}
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code in (200, 404):
        return {"ok": True, "detail": f"키 확인됨 · 모델 {cfg.i2v_model}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "Key ID 또는 Secret이 올바르지 않습니다 (Higgsfield Console › API keys)"}
    if resp.status_code == 403:
        return {"ok": False, "detail": "크레딧이 부족합니다 (Higgsfield Console에서 충전)"}
    return {"ok": False, "detail": f"Higgsfield 응답 {resp.status_code}"}
