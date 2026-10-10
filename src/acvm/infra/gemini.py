"""Google Gemini adapter (Generative Language REST API, ``x-goog-api-key``).

* :class:`GeminiLLM` implements the :class:`acvm.ports.LLM` port — the same ``structured``/``text`` calls the agents
  make on Claude, so Gemini can be the writer/critic/judge when only a Gemini key is registered, or the automatic
  stand-in when Claude fails (:class:`acvm.infra.llm.ChainLLM`).
* Structured output: ``responseMimeType=application/json`` + ``responseJsonSchema`` (refs inlined), validated with
  the Pydantic model; a schema the API rejects falls back once to prompt-embedded schema + JSON mode.
* Vision: images go as ``inlineData`` parts (R-03 keyframe check).
* :class:`GeminiSTT` implements :class:`acvm.media.stt.STTEngine` (대본 추출 of the operator's own footage).
* :func:`check_gemini` validates a key without spending tokens (``GET models/{model}``).

The key is sent only in the request header and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from acvm.config import GeminiConfig
from acvm.core.errors import UpstreamError
from acvm.core.logging import get_logger
from acvm.infra.llm import LLMRefusal, LLMTruncated, LLMUsage, UsageSink
from acvm.ports import Tier

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

_REFUSAL_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
MAX_OUTPUT_TOKENS = 16_000


def _mime(img: bytes) -> str:
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s against ``$defs`` (Pydantic nested models) so the schema is self-contained."""
    defs = schema.get("$defs", {}) or {}

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 32:
            return node
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged, depth + 1)
            return {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node]
        return node

    out = walk(schema)
    assert isinstance(out, dict)
    return out


def _headers(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    try:
        msg = str(resp.json().get("error", {}).get("message", ""))[:200]
    except Exception:
        msg = ""
    retryable = resp.status_code == 429 or resp.status_code >= 500
    raise UpstreamError(f"Gemini {what} HTTP {resp.status_code}{': ' + msg if msg else ''}",
                        status=resp.status_code, retryable=retryable)


class GeminiLLM:
    """LLM port on Gemini. Tiers map to ``GeminiConfig.model_large/medium/small``."""

    provider = "gemini"

    def __init__(self, cfg: GeminiConfig, *, api_key: str, http: httpx.AsyncClient | None = None,
                 usage_sink: UsageSink | None = None, sleep: Any = asyncio.sleep) -> None:
        self.cfg = cfg
        self._key = api_key
        self._http = http
        self.usage_sink = usage_sink
        self._sleep = sleep

    def model_for(self, tier: Tier) -> str:
        return {"large": self.cfg.model_large, "medium": self.cfg.model_medium, "small": self.cfg.model_small}[tier]

    async def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.cfg.base_url.rstrip('/')}/models/{model}:generateContent"
        attempt = 0
        while True:
            try:
                if self._http is not None:
                    resp = await self._http.post(url, headers=_headers(self._key), json=body,
                                                 timeout=self.cfg.timeout_seconds)
                else:
                    async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as http:
                        resp = await http.post(url, headers=_headers(self._key), json=body)
                _raise_for(resp, "generateContent")
                data: dict[str, Any] = resp.json()
                return data
            except UpstreamError as exc:
                if not exc.retryable or attempt >= self.cfg.max_retries:
                    raise
            except httpx.HTTPError as exc:
                if attempt >= self.cfg.max_retries:
                    raise UpstreamError("Gemini connection error", retryable=True) from exc
            attempt += 1
            await self._sleep(min(8.0, 1.5 * 2 ** (attempt - 1)))

    def _body(self, system: str, user: str, images: Sequence[bytes]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [{"inlineData": {"mimeType": _mime(img),
                                                       "data": base64.standard_b64encode(img).decode()}}
                                       for img in images]
        parts.append({"text": user})
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS}}

    async def _record(self, model: str, purpose: str, data: dict[str, Any], started: float) -> None:
        if self.usage_sink is None:
            return
        u = data.get("usageMetadata") or {}
        inp = int(u.get("promptTokenCount", 0) or 0)
        out = int(u.get("candidatesTokenCount", 0) or 0) + int(u.get("thoughtsTokenCount", 0) or 0)
        cost = (inp * self.cfg.input_krw_per_mtok + out * self.cfg.output_krw_per_mtok) / 1_000_000
        await self.usage_sink(LLMUsage(model=model, purpose=purpose, input_tokens=inp, output_tokens=out,
                                       cache_read_tokens=int(u.get("cachedContentTokenCount", 0) or 0),
                                       cache_write_tokens=0, cost_krw=cost,
                                       latency_ms=(time.perf_counter() - started) * 1000))

    @staticmethod
    def _text_of(data: dict[str, Any]) -> str:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMRefusal("Gemini blocked the prompt", category=str(block))
        cands = data.get("candidates") or []
        if not cands:
            raise LLMTruncated("Gemini returned no candidates")
        cand = cands[0]
        reason = str(cand.get("finishReason") or "")
        if reason in _REFUSAL_REASONS:
            raise LLMRefusal("Gemini declined", category=reason)
        if reason == "MAX_TOKENS":
            raise LLMTruncated("Gemini response truncated at maxOutputTokens")
        parts = (cand.get("content") or {}).get("parts") or []
        return "".join(str(p.get("text", "")) for p in parts if not p.get("thought"))

    async def structured(self, *, tier: Tier, system: str, user: str, schema: type[T],
                         images: Sequence[bytes] = (), effort: str | None = None, purpose: str = "") -> T:
        model = self.model_for(tier)
        started = time.perf_counter()
        body = self._body(system, user, images)
        body["generationConfig"].update({"responseMimeType": "application/json",
                                         "responseJsonSchema": inline_refs(schema.model_json_schema())})
        try:
            data = await self._post(model, body)
        except UpstreamError as exc:
            if exc.status != 400:
                raise
            # schema feature the API does not accept → JSON mode with the schema in the prompt
            body = self._body(system, f"{user}\n\n다음 JSON 스키마에 맞는 JSON 객체 하나만 출력하세요:\n"
                                      f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}", images)
            body["generationConfig"]["responseMimeType"] = "application/json"
            data = await self._post(model, body)
        await self._record(model, purpose, data, started)
        text = self._text_of(data).strip()
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise UpstreamError("Gemini output did not match the schema", retryable=True,
                                purpose=purpose) from exc

    async def text(self, *, tier: Tier, system: str, user: str, effort: str | None = None,
                   purpose: str = "") -> str:
        model = self.model_for(tier)
        started = time.perf_counter()
        data = await self._post(model, self._body(system, user, ()))
        await self._record(model, purpose, data, started)
        return self._text_of(data)


