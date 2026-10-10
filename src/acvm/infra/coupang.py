"""Coupang Partners Open API client (§4 M1 수집원, §4 M3 딥링크, §7 쿠팡 파트너스 row, §9.5 #2, §10).

Endpoints (base ``https://api-gateway.coupang.com``, prefix ``/v2/providers/affiliate_open_api/apis/openapi/v1``):

    GET  /products/search?keyword&limit&subId              상품 검색   (budget: 8 calls/hour, §7)
    GET  /products/bestcategories/{categoryId}?limit&subId 카테고리 베스트
    GET  /products/goldbox?subId                           골드박스
    POST /deeplink  {"coupangUrls": [...], "subId": "..."} 딥링크      (budget: 30 calls/minute)
    GET  /reports/{clicks|orders|cancels|commission}?startDate&endDate&subId&page  (30 calls/hour)

Every response is the envelope ``{"rCode": "0", "rMessage": "", "data": ...}``; a non-"0" rCode is an error.

Authentication is Coupang's CEA HMAC scheme::

    signed-date = UTC yyMMdd 'T' HHmmss 'Z'
    message     = signed-date + METHOD + path + query          (query without the leading '?')
    signature   = hex(HMAC-SHA256(secret_key, message))
    Authorization: CEA algorithm=HmacSHA256, access-key={ak}, signed-date={dt}, signature={sig}

Operating policy, identical for the real client and the sandbox (both share ``_call``):

* **Call budget** (§4 M1 "호출 예산 관리자", §7): token buckets in the shared ``QuotaStore``; an empty
  bucket raises ``QuotaExhausted(retry_after)`` *before* any HTTP request is made.
* **6-hour cache** (§4 M1): GET responses are cached in ``kv_cache`` keyed by endpoint + signed query;
  a fresh hit costs no budget.
* **403 policy** (§9.5 #2 "즉시 호출 중단, 캐시로 운영, 1시간 후 단건 재시도"): the first 403 opens a
  per-endpoint circuit for ``breaker_open_seconds``. Breaker state lives in ``kv_cache`` under
  ``coupang:breaker:<endpoint>`` (row-locked read-modify-write) so every worker sees it. While open,
  calls are served from the cache even when stale; without a cached copy they raise ``CircuitOpen``.
  After the open period exactly one probe is let through (a crashed probe's lease expires after
  ``PROBE_LEASE_SECONDS``); success closes the circuit, another 403 re-opens it.

Secrets are resolved per request from the ``SecretStore`` and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal, Protocol, runtime_checkable
from urllib.parse import quote, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from acvm.config import Settings
from acvm.core.clock import Clock, ensure_utc
from acvm.core.enums import LinkHealth
from acvm.core.errors import CircuitOpen, ConfigError, QuotaExhausted, UpstreamError, ValidationFailed
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.core.metrics import CIRCUIT_STATE, COUPANG_CALLS
from acvm.db import tables as t
from acvm.db.repos import Repos
from acvm.ports import QuotaStore, SecretStore
from acvm.quant.ratelimit import BreakerState, CircuitBreaker

log = get_logger(__name__)

API_PREFIX: Final = "/v2/providers/affiliate_open_api/apis/openapi/v1"
SEARCH_MAX_LIMIT: Final = 10
BEST_MAX_LIMIT: Final = 100
DEEPLINK_MAX_URLS: Final = 20
REPORT_PAGE_SIZE: Final = 1000
REPORT_MAX_PAGES: Final = 20
PROBE_LEASE_SECONDS: Final = 300.0
BREAKER_STATE_TTL: Final = timedelta(days=7)
COUPANG_HOSTS: Final = frozenset({"www.coupang.com", "coupang.com", "m.coupang.com", "link.coupang.com"})

ReportKind = Literal["clicks", "orders", "cancels", "commission"]
REPORT_KINDS: Final[tuple[str, ...]] = ("clicks", "orders", "cancels", "commission")

KvProvider = Callable[[], AbstractAsyncContextManager[Repos]]
"""Zero-arg factory for a unit of work (pass ``ctx.uow``); the client only touches ``kv_cache``."""


# ───────────────────────────── response models ─────────────────────────────


class CoupangProduct(BaseModel):
    """One product item from search / bestcategories / goldbox.

    The Partners API returns no rating or review count — those fields are populated only by the sandbox
    (richer fixture data) or stay ``None`` for M1 to impute from category priors. ``fetched_at`` is when
    the API produced the data (a cache hit keeps the original time), which is what C-03 price freshness
    is judged against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: int
    name: str
    price: int
    image_url: str = ""
    product_url: str = ""
    is_rocket: bool = False
    is_free_shipping: bool = False
    category_name: str = ""
    rank: int | None = None
    keyword: str = ""
    rating: float | None = None
    review_count: int | None = None
    sold_out: bool = False
    discontinued: bool = False
    fetched_at: datetime

    @property
    def canonical_url(self) -> str:
        return canonical_product_url(self.product_id)


class DeeplinkResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    original_url: str
    shorten_url: str
    landing_url: str


@runtime_checkable
class CoupangClient(Protocol):
    """What M1/M3 depend on (``ctx.extras["coupang"]``). Real and sandbox clients both satisfy it."""

    async def search(self, keyword: str, limit: int = 10, *, sub_id: str | None = None) -> list[CoupangProduct]: ...

    async def best_category(self, category_id: str, limit: int = 20, *,
                            sub_id: str | None = None) -> list[CoupangProduct]: ...

    async def goldbox(self, *, sub_id: str | None = None) -> list[CoupangProduct]: ...

    async def deeplink(self, urls: Sequence[str], sub_id: str) -> list[DeeplinkResult]: ...

    async def reports(self, kind: ReportKind, start: date, end: date,
                      sub_id: str | None = None) -> list[dict[str, Any]]: ...

    async def availability(self, product_id: int, name: str) -> LinkHealth: ...

    async def aclose(self) -> None: ...


def canonical_product_url(product_id: int | str) -> str:
    """Untagged product page. Used as deeplink input and as the paused-link redirect (no affiliate tag)."""
    pid = str(product_id).strip()
    if not pid.isdigit():
        raise ValidationFailed("coupang product id must be numeric", product_id=pid)
    return f"https://www.coupang.com/vp/products/{pid}"


def signed_date(now: datetime) -> str:
    """CEA signed-date: UTC ``yyMMdd'T'HHmmss'Z'``."""
    u = ensure_utc(now)
    return u.strftime("%y%m%d") + "T" + u.strftime("%H%M%S") + "Z"


def cea_authorization(access_key: str, secret_key: str, method: str, path: str, query: str,
                      now: datetime) -> str:
    """Authorization header value for one request (message = signed-date + METHOD + path + query)."""
    dt = signed_date(now)
    message = dt + method.upper() + path + query
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={dt}, signature={signature}"


def encode_query(params: Sequence[tuple[str, str]]) -> str:
    """The exact query string that is both signed and sent (RFC 3986 percent-encoding, space → %20)."""
    return urlencode(list(params), quote_via=quote, safe="")


# ───────────────────────────── internals ─────────────────────────────


@dataclass(frozen=True, slots=True)
class _Endpoint:
    name: str  # breaker key + metrics label
    bucket: str  # QuotaStore token-bucket key
    capacity: float
    refill_per_second: float
    cacheable: bool


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    payload: dict[str, Any]
    retry_after: str | None = None


@dataclass(frozen=True, slots=True)
class _Gate:
    allowed: bool
    probe: bool
    retry_after: float


