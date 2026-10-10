"""Ports: the infrastructure interfaces modules depend on (hexagonal architecture).

Modules import *only* these protocols, `acvm.core`, `acvm.domain`, `acvm.quant`, `acvm.db` and
their own package — never another module (enforced by tests/unit/test_architecture.py). Concrete
adapters live in `acvm.infra` and are wired together in `acvm.context.AppContext`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Tier = Literal["large", "medium", "small"]


class SecretStore(Protocol):
    async def get(self, ref: str) -> str:
        """Resolve a secret reference (Vault path / env name). Raises KeyError if absent."""
        ...

    async def put(self, ref: str, value: str) -> None: ...

    async def delete(self, ref: str) -> bool:
        """Remove a secret; True when something was removed (a value pinned by the environment cannot be)."""
        ...


class ObjectStore(Protocol):
    async def put_file(self, local: Path, key: str, content_type: str) -> str:
        """Upload and return the canonical ``uri`` (s3://bucket/key or file://…)."""
        ...

    async def put_bytes(self, data: bytes, key: str, content_type: str) -> str: ...

    async def get_bytes(self, uri: str) -> bytes: ...

    async def signed_url(self, uri: str, ttl_seconds: int) -> str:
        """Public HTTPS URL valid for ttl (Instagram/TikTok pull media from it, §4 M8)."""
        ...

    async def local_path(self, uri: str) -> Path:
        """Materialise the object on local disk (render inputs, frame verification)."""
        ...


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    used: int
    limit: int
    retry_after_seconds: float = 0.0


class QuotaStore(Protocol):
    """Rolling-window counters + token buckets keyed by arbitrary strings (account×platform, API)."""

    async def acquire_window(self, key: str, limit: int, window_seconds: float, now: datetime,
                             token: str) -> QuotaDecision: ...

    async def release_window(self, key: str, token: str) -> None:
        """Undo an acquisition (publish failed before reaching the platform)."""
        ...

    async def window_usage(self, key: str, window_seconds: float, now: datetime) -> int: ...

    async def take_token(self, key: str, capacity: float, refill_per_second: float,
                         now: datetime) -> QuotaDecision: ...


@dataclass(frozen=True, slots=True)
class PerfRow:
    ts: datetime
    content_id: uuid.UUID
    publication_id: uuid.UUID
    platform: str
    metric: str  # impressions | views | avg_watch_sec | shares | link_clicks | likes | replies
    value: float


@dataclass(frozen=True, slots=True)
class AffiliateReportRow:
    date: date
    sub_id: str
    clicks: int
    orders: int
    gmv_krw: int
    commission_krw: int
    cancelled_krw: int


@dataclass(frozen=True, slots=True)
class ClickRow:
    ts: datetime
    hub_code: str
    sub_id: str
    ip_hash: str
    ua_hash: str
    referer: str = ""


class PerfStore(Protocol):
    """Analytical store (ClickHouse in production, §6.2). ReplacingMergeTree semantics: re-inserting
    the same (publication, metric, ts) or (sub_id, date) replaces rather than double-counts."""

    async def insert_perf(self, rows: Sequence[PerfRow]) -> None: ...

    async def insert_reports(self, rows: Sequence[AffiliateReportRow]) -> None: ...

    async def insert_clicks(self, rows: Sequence[ClickRow]) -> None: ...

    async def latest_metrics(self, publication_id: uuid.UUID) -> dict[str, float]:
        """Latest value per metric (metrics are cumulative counters from platform insights)."""
        ...

    async def report_totals(self, sub_ids: Sequence[str], since: date | None = None) -> dict[str, dict[str, int]]:
        """sub_id → {clicks, orders, gmv_krw, commission_krw, cancelled_krw} summed over dates."""
        ...

    async def metric_population(self, platform: str, metric: str, since: datetime,
                                account_publication_ids: Sequence[uuid.UUID] | None = None) -> list[float]:
        """Latest value of ``metric`` for every publication on ``platform`` since ``since``."""
        ...

    async def clicks_since(self, since: datetime, hub_code: str | None = None) -> list[ClickRow]: ...

    async def daily_money(self, since: date) -> list[dict[str, Any]]:
        """Per-day GMV / commission / cancellations for the money view (§9.3)."""
        ...


class LLM(Protocol):
    async def structured(self, *, tier: Tier, system: str, user: str, schema: type[T],
                         images: Sequence[bytes] = (), effort: str | None = None,
                         purpose: str = "") -> T:
        """Return a schema-valid object. ``system`` must be stable across calls (prompt caching)."""
        ...

    async def text(self, *, tier: Tier, system: str, user: str, effort: str | None = None,
                   purpose: str = "") -> str: ...


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class WordTiming:
    text: str
    start: float
    end: float


@dataclass(slots=True)
class TTSResult:
    wav_path: Path
    words: list[WordTiming]
    duration_s: float
    voice_id: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)


class TTS(Protocol):
    provider: str

    async def synthesize(self, chunks: Sequence[str], out_wav: Path, voice_id: str, *,
                         speed: float = 1.0, directives: Sequence[dict[str, Any]] | None = None) -> TTSResult: ...


@dataclass(frozen=True, slots=True)
class GeneratedMedia:
    """Bytes produced by a generative provider, with the provenance the rights ledger records."""

    data: bytes
    mime: str
    provider: str
    model: str
    prompt: str
    usage: dict[str, Any] = field(default_factory=dict)


class ImageGen(Protocol):
    """Text→image (scene backgrounds only — never a product image, §4 M5)."""

    provider: str
    model: str

    async def generate(self, prompt: str, *, size: str | None = None) -> GeneratedMedia: ...


class VideoGen(Protocol):
    """Image→video (animates generated scene stills only)."""

    provider: str
    model: str

    async def image_to_video(self, image: bytes, mime: str, prompt: str, *, seconds: int | None = None,
                             negative_prompt: str = "", idempotency_key: str = "") -> GeneratedMedia: ...