class _Segments(BaseModel):
    class Segment(BaseModel):
        text: str
        start: float
        end: float

    text: str
    segments: list[Segment] = []


class GeminiSTT:
    """STT engine on Gemini audio understanding. Segment timings are spread over words by length."""

    name = "gemini"

    def __init__(self, llm: GeminiLLM) -> None:
        self.llm = llm

    async def transcribe_wav(self, wav: Path, *, language: str) -> Any:
        from acvm.media.stt import Transcript, TranscriptWord

        audio = await asyncio.to_thread(Path(wav).read_bytes)
        if len(audio) > 18 * 1024 * 1024:
            raise UpstreamError("audio too long for inline Gemini transcription (≈9 min max)", retryable=False)
        model = self.llm.model_for("medium")
        body = {
            "systemInstruction": {"parts": [{"text": "You are a precise speech-to-text engine. Transcribe exactly "
                                                     "what is spoken. Do not summarise, translate or add words."}]},
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "audio/wav", "data": base64.standard_b64encode(audio).decode()}},
                {"text": f"Transcribe this audio (language: {language}). Return JSON: full text plus segments "
                         "with start/end in seconds."}]}],
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "responseMimeType": "application/json",
                                 "responseJsonSchema": inline_refs(_Segments.model_json_schema())},
        }
        data = await self.llm._post(model, body)
        try:
            parsed = _Segments.model_validate_json(GeminiLLM._text_of(data).strip())
        except ValidationError as exc:
            raise UpstreamError("Gemini transcript was not valid JSON", retryable=True) from exc
        words: list[TranscriptWord] = []
        for seg in parsed.segments:
            toks = seg.text.split()
            span = max(0.0, seg.end - seg.start)
            total = sum(len(t) for t in toks) or 1
            t = seg.start
            for tok in toks:
                d = span * len(tok) / total
                words.append(TranscriptWord(tok, round(t, 3), round(t + d, 3)))
                t += d
        text = parsed.text.strip() or " ".join(s.text.strip() for s in parsed.segments)
        return Transcript(text=text, words=words, language=language, engine=self.name)


async def check_gemini(cfg: GeminiConfig, api_key: str, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Validate the key and the configured model without generating anything."""
    url = f"{cfg.base_url.rstrip('/')}/models/{cfg.model_medium}"
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        resp = await client.get(url, headers={"x-goog-api-key": api_key})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"연결 실패: {type(exc).__name__}"}
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code == 200:
        name = str(resp.json().get("displayName") or cfg.model_medium)
        return {"ok": True, "detail": f"키 확인됨 · 모델 {name}"}
    if resp.status_code in (400, 401, 403):
        return {"ok": False, "detail": "키가 유효하지 않거나 권한이 없습니다 (Google AI Studio에서 키 확인)"}
    if resp.status_code == 404:
        return {"ok": False, "detail": f"키는 유효하지만 모델 {cfg.model_medium} 을(를) 쓸 수 없습니다 — "
                                       "ACVM_GEMINI__MODEL_* 설정 확인"}
    return {"ok": False, "detail": f"Gemini 응답 {resp.status_code}"}