class _BreakerStore:
    """Per-endpoint 403 circuit breakers persisted in ``kv_cache`` (shared by every worker).

    Transitions use ``acvm.quant.ratelimit.CircuitBreaker`` (threshold 1 → the first 403 opens it); each
    read-modify-write runs inside the caller's transaction with ``SELECT … FOR UPDATE`` on PostgreSQL, so
    two workers cannot both take the single half-open probe.
    """

    PREFIX: Final = "coupang:breaker:"

    def __init__(self, open_seconds: float) -> None:
        self.open_seconds = open_seconds

    async def _load(self, repos: Repos, name: str) -> tuple[t.KVCacheRow | None, CircuitBreaker, float]:
        row = await repos.kv.get_for_update(self.PREFIX + name)
        br = CircuitBreaker(failure_threshold=1, open_seconds=self.open_seconds)
        probe_started = 0.0
        if row is not None:
            v = row.value
            br.state = BreakerState(v.get("state", BreakerState.CLOSED.value))
            br.failures = int(v.get("failures", 0))
            br.opened_at = float(v.get("opened_at", 0.0))
            br.probe_in_flight = bool(v.get("probe_in_flight", False))
            probe_started = float(v.get("probe_started_at", 0.0))
        return row, br, probe_started

    async def _save(self, repos: Repos, name: str, row: t.KVCacheRow | None, br: CircuitBreaker,
                    probe_started: float, now: datetime) -> None:
        value = {"state": br.state.value, "failures": br.failures, "opened_at": br.opened_at,
                 "probe_in_flight": br.probe_in_flight, "probe_started_at": probe_started}
        if row is None:
            await repos.kv.put(self.PREFIX + name, value, BREAKER_STATE_TTL, now)
        else:
            row.value = value  # reassign so the JSON column is flagged dirty
            row.expires_at = now + BREAKER_STATE_TTL
            await repos.session.flush()
        CIRCUIT_STATE.labels(name=f"coupang:{name}").set(0 if br.state is BreakerState.CLOSED else 1)

    async def acquire(self, repos: Repos, name: str, now: datetime) -> _Gate:
        row, br, probe_started = await self._load(repos, name)
        if row is None or br.state is BreakerState.CLOSED:
            return _Gate(True, False, 0.0)
        ts = now.timestamp()
        if br.state is BreakerState.HALF_OPEN and br.probe_in_flight and ts - probe_started >= PROBE_LEASE_SECONDS:
            br.probe_in_flight = False  # the previous probe's worker died: its lease has expired
        allowed = br.allow(ts)
        if allowed:
            await self._save(repos, name, row, br, ts, now)
            return _Gate(True, True, 0.0)
        if br.state is BreakerState.HALF_OPEN:
            retry = max(1.0, PROBE_LEASE_SECONDS - (ts - probe_started))
        else:
            retry = max(1.0, br.retry_after(ts))
        return _Gate(False, False, retry)

    async def trip(self, repos: Repos, name: str, now: datetime) -> None:
        row, br, _ = await self._load(repos, name)
        br.record_failure(now.timestamp())
        await self._save(repos, name, row, br, 0.0, now)

    async def close(self, repos: Repos, name: str, now: datetime) -> None:
        row, br, _ = await self._load(repos, name)
        if row is None or br.state is BreakerState.CLOSED:
            return
        br.record_success()
        await self._save(repos, name, row, br, 0.0, now)

    async def release_probe(self, repos: Repos, name: str, now: datetime) -> None:
        """A probe that never reached Coupang (budget, network) must not keep the circuit half-open."""
        row, br, _ = await self._load(repos, name)
        if row is not None and br.state is BreakerState.HALF_OPEN and br.probe_in_flight:
            br.probe_in_flight = False
            await self._save(repos, name, row, br, 0.0, now)


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "y", "yes")
    return bool(v)


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_SUB_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")
_CATEGORY_ID = re.compile(r"^\d{3,6}$")


def _check_sub_id(sub_id: str) -> str:
    if not _SUB_ID.match(sub_id):
        raise ValidationFailed("subId must be 1–32 alphanumeric characters", sub_id=sub_id)
    return sub_id


# ───────────────────────────── the client ─────────────────────────────


