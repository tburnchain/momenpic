"""Claude adapter for the LLM port (§5.1 model tiers).

* tiers: large → Claude Opus 5.5, medium → Claude Sonnet 5.5, small → Claude Haiku 4.5
  (Critic runs on a different model from Writer by construction: Writer=medium, Critic=large);
* structured outputs via ``beta.messages.parse(output_format=Model)`` — schema-valid or an error;
* stable system prompts are cached (``cache_control``) — every agent's system prompt is frozen text;
* server-side refusal fallbacks (``fallbacks="default"``) on Opus 5.5 / Sonnet 5.5;
* usage → KRW cost callback feeding the cost ledger (§5.2 budget capping).
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import anthropic
from pydantic import BaseModel

from acvm.config import LLMConfig
from acvm.core.errors import ACVMError, UpstreamError
from acvm.core.logging import get_logger
from acvm.ports import Tier

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

USD_KRW = 1_400.0
# $ per 1M tokens: (input, output, cache_read)
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
_EFFORT_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


class LLMRefusal(ACVMError):
    code = "llm_refusal"


class LLMTruncated(ACVMError):
    retryable = True
    code = "llm_truncated"


@dataclass(frozen=True, slots=True)
class LLMUsage:
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_krw: float
    latency_ms: float


UsageSink = Callable[[LLMUsage], Awaitable[None]]


def estimate_cost_krw(model: str, usage: Any) -> float:
    pin, pout, pcache = PRICES.get(model, PRICES["claude-opus-5-5"])
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    usd = (inp * pin + out * pout + cr * pcache + cw * pin * 1.25) / 1_000_000
    return usd * USD_KRW


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, cfg: LLMConfig, *, api_key: str | None = None, usage_sink: UsageSink | None = None,
                 client: anthropic.AsyncAnthropic | None = None) -> None:
        self.cfg = cfg
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=cfg.max_retries,
                                                         timeout=cfg.timeout_seconds)
        self.usage_sink = usage_sink

    def model_for(self, tier: Tier) -> str:
        return {"large": self.cfg.model_large, "medium": self.cfg.model_medium,
                "small": self.cfg.model_small}[tier]

    def _content(self, user: str, images: Sequence[bytes]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for img in images:
            media = "image/png" if img[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                       "data": base64.standard_b64encode(img).decode()}})
        blocks.append({"type": "text", "text": user})
        return blocks

    def _common(self, model: str, system: str, effort: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 16_000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
        if model in _EFFORT_MODELS:
            kwargs["output_config"] = {"effort": effort or "medium"}
        if self.cfg.server_fallbacks and model in _FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def _record(self, model: str, purpose: str, usage: Any, started: float) -> None:
        if self.usage_sink is None or usage is None:
            return
        await self.usage_sink(LLMUsage(
            model=model, purpose=purpose, input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cost_krw=estimate_cost_krw(model, usage), latency_ms=(time.perf_counter() - started) * 1000))

    @staticmethod
    def _check_stop(resp: Any) -> None:
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal("model declined", category=getattr(details, "category", None))
        if resp.stop_reason == "max_tokens":
            raise LLMTruncated("response truncated at max_tokens")

    async def structured(self, *, tier: Tier, system: str, user: str, schema: type[T],
                         images: Sequence[bytes] = (), effort: str | None = None, purpose: str = "") -> T:
        model = self.model_for(tier)
        started = time.perf_counter()
        try:
            resp = await self.client.beta.messages.parse(
                messages=[{"role": "user", "content": cast(Any, self._content(user, images))}],
                output_format=schema, **self._common(model, system, effort))
        except anthropic.BadRequestError as e:
            raise UpstreamError(f"LLM bad request: {e.message}", status=400, retryable=False) from e
        except anthropic.RateLimitError as e:
            raise UpstreamError("LLM rate limited", status=429, retryable=True) from e
        except anthropic.APIStatusError as e:
            raise UpstreamError(f"LLM error {e.status_code}", status=e.status_code,
                                retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise UpstreamError("LLM connection error", retryable=True) from e
        await self._record(model, purpose, resp.usage, started)
        self._check_stop(resp)
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMTruncated("no parsed output")
        return parsed

    async def text(self, *, tier: Tier, system: str, user: str, effort: str | None = None,
                   purpose: str = "") -> str:
        model = self.model_for(tier)
        started = time.perf_counter()
        try:
            resp = await self.client.beta.messages.create(
                messages=[{"role": "user", "content": user}], **self._common(model, system, effort))
        except anthropic.RateLimitError as e:
            raise UpstreamError("LLM rate limited", status=429, retryable=True) from e
        except anthropic.APIStatusError as e:
            raise UpstreamError(f"LLM error {e.status_code}", status=e.status_code,
                                retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise UpstreamError("LLM connection error", retryable=True) from e
        await self._record(model, purpose, resp.usage, started)
        self._check_stop(resp)
        return "".join(b.text for b in resp.content if b.type == "text")


class ChainLLM:
    """Primary LLM with automatic stand-ins (e.g. Claude → Gemini).

    Only infrastructure failures (:class:`UpstreamError`: outage, rate limit, bad key) move to the next provider; a
    refusal is a judgement about the content and is never "shopped around" to another model.
    """

    def __init__(self, members: Sequence[Any]) -> None:
        if not members:
            raise ValueError("ChainLLM needs at least one member")
        self.members = list(members)
        self.provider = "+".join(getattr(m, "provider", "anthropic") for m in self.members)

    async def structured(self, *, tier: Tier, system: str, user: str, schema: type[T],
                         images: Sequence[bytes] = (), effort: str | None = None, purpose: str = "") -> T:
        last: UpstreamError | None = None
        for m in self.members:
            try:
                out: T = await m.structured(tier=tier, system=system, user=user, schema=schema, images=images,
                                            effort=effort, purpose=purpose)
                return out
            except UpstreamError as exc:
                log.warning("llm_member_failed", provider=getattr(m, "provider", "anthropic"), purpose=purpose,
                            status=exc.status, error=str(exc)[:120])
                last = exc
        assert last is not None
        raise last

    async def text(self, *, tier: Tier, system: str, user: str, effort: str | None = None,
                   purpose: str = "") -> str:
        last: UpstreamError | None = None
        for m in self.members:
            try:
                out: str = await m.text(tier=tier, system=system, user=user, effort=effort, purpose=purpose)
                return out
            except UpstreamError as exc:
                log.warning("llm_member_failed", provider=getattr(m, "provider", "anthropic"), purpose=purpose,
                            status=exc.status, error=str(exc)[:120])
                last = exc
        assert last is not None
        raise last
