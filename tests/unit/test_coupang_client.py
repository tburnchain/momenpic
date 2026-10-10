"""Real Coupang Partners client against respx-mocked HTTP (§4 M1, §7, §9.5 #2)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.errors import CircuitOpen, QuotaExhausted, UpstreamError, ValidationFailed
from acvm.infra.coupang import API_PREFIX, CoupangPartnersClient, cea_authorization, signed_date

BASE = "https://api-gateway.coupang.com"
SEARCH = f"{API_PREFIX}/products/search"
AUTH_RE = re.compile(r"CEA algorithm=HmacSHA256, access-key=(\S+), signed-date=(\d{6}T\d{6}Z), "
                     r"signature=([0-9a-f]{64})")


def _client(ctx: AppContext) -> CoupangPartnersClient:
    return CoupangPartnersClient(ctx.settings, ctx.secrets, ctx.quota, ctx.clock, ctx.uow,
                                 http=httpx.AsyncClient())


def _item(pid: int, name: str = "과자 밀봉 클립 20개입", price: int = 8_900) -> dict[str, Any]:
    return {"productId": pid, "productName": name, "productPrice": price,
            "productImage": f"https://thumbnail.coupangcdn.com/{pid}.jpg",
            "productUrl": f"https://link.coupang.com/re/AFFSDP?lptag=AF1234567&pageKey={pid}",
            "isRocket": True, "isFreeShipping": False, "categoryName": "주방용품", "rank": 1, "keyword": "클립"}


def _search_ok(*items: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"rCode": "0", "rMessage": "", "data": {
        "landingUrl": "https://link.coupang.com/re/AFFSEARCH?lptag=AF1234567", "productData": list(items)}})


def test_signed_date_format() -> None:
    from datetime import UTC, datetime
    assert signed_date(datetime(2026, 10, 8, 0, 30, 5, tzinfo=UTC)) == "261008T003005Z"


@respx.mock
async def test_search_signs_method_path_query_with_hmac(ctx: AppContext) -> None:
    route = respx.get(f"{BASE}{SEARCH}").mock(return_value=_search_ok(_item(101), _item(102, "밀폐용기")))
    products = await _client(ctx).search("주방  클립", limit=5)

    req = route.calls.last.request
    m = AUTH_RE.fullmatch(req.headers["Authorization"])
    assert m is not None
    access_key, dt, signature = m.groups()
    assert access_key == "test-access"
    assert dt == "261008T003000Z"  # FrozenClock 2026-10-08 00:30:00 UTC
    query = req.url.query.decode()
    assert query == "keyword=%EC%A3%BC%EB%B0%A9%20%ED%81%B4%EB%A6%BD&limit=5"  # whitespace collapsed
    expected = hmac.new(b"test-secret", (dt + "GET" + req.url.path + query).encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert "test-secret" not in req.headers["Authorization"]
    assert [p.product_id for p in products] == [101, 102]
    p = products[0]
    assert p.price == 8_900 and p.is_rocket and p.category_name == "주방용품" and p.rank == 1
    assert p.rating is None and p.review_count is None  # the Partners search API has no ratings
    assert p.fetched_at == ctx.clock.now()
    assert p.canonical_url == "https://www.coupang.com/vp/products/101"


def test_cea_authorization_matches_reference_algorithm() -> None:
    from datetime import UTC, datetime
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    header = cea_authorization("ak", "sk", "post", "/v2/x/deeplink", "", now)
    sig = hmac.new(b"sk", b"260102T030405ZPOST/v2/x/deeplink", hashlib.sha256).hexdigest()
    assert header == f"CEA algorithm=HmacSHA256, access-key=ak, signed-date=260102T030405Z, signature={sig}"


@respx.mock
async def test_deeplink_posts_body_and_maps_results(ctx: AppContext) -> None:
    route = respx.post(f"{BASE}{API_PREFIX}/deeplink").mock(return_value=httpx.Response(200, json={
        "rCode": "0", "rMessage": "", "data": [{
            "originalUrl": "https://www.coupang.com/vp/products/101",
            "shortenUrl": "https://link.coupang.com/a/bXyZ12",
            "landingUrl": "https://link.coupang.com/re/AFFSDP?lptag=AF1234567&subid=abc123&pageKey=101"}]}))
    out = await _client(ctx).deeplink(["https://www.coupang.com/vp/products/101"], "abc123")

    req = route.calls.last.request
    assert json.loads(req.content) == {"coupangUrls": ["https://www.coupang.com/vp/products/101"], "subId": "abc123"}
    dt = AUTH_RE.fullmatch(req.headers["Authorization"]).group(2)  # type: ignore[union-attr]
    expected = hmac.new(b"test-secret", (dt + "POST" + req.url.path).encode(), hashlib.sha256).hexdigest()
    assert req.headers["Authorization"].endswith(f"signature={expected}")
    assert out[0].shorten_url == "https://link.coupang.com/a/bXyZ12"
    assert "subid=abc123" in out[0].landing_url
    with pytest.raises(ValidationFailed):
        await _client(ctx).deeplink(["https://evil.example/vp/products/1"], "abc123")
    with pytest.raises(ValidationFailed):
        await _client(ctx).deeplink(["https://www.coupang.com/vp/products/1"], "bad sub id!")


@respx.mock
async def test_non_zero_rcode_is_a_non_retryable_error_and_not_cached(ctx: AppContext) -> None:
    route = respx.get(f"{BASE}{SEARCH}").mock(return_value=httpx.Response(
        200, json={"rCode": "400", "rMessage": "keyword is required", "data": None}))
    client = _client(ctx)
    with pytest.raises(UpstreamError) as exc_info:
        await client.search("클립")
    assert exc_info.value.retryable is False
    assert "rCode=400" in str(exc_info.value)
    with pytest.raises(UpstreamError):
        await client.search("클립")
    assert route.call_count == 2  # the error was not cached


@respx.mock
async def test_server_errors_are_retryable(ctx: AppContext) -> None:
    respx.get(f"{BASE}{SEARCH}").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(UpstreamError) as exc_info:
        await _client(ctx).search("클립")
    assert exc_info.value.retryable is True and exc_info.value.status == 502


@respx.mock
async def test_search_budget_is_8_per_hour_and_cache_hits_are_free(ctx: AppContext, clock: FrozenClock) -> None:
    route = respx.get(f"{BASE}{SEARCH}").mock(return_value=_search_ok(_item(1)))
    client = _client(ctx)
    for i in range(8):
        await client.search(f"키워드{i}")
    assert route.call_count == 8
    await client.search("키워드0")  # 6-hour cache hit: no HTTP, no budget
    assert route.call_count == 8
    with pytest.raises(QuotaExhausted) as exc_info:
        await client.search("새 키워드")
    assert route.call_count == 8  # refused before any request
    assert exc_info.value.retry_after_seconds == pytest.approx(3600 / 8, rel=1e-6)
    clock.advance(seconds=450)  # one token refilled
    await client.search("새 키워드")
    assert route.call_count == 9


@respx.mock
async def test_403_opens_breaker_serves_stale_cache_then_single_probe(ctx: AppContext, clock: FrozenClock) -> None:
    responses: list[httpx.Response] = [_search_ok(_item(7, price=9_900))]
    route = respx.get(f"{BASE}{SEARCH}").mock(side_effect=lambda req: responses[-1])
    client = _client(ctx)
    first = await client.search("밀봉 클립")
    assert route.call_count == 1

    clock.advance(hours=7)  # cache now stale (6 h TTL)
    responses.append(httpx.Response(403, json={"rCode": "403", "rMessage": "Forbidden"}))
    stale = await client.search("밀봉 클립")  # 403 → stop, operate from cache
    assert route.call_count == 2
    assert [p.product_id for p in stale] == [7] and stale[0].fetched_at == first[0].fetched_at

    again = await client.search("밀봉 클립")  # circuit open: served from cache, no HTTP at all
    assert route.call_count == 2 and again == stale
    with pytest.raises(CircuitOpen) as exc_info:  # nothing cached for this keyword
        await client.search("다른 키워드")
    assert route.call_count == 2
    assert 0 < exc_info.value.retry_after_seconds <= 3600
    async with ctx.uow() as repos:
        state = await repos.kv.get_stale("coupang:breaker:search")
    assert state is not None and state["state"] == "open"

    # other endpoints have their own breaker
    respx.get(f"{BASE}{API_PREFIX}/products/goldbox").mock(
        return_value=httpx.Response(200, json={"rCode": "0", "rMessage": "", "data": [_item(9)]}))
    assert [p.product_id for p in await client.goldbox()] == [9]

    clock.advance(minutes=59)
    with pytest.raises(CircuitOpen):
        await client.search("다른 키워드")
    assert route.call_count == 2

    clock.advance(minutes=1, seconds=1)  # 1 h elapsed → exactly one probe
    responses.append(_search_ok(_item(8)))
    probed = await client.search("다른 키워드")
    assert route.call_count == 3 and [p.product_id for p in probed] == [8]
    async with ctx.uow() as repos:
        state = await repos.kv.get_stale("coupang:breaker:search")
    assert state is not None and state["state"] == "closed"
    await client.search("세 번째")
    assert route.call_count == 4


@respx.mock
async def test_failed_probe_reopens_for_another_hour(ctx: AppContext, clock: FrozenClock) -> None:
    route = respx.get(f"{BASE}{SEARCH}").mock(return_value=httpx.Response(403))
    client = _client(ctx)
    with pytest.raises(CircuitOpen):
        await client.search("클립")
    clock.advance(hours=1, seconds=1)
    with pytest.raises(CircuitOpen):
        await client.search("클립")  # the single probe also gets 403
    assert route.call_count == 2
    clock.advance(minutes=30)
    with pytest.raises(CircuitOpen):
        await client.search("클립")
    assert route.call_count == 2  # no consecutive 403s (§9.5 #2)


@respx.mock
async def test_budget_refusal_releases_half_open_probe(ctx: AppContext, clock: FrozenClock) -> None:
    route = respx.get(f"{BASE}{SEARCH}").mock(return_value=httpx.Response(403))
    client = _client(ctx)
    with pytest.raises(CircuitOpen):
        await client.search("클립")
    clock.advance(hours=1, seconds=1)
    while (await ctx.quota.take_token("coupang:search", 8, 8 / 3600, clock.now())).allowed:
        pass  # another consumer spent the whole (refilled) search budget
    with pytest.raises(QuotaExhausted):
        await client.search("클립")
    async with ctx.uow() as repos:
        state = await repos.kv.get_stale("coupang:breaker:search")
    assert state is not None and state["state"] == "half_open" and state["probe_in_flight"] is False
    assert route.call_count == 1


@respx.mock
async def test_reports_paginate_and_pass_sub_id(ctx: AppContext) -> None:
    page0 = [{"date": "20261007", "subId": "abc", "click": 1} for _ in range(1000)]
    page1 = [{"date": "20261008", "subId": "abc", "click": 2} for _ in range(5)]
    route = respx.get(f"{BASE}{API_PREFIX}/reports/clicks").mock(side_effect=[
        httpx.Response(200, json={"rCode": "0", "rMessage": "", "data": page0}),
        httpx.Response(200, json={"rCode": "0", "rMessage": "", "data": page1})])
    rows = await _client(ctx).reports("clicks", date(2026, 10, 1), date(2026, 10, 8), sub_id="abc")
    assert len(rows) == 1005
    queries = [c.request.url.query.decode() for c in route.calls]
    assert queries == ["startDate=20261001&endDate=20261008&subId=abc&page=0",
                       "startDate=20261001&endDate=20261008&subId=abc&page=1"]
    with pytest.raises(ValidationFailed):
        await _client(ctx).reports("clicks", date(2026, 10, 8), date(2026, 10, 1))