class CoupangPartnersClient:
    """Real Coupang Partners Open API client (HTTP + CEA HMAC). See the module docstring for policy.

    Construct with ``CoupangPartnersClient(ctx.settings, ctx.secrets, ctx.quota, ctx.clock, ctx.uow)`` —
    or use ``acvm.infra.coupang_sandbox.coupang_client_for(ctx)``, which also caches the instance in
    ``ctx.extras["coupang"]`` and honours ``settings.coupang.sandbox``.
    """

    def __init__(self, settings: Settings, secrets: SecretStore, quota: QuotaStore, clock: Clock,
                 kv_provider: KvProvider, *, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.cfg = settings.coupang
        self._secrets = secrets
        self._quota = quota
        self._clock = clock
        self._uow = kv_provider
        self._http = http
        self._owns_http = http is None
        self._breakers = _BreakerStore(float(self.cfg.breaker_open_seconds))
        self._cache_ttl = timedelta(seconds=self.cfg.cache_ttl_seconds)
        per_hour = float(self.cfg.search_calls_per_hour)
        self._ep_search = _Endpoint("search", "coupang:search", per_hour, per_hour / 3600.0, True)
        # Category-best and goldbox share a bucket sized like search: the reported 10/hour limit (§4 M1)
        # is documented for search only, so the catalogue endpoints get their own bucket at the same
        # conservative rate instead of eating into the search budget.
        cat = float(self.cfg.catalog_calls_per_hour)
        self._ep_best = _Endpoint("bestcategories", "coupang:catalog", cat, cat / 3600.0, True)
        self._ep_goldbox = _Endpoint("goldbox", "coupang:catalog", cat, cat / 3600.0, True)
        per_min = float(self.cfg.deeplink_calls_per_minute)
        self._ep_deeplink = _Endpoint("deeplink", "coupang:deeplink", per_min, per_min / 60.0, False)
        rep = float(self.cfg.report_calls_per_hour)
        self._ep_reports = _Endpoint("reports", "coupang:reports", rep, rep / 3600.0, True)

    # ── public API ──

    async def search(self, keyword: str, limit: int = 10, *, sub_id: str | None = None) -> list[CoupangProduct]:
        """Keyword search (§4 M1). Costs one token of the 8/hour search budget unless cached."""
        kw = " ".join(keyword.split())
        if not kw:
            raise ValidationFailed("search keyword is empty")
        params = [("keyword", kw), ("limit", str(max(1, min(limit, SEARCH_MAX_LIMIT))))]
        if sub_id:
            params.append(("subId", _check_sub_id(sub_id)))
        data, fetched = await self._call(self._ep_search, "GET", f"{API_PREFIX}/products/search", params)
        items = data.get("productData", []) if isinstance(data, dict) else data
        return self._products(items, fetched)

    async def best_category(self, category_id: str, limit: int = 20, *,
                            sub_id: str | None = None) -> list[CoupangProduct]:
        """Category best-sellers (§4 M1 카테고리 베스트)."""
        cid = str(category_id).strip()
        if not _CATEGORY_ID.match(cid):
            raise ValidationFailed("category id must be numeric", category_id=cid)
        params = [("limit", str(max(1, min(limit, BEST_MAX_LIMIT))))]
        if sub_id:
            params.append(("subId", _check_sub_id(sub_id)))
        data, fetched = await self._call(self._ep_best, "GET", f"{API_PREFIX}/products/bestcategories/{cid}", params)
        return self._products(data, fetched)

    async def goldbox(self, *, sub_id: str | None = None) -> list[CoupangProduct]:
        """Today's goldbox deals (§4 M1 골드박스)."""
        params = [("subId", _check_sub_id(sub_id))] if sub_id else []
        data, fetched = await self._call(self._ep_goldbox, "GET", f"{API_PREFIX}/products/goldbox", params)
        return self._products(data, fetched)

    async def deeplink(self, urls: Sequence[str], sub_id: str) -> list[DeeplinkResult]:
        """Product URL(s) → tracked links carrying ``sub_id`` (§4 M3). Never cached: every publication has
        its own subId, and M3 persists the result immutably (§10)."""
        _check_sub_id(sub_id)
        clean = [u.strip() for u in urls if u and u.strip()]
        if not clean:
            raise ValidationFailed("deeplink needs at least one URL")
        for u in clean:
            parsed = urlparse(u)
            if parsed.scheme != "https" or parsed.hostname not in COUPANG_HOSTS:
                raise ValidationFailed("deeplink accepts coupang.com URLs only", url=u)
        out: list[DeeplinkResult] = []
        for i in range(0, len(clean), DEEPLINK_MAX_URLS):
            chunk = clean[i : i + DEEPLINK_MAX_URLS]
            data, _ = await self._call(self._ep_deeplink, "POST", f"{API_PREFIX}/deeplink", [],
                                       body={"coupangUrls": chunk, "subId": sub_id})
            for item in data or []:
                out.append(DeeplinkResult(original_url=str(item.get("originalUrl", "")),
                                          shorten_url=str(item.get("shortenUrl", "")),
                                          landing_url=str(item.get("landingUrl", ""))))
        return out

    async def reports(self, kind: ReportKind, start: date, end: date,
                      sub_id: str | None = None) -> list[dict[str, Any]]:
        """Daily affiliate reports (§4 M9 input), all pages. Each page costs one report-budget token."""
        if kind not in REPORT_KINDS:
            raise ValidationFailed("unknown report kind", kind=kind)
        if end < start:
            raise ValidationFailed("report end date precedes start date", start=str(start), end=str(end))
        rows: list[dict[str, Any]] = []
        for page in range(REPORT_MAX_PAGES):
            params = [("startDate", start.strftime("%Y%m%d")), ("endDate", end.strftime("%Y%m%d"))]
            if sub_id:
                params.append(("subId", _check_sub_id(sub_id)))
            params.append(("page", str(page)))
            data, _ = await self._call(self._ep_reports, "GET", f"{API_PREFIX}/reports/{kind}", params)
            batch = [dict(r) for r in (data or []) if isinstance(r, dict)]
            rows.extend(batch)
            if len(batch) < REPORT_PAGE_SIZE:
                break
        return rows

    async def availability(self, product_id: int, name: str) -> LinkHealth:
        """Link health for M3's 6-hourly checker (§4 M3 링크 헬스체커).

        The Partners API has no stock endpoint, so the real client spends one *search* call on the
        product's name: listed → ``OK``; not listed → ``UNKNOWN`` (delisting cannot be told apart from
        ranking noise). Budget/circuit errors propagate so the sweep stops instead of guessing, and data
        older than the cache TTL (served during an open circuit) is refused the same way.
        """
        keyword = re.sub(r"[\[\](){}]", " ", name)[:50]
        results = await self.search(keyword, limit=SEARCH_MAX_LIMIT)
        now = self._clock.now()
        if results and now - min(r.fetched_at for r in results) > self._cache_ttl:
            raise CircuitOpen("availability unknown: only stale search data available",
                              retry_after_seconds=float(self.cfg.breaker_open_seconds))
        return LinkHealth.OK if any(r.product_id == int(product_id) for r in results) else LinkHealth.UNKNOWN

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    # ── transport (overridden by the sandbox) ──

    async def _transport(self, method: str, path: str, query: str, body: dict[str, Any] | None) -> RawResponse:
        try:
            access_key = await self._secrets.get(self.cfg.access_key_ref)
            secret_key = await self._secrets.get(self.cfg.secret_key_ref)
        except KeyError as exc:
            raise ConfigError("Coupang Partners credentials are not configured",
                              refs=[self.cfg.access_key_ref, self.cfg.secret_key_ref]) from exc
        url = self.cfg.base_url.rstrip("/") + path + (f"?{query}" if query else "")
        headers = {
            "Authorization": cea_authorization(access_key, secret_key, method, path, query, self._clock.now()),
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }
        content = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        resp = await self._http.request(method, url, headers=headers, content=content)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return RawResponse(resp.status_code, payload if isinstance(payload, dict) else {"data": payload},
                            resp.headers.get("Retry-After"))

    # ── shared policy pipeline ──

    def _cache_key(self, ep: _Endpoint, path: str, query: str) -> str:
        return f"coupang:cache:{ep.name}:{stable_hash({'path': path, 'query': query})}"

    async def _call(self, ep: _Endpoint, method: str, path: str, params: Sequence[tuple[str, str]],
                    body: dict[str, Any] | None = None) -> tuple[Any, datetime]:
        query = encode_query(params)
        cache_key = self._cache_key(ep, path, query) if ep.cacheable else None
        now = self._clock.now()
        async with self._uow() as repos:
            if cache_key is not None:
                hit = await repos.kv.get(cache_key, now)
                if hit is not None:
                    COUPANG_CALLS.labels(endpoint=ep.name, outcome="cache_hit").inc()
                    return hit["data"], datetime.fromisoformat(hit["fetched_at"])
            gate = await self._breakers.acquire(repos, ep.name, now)
        if not gate.allowed:
            return await self._stale_or_raise(ep, cache_key, gate.retry_after, "circuit_open")

        decision = await self._quota.take_token(ep.bucket, ep.capacity, ep.refill_per_second, now)
        if not decision.allowed:
            await self._release_probe(ep, gate, now)
            COUPANG_CALLS.labels(endpoint=ep.name, outcome="budget_exhausted").inc()
            log.warning("coupang_budget_exhausted", endpoint=ep.name, retry_after=decision.retry_after_seconds)
            raise QuotaExhausted(f"coupang {ep.name} call budget exhausted",
                                 retry_after_seconds=decision.retry_after_seconds, endpoint=ep.name)

        try:
            raw = await self._transport(method, path, query, body)
        except httpx.HTTPError as exc:
            await self._release_probe(ep, gate, now)
            COUPANG_CALLS.labels(endpoint=ep.name, outcome="transport_error").inc()
            raise UpstreamError(f"coupang {ep.name} transport error: {type(exc).__name__}",
                                retryable=True, endpoint=ep.name) from exc
        except Exception:
            await self._release_probe(ep, gate, now)
            raise

        if raw.status == 403:
            async with self._uow() as repos:
                await self._breakers.trip(repos, ep.name, now)
            COUPANG_CALLS.labels(endpoint=ep.name, outcome="forbidden").inc()
            log.error("coupang_403_circuit_opened", endpoint=ep.name, open_seconds=self.cfg.breaker_open_seconds)
            return await self._stale_or_raise(ep, cache_key, float(self.cfg.breaker_open_seconds), "forbidden")
        if raw.status == 429:
            await self._release_probe(ep, gate, now)
            COUPANG_CALLS.labels(endpoint=ep.name, outcome="rate_limited").inc()
            retry = _as_float(raw.retry_after) or (1.0 / ep.refill_per_second if ep.refill_per_second else 3600.0)
            raise QuotaExhausted(f"coupang {ep.name} rate limited (429)", retry_after_seconds=retry,
                                 endpoint=ep.name)
        if raw.status >= 400:
            await self._release_probe(ep, gate, now)
            COUPANG_CALLS.labels(endpoint=ep.name, outcome=f"http_{raw.status // 100}xx").inc()
            raise UpstreamError(f"coupang {ep.name} HTTP {raw.status}", status=raw.status,
                                retryable=raw.status >= 500, endpoint=ep.name)

        if gate.probe:  # Coupang answered without 403: the key is usable again
            async with self._uow() as repos:
                await self._breakers.close(repos, ep.name, now)
            log.info("coupang_circuit_closed", endpoint=ep.name)

        rcode = str(raw.payload.get("rCode", ""))
        if rcode != "0":
            COUPANG_CALLS.labels(endpoint=ep.name, outcome="rcode_error").inc()
            raise UpstreamError(f"coupang {ep.name} rCode={rcode or '∅'}: {raw.payload.get('rMessage', '')}",
                                status=raw.status, retryable=False, endpoint=ep.name, r_code=rcode)
        data = raw.payload.get("data")
        if cache_key is not None:
            async with self._uow() as repos:
                await repos.kv.put(cache_key, {"data": data, "fetched_at": now.isoformat()}, self._cache_ttl, now)
        COUPANG_CALLS.labels(endpoint=ep.name, outcome="ok").inc()
        return data, now

    async def _release_probe(self, ep: _Endpoint, gate: _Gate, now: datetime) -> None:
        if gate.probe:
            async with self._uow() as repos:
                await self._breakers.release_probe(repos, ep.name, now)

    async def _stale_or_raise(self, ep: _Endpoint, cache_key: str | None, retry_after: float,
                              reason: str) -> tuple[Any, datetime]:
        if cache_key is not None:
            async with self._uow() as repos:
                stale = await repos.kv.get_stale(cache_key)
            if stale is not None:
                COUPANG_CALLS.labels(endpoint=ep.name, outcome="stale_cache").inc()
                log.info("coupang_serving_stale_cache", endpoint=ep.name, reason=reason,
                         fetched_at=stale["fetched_at"])
                return stale["data"], datetime.fromisoformat(stale["fetched_at"])
        COUPANG_CALLS.labels(endpoint=ep.name, outcome="circuit_open").inc()
        raise CircuitOpen(f"coupang {ep.name} circuit open ({reason}) and nothing cached",
                          retry_after_seconds=retry_after, endpoint=ep.name)

    # ── mapping ──

    @staticmethod
    def _products(items: Any, fetched_at: datetime) -> list[CoupangProduct]:
        out: list[CoupangProduct] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            pid = _as_int(item.get("productId"))
            name = str(item.get("productName") or "").strip()
            if pid is None or not name:
                log.warning("coupang_item_skipped", reason="missing productId/productName")
                continue
            out.append(CoupangProduct(
                product_id=pid, name=name, price=_as_int(item.get("productPrice")) or 0,
                image_url=str(item.get("productImage") or ""), product_url=str(item.get("productUrl") or ""),
                is_rocket=_as_bool(item.get("isRocket")), is_free_shipping=_as_bool(item.get("isFreeShipping")),
                category_name=str(item.get("categoryName") or ""), rank=_as_int(item.get("rank")),
                keyword=str(item.get("keyword") or ""), rating=_as_float(item.get("rating")),
                review_count=_as_int(item.get("reviewCount")), sold_out=_as_bool(item.get("soldOut")),
                discontinued=_as_bool(item.get("discontinued")), fetched_at=fetched_at))
        return out
