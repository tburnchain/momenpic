"""Sandbox client: deterministic, same interface and operating policy as the real client."""

from __future__ import annotations

import re
from datetime import date

import pytest

from acvm.context import AppContext
from acvm.core.enums import LinkHealth
from acvm.core.errors import QuotaExhausted
from acvm.infra.coupang import CoupangClient, CoupangPartnersClient
from acvm.infra.coupang_catalog import is_regulated
from acvm.infra.coupang_sandbox import (
    CATALOG,
    SANDBOX_PARTNER_ID,
    SandboxCoupangClient,
    build_coupang_client,
    coupang_client_for,
)


def _sandbox(ctx: AppContext) -> SandboxCoupangClient:
    return SandboxCoupangClient(ctx.settings, ctx.secrets, ctx.quota, ctx.clock, ctx.uow)


def test_catalogue_covers_the_first_three_niches_and_edge_cases() -> None:
    for cat in ("1013", "1018", "1014"):
        assert sum(1 for it in CATALOG if it.category_id == cat) >= 12
    assert sum(1 for it in CATALOG if it.category_id == "1024") >= 3
    assert any(it.sold_out for it in CATALOG) and any(it.discontinued for it in CATALOG)
    assert len({it.product_id for it in CATALOG}) == len(CATALOG)
    assert any(is_regulated(it.name, it.category_id) and it.category_id != "1024" for it in CATALOG)


async def test_sandbox_is_deterministic_across_instances(ctx: AppContext, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from acvm.context import build_test_context

    other_ctx = await build_test_context(clock=ctx.clock, tmp_dir=str(tmp_path / "other"))
    try:
        a, b = _sandbox(ctx), _sandbox(other_ctx)
        assert await a.best_category("1013", limit=10) == await b.best_category("1013", limit=10)
        assert await a.search("밀봉 클립") == await b.search("밀봉 클립")
        assert await a.goldbox() == await b.goldbox()
        url = "https://www.coupang.com/vp/products/7101300001"
        assert await a.deeplink([url], "a1b2c3") == await b.deeplink([url], "a1b2c3")
        assert (await a.reports("orders", date(2026, 9, 1), date(2026, 9, 30), sub_id="a1b2c3")
                == await b.reports("orders", date(2026, 9, 1), date(2026, 9, 30), sub_id="a1b2c3"))
    finally:
        await other_ctx.db.dispose()


async def test_best_category_lists_in_rank_order_and_hides_discontinued(ctx: AppContext) -> None:
    best = await _sandbox(ctx).best_category("1013", limit=100)
    assert [p.rank for p in best] == list(range(1, len(best) + 1))
    ids = {p.product_id for p in best}
    assert 7101300016 not in ids  # discontinued → delisted
    sold_out = next(p for p in best if p.product_id == 7101300015)
    assert sold_out.sold_out  # sold out → still listed, flagged
    assert all(p.rating is not None and p.review_count is not None for p in best)
    assert all(p.category_name == "주방용품" for p in best)


async def test_search_matches_keywords(ctx: AppContext) -> None:
    found = await _sandbox(ctx).search("밀봉 클립", limit=10)
    assert found[0].product_id in (7101300001, 7101300013)
    assert all("클립" in p.name or "밀봉" in p.name for p in found)


async def test_deeplink_format_carries_partner_tag_and_sub_id(ctx: AppContext) -> None:
    client = _sandbox(ctx)
    [res] = await client.deeplink(["https://www.coupang.com/vp/products/7101300001"], "a1b2c3d4")
    assert re.fullmatch(r"https://link\.coupang\.com/a/[a-z2-7]{10}", res.shorten_url)
    assert res.landing_url == ("https://link.coupang.com/re/AFFSDP?lptag=AF0000000&subid=a1b2c3d4"
                               "&pageKey=7101300001")
    ctx.settings.coupang.partner_id = "AF7654321"
    [tagged] = await client.deeplink(["https://www.coupang.com/vp/products/7101300001"], "a1b2c3d4")
    assert "lptag=AF7654321" in tagged.landing_url and SANDBOX_PARTNER_ID not in tagged.landing_url


async def test_reports_are_deterministic_per_sub_id(ctx: AppContext) -> None:
    client = _sandbox(ctx)
    clicks = await client.reports("clicks", date(2026, 9, 1), date(2026, 9, 30), sub_id="s1")
    again = await client.reports("clicks", date(2026, 9, 1), date(2026, 9, 30), sub_id="s1")
    other = await client.reports("clicks", date(2026, 9, 1), date(2026, 9, 30), sub_id="s2")
    assert clicks == again and clicks != other
    assert all(r["subId"] == "s1" and r["click"] > 0 for r in clicks)
    commission = await client.reports("commission", date(2026, 9, 1), date(2026, 9, 30), sub_id="s1")
    assert sum(r["click"] for r in commission) == sum(r["click"] for r in clicks)


async def test_availability_flags_and_override(ctx: AppContext) -> None:
    client = _sandbox(ctx)
    assert await client.availability(7101300001, "") is LinkHealth.OK
    assert await client.availability(7101300015, "") is LinkHealth.SOLD_OUT
    assert await client.availability(7101300016, "") is LinkHealth.DISCONTINUED
    assert await client.availability(1, "") is LinkHealth.UNKNOWN
    client.set_availability(7101300001, LinkHealth.SOLD_OUT)
    assert await client.availability(7101300001, "") is LinkHealth.SOLD_OUT


async def test_sandbox_honours_the_same_call_budget(ctx: AppContext) -> None:
    client = _sandbox(ctx)
    for i in range(8):
        await client.search(f"클립{i}")
    with pytest.raises(QuotaExhausted):
        await client.search("한 번 더")
    assert client.transport_calls["search"] == 8


async def test_factory_honours_sandbox_flag_and_caches_in_extras(ctx: AppContext) -> None:
    assert isinstance(build_coupang_client(ctx), SandboxCoupangClient)
    ctx.settings.coupang.sandbox = False
    real = build_coupang_client(ctx)
    assert type(real) is CoupangPartnersClient
    ctx.settings.coupang.sandbox = True
    first = coupang_client_for(ctx)
    assert ctx.extras["coupang"] is first and coupang_client_for(ctx) is first
    assert isinstance(first, CoupangClient)
