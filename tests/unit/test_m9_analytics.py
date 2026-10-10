"""M9 analytics: insight collection cadence, report folding, attribution events, invalid clicks, dashboards."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from acvm.context import AppContext
from acvm.core.clock import FrozenClock
from acvm.core.enums import ContentKind, HookType, Platform, PublicationState
from acvm.core.events import Event
from acvm.core.ids import publication_key, short_code, sub_id_for
from acvm.core.metrics import ATTRIBUTION_LAG_SECONDS, INVALID_CLICK_ALERTS
from acvm.domain.models import AffiliateLink, ChannelAccount, Content, Product, Publication, Script
from acvm.infra.perf_memory import MemoryPerfStore
from acvm.infra.platforms.base import Insights
from acvm.modules.m9_analytics import AnalyticsService, learning_view, money_view, pipeline_view, risk_view
from acvm.ports import AffiliateReportRow, ClickRow, PerfRow


@pytest.fixture
async def product(ctx: AppContext, product_factory) -> Product:  # type: ignore[no-untyped-def]
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    return p


async def seed_pub(ctx: AppContext, account: ChannelAccount, product: Product, published_at: datetime,
                   post_id: str, *, kind: ContentKind = ContentKind.SHORT
                   ) -> tuple[Content, AffiliateLink, Publication]:
    content = Content(workspace_id=account.workspace_id, product_id=product.id, kind=kind, persona="30대 여성 직장인",
                      template_id="problem-solution-v3", created_at=published_at,
                      script=Script(hooks=["h"], hook_type=HookType.QUESTION, persona="30대 여성 직장인",
                                    template_id="problem-solution-v3"))
    key = publication_key(content.id, account.platform.value, account.id)
    link = AffiliateLink(product_id=product.id, sub_id=sub_id_for(key), deeplink="https://link.coupang.com/re/x",
                         short_url="https://link.coupang.com/a/x", hub_code=short_code(key), signature="sig",
                         publication_key=key, created_at=published_at)
    pub = Publication(content_id=content.id, channel_account_id=account.id, platform=account.platform,
                      link_id=link.id, platform_post_id=post_id, state=PublicationState.PUBLISHED,
                      scheduled_at=published_at, published_at=published_at, idempotency_key=key)
    async with ctx.uow() as r:
        await r.contents.add(content)
        await r.links.add(link)
        await r.publications.add(pub)
    return content, link, pub


class FakePlatform:
    def __init__(self, metrics: dict[str, dict[str, float]], fail: set[str] | None = None) -> None:
        self.metrics, self.fail = metrics, fail or set()
        self.calls: list[tuple[str, str]] = []

    async def insights(self, post_id: str, token: str) -> Insights:
        self.calls.append((post_id, token))
        if post_id in self.fail:
            raise RuntimeError(f"HTTP 500 for ?access_token={token}")
        return Insights(metrics=dict(self.metrics[post_id], note="not-a-number"))  # type: ignore[dict-item]


class FakeCoupang:
    def __init__(self, data: dict[str, list[dict[str, Any]]], fail_kind: str | None = None) -> None:
        self.data, self.fail_kind = data, fail_kind
        self.calls: list[tuple[str, date, date]] = []

    async def reports(self, kind: str, start: date, end: date, sub_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append((kind, start, end))
        if kind == self.fail_kind:
            raise RuntimeError("coupang 503")
        return self.data.get(kind, [])


def _gauge(source: str) -> float:
    return ATTRIBUTION_LAG_SECONDS.labels(source=source)._value.get()  # type: ignore[attr-defined]


async def test_collect_insights_cadence_and_replace(ctx: AppContext, clock: FrozenClock, accounts,  # type: ignore[no-untyped-def]
                                                    product: Product) -> None:
    now = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    clock.set(now)
    th, ig, yt = accounts[Platform.THREADS], accounts[Platform.INSTAGRAM], accounts[Platform.YOUTUBE]
    await ctx.secrets.put(th.token_ref, json.dumps({"access_token": "tok-threads", "expires_in": 3600}))
    await ctx.secrets.put(ig.token_ref, "tok-ig")
    _, _, fresh = await seed_pub(ctx, th, product, now - timedelta(days=1), "p-fresh")
    _, _, mid = await seed_pub(ctx, ig, product, now - timedelta(days=10), "p-mid")
    await seed_pub(ctx, th, product, now - timedelta(days=40), "p-old")
    await seed_pub(ctx, yt, product, now - timedelta(days=1), "p-yt")  # no client for youtube → skipped
    _, _, broken = await seed_pub(ctx, th, product, now - timedelta(hours=5), "p-broken")
    threads = FakePlatform({"p-fresh": {"views": 900, "link_clicks": 9}, "p-old": {"views": 1}}, fail={"p-broken"})
    insta = FakePlatform({"p-mid": {"views": 3000, "shares": 4}})
    ctx.extras["platform_clients"] = {Platform.THREADS: threads, Platform.INSTAGRAM: insta}
    svc = AnalyticsService(ctx)

    assert await svc.collect_insights() == 1  # 01:00 bucket: mid (10 d) waits for a 6-hour bucket, old never
    assert ("p-fresh", "tok-threads") in threads.calls and insta.calls == []
    assert await ctx.perf.latest_metrics(fresh.id) == {"views": 900.0, "link_clicks": 9.0}
    assert await ctx.perf.latest_metrics(broken.id) == {}
    assert _gauge("platform") == 30 * 60

    clock.set(datetime(2026, 10, 8, 6, 5, tzinfo=UTC))
    assert await svc.collect_insights() == 2
    assert insta.calls == [("p-mid", "tok-ig")]
    threads.metrics["p-fresh"] = {"views": 1500, "link_clicks": 20}
    clock.set(datetime(2026, 10, 8, 6, 50, tzinfo=UTC))
    store = ctx.perf
    assert isinstance(store, MemoryPerfStore)
    before = len(store.perf)
    assert await svc.collect_insights() == 2  # same hour bucket ⇒ rows replaced, not appended
    assert len(store.perf) == before
    assert await ctx.perf.latest_metrics(fresh.id) == {"views": 1500.0, "link_clicks": 20.0}
    assert {r.ts for r in store.perf.values() if r.publication_id == mid.id} == {datetime(2026, 10, 8, 6, tzinfo=UTC)}

    ctx.extras.pop("platform_clients")
    assert await svc.collect_insights() == 0


REPORTS = {
    "clicks": [{"date": "20261007", "subId": "s1", "click": 40}, {"date": "20261007", "subId": "s1", "clickCount": 2},
               {"date": "20261007", "subId": "s2", "clicks": 10}, {"date": "20261006", "subId": "s1", "click": 999}],
    "orders": [{"date": "20261007", "subId": "s1", "orderCount": 2, "gmv": "59,800", "commission": 1794},
               {"date": "2026-10-07", "subId": "s2", "orders": 1, "saleAmount": 12900, "commissionAmount": 387}],
    "cancels": [{"date": "20261007", "subId": "s1", "orderCount": 1, "gmv": 29900, "commission": -897}],
    "commission": [{"date": "20261007", "subId": "s1", "commission": 1800}, {"date": "20261007", "commission": 50}],
}


async def test_collect_reports_folds_kinds_and_replaces(ctx: AppContext) -> None:
    svc = AnalyticsService(ctx)
    assert await svc.collect_reports(date(2026, 10, 7)) == 0  # no client wired → skip
    coupang = FakeCoupang(json.loads(json.dumps(REPORTS)))
    ctx.extras["coupang"] = coupang
    assert await svc.collect_reports(date(2026, 10, 7)) == 3
    assert {k for k, _, _ in coupang.calls} == {"clicks", "orders", "cancels", "commission"}
    totals = await ctx.perf.report_totals(["s1", "s2", ""])
    assert totals["s1"] == {"clicks": 42, "orders": 2, "gmv_krw": 59_800, "commission_krw": 1_800, "cancelled_krw": 897}
    assert totals["s2"] == {"clicks": 10, "orders": 1, "gmv_krw": 12_900, "commission_krw": 387, "cancelled_krw": 0}
    assert totals[""]["commission_krw"] == 50  # unattributed revenue still counts in the money view
    assert _gauge("coupang") == 9.5 * 3600  # D 24:00 KST → 09:30 KST next day

    coupang.data["commission"][0]["commission"] = 2000
    assert await svc.collect_reports(date(2026, 10, 7)) == 3
    assert (await ctx.perf.report_totals(["s1"]))["s1"]["commission_krw"] == 2_000  # replaced, not summed

    ctx.extras["coupang"] = FakeCoupang(REPORTS, fail_kind="cancels")
    with pytest.raises(RuntimeError):
        await svc.collect_reports(date(2026, 10, 7))
    assert (await ctx.perf.report_totals(["s1"]))["s1"]["commission_krw"] == 2_000  # no partial overwrite


async def test_attribute_emits_epc_and_percentile(ctx: AppContext, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()
    th = accounts[Platform.THREADS]
    ca, la, pa = await seed_pub(ctx, th, product, now - timedelta(days=2), "a")
    cb, _, pb = await seed_pub(ctx, th, product, now - timedelta(days=2), "b")
    cc, lc, pc = await seed_pub(ctx, th, product, now - timedelta(days=3), "c")
    await seed_pub(ctx, th, product, now - timedelta(days=3), "silent")  # no data at all → no event
    ts = now - timedelta(hours=1)
    await ctx.perf.insert_perf([
        PerfRow(ts, ca.id, pa.id, "threads", "views", 5000), PerfRow(ts, ca.id, pa.id, "threads", "link_clicks", 50),
        PerfRow(ts, cb.id, pb.id, "threads", "views", 500), PerfRow(ts, cb.id, pb.id, "threads", "link_clicks", 8),
        PerfRow(ts, cc.id, pc.id, "threads", "views", 1000), PerfRow(ts, cc.id, pc.id, "threads", "link_clicks", 10)])
    await ctx.perf.insert_reports([AffiliateReportRow(date(2026, 10, 7), la.sub_id, 40, 2, 60_000, 1_800, 0),
                                   AffiliateReportRow(date(2026, 10, 7), lc.sub_id, 0, 1, 30_000, 900, 0)])
    seen: list[Event] = []

    async def handler(ev: Event) -> None:
        seen.append(ev)

    ctx.bus.subscribe("perf.updated", "test", handler)
    svc = AnalyticsService(ctx)
    events = await svc.attribute()
    by_content = {e.content_id: e for e in events}
    assert set(by_content) == {ca.id, cb.id, cc.id}
    assert by_content[ca.id].epc == pytest.approx(45.0)  # 1 800 / 40 report clicks
    assert by_content[cb.id].epc == 0.0
    assert by_content[cc.id].epc == pytest.approx(90.0)  # no report clicks → platform link clicks
    assert by_content[ca.id].percentile == pytest.approx(2.5 / 3)
    assert by_content[cb.id].percentile == pytest.approx(0.5 / 3)
    assert by_content[ca.id].platform is Platform.THREADS

    await svc.attribute()  # same hour ⇒ same idempotency keys
    await ctx.relay_outbox()
    assert len(seen) == 3
    assert {e.idempotency_key for e in seen} == {f"{p.id}:2026100800" for p in (pa, pb, pc)}


async def test_detect_invalid_clicks_burst_and_ctr_spike(ctx: AppContext, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()
    th, ig = accounts[Platform.THREADS], accounts[Platform.INSTAGRAM]
    _, burst_link, _ = await seed_pub(ctx, th, product, now - timedelta(days=1), "x")
    base = now - timedelta(hours=2)
    clicks = [ClickRow(base + timedelta(seconds=8 * i), burst_link.hub_code, burst_link.sub_id, "bot-ip", "ua")
              for i in range(5)]
    clicks += [ClickRow(base + timedelta(minutes=i), burst_link.hub_code, burst_link.sub_id, f"ip{i}", "ua")
               for i in range(6)]
    await ctx.perf.insert_clicks(clicks)

    rows: list[PerfRow] = []
    for i in range(8):
        c, _, p = await seed_pub(ctx, ig, product, now - timedelta(days=3 + i), f"sib{i}")
        rows += [PerfRow(now, c.id, p.id, "instagram", "views", 1000), PerfRow(now, c.id, p.id, "instagram",
                                                                             "link_clicks", 8 + i % 5)]
    c, spike_link, p = await seed_pub(ctx, ig, product, now - timedelta(days=1), "spike")
    rows += [PerfRow(now, c.id, p.id, "instagram", "views", 1000), PerfRow(now, c.id, p.id, "instagram",
                                                                         "link_clicks", 200)]
    await ctx.perf.insert_perf(rows)

    burst_before = INVALID_CLICK_ALERTS.labels(reason="click_burst")._value.get()  # type: ignore[attr-defined]
    svc = AnalyticsService(ctx)
    found = await svc.detect_invalid_clicks()
    by_reason = {e.reason: e for e in found}
    assert set(by_reason) == {"click_burst", "ctr_spike"}
    assert by_reason["click_burst"].link_id == burst_link.id and by_reason["click_burst"].score == 5.0
    assert by_reason["ctr_spike"].link_id == spike_link.id and by_reason["ctr_spike"].score > 4
    assert INVALID_CLICK_ALERTS.labels(reason="click_burst")._value.get() == burst_before + 1  # type: ignore[attr-defined]
    async with ctx.uow() as r:
        audits = await r.audit.recent(action="invalid_click.detected")
        pending = [e for e in await r.outbox.pending() if e.type == "invalid_click.detected"]
    assert {a.target for a in audits} == {burst_link.sub_id, spike_link.sub_id}
    assert all("bot-ip" not in json.dumps(a.payload) for a in audits)  # hashed IP stays in the activity record
    assert len(pending) == 2

    assert await svc.detect_invalid_clicks() == []  # one alert per link per day


async def test_dashboards(ctx: AppContext, workspace, accounts, product: Product) -> None:  # type: ignore[no-untyped-def]
    now = ctx.clock.now()
    th = accounts[Platform.THREADS]
    rows: list[PerfRow] = []
    tail_pub = None
    for i in range(31):
        views = 50_000 if i == 0 else 2_000 + i
        c, _, p = await seed_pub(ctx, th, product, now - timedelta(days=1 + i % 20), f"d{i}")
        rows.append(PerfRow(now, c.id, p.id, "threads", "views", views))
        tail_pub = tail_pub or p
    await ctx.perf.insert_perf(rows)
    await ctx.perf.insert_reports([AffiliateReportRow(date(2026, 10, 8), "s", 100, 3, 90_000, 2_700, 300),
                                   AffiliateReportRow(date(2026, 10, 6), "s", 10, 1, 30_000, 900, 0)])
    async with ctx.uow() as r:
        await r.costs.charge(workspace_id=workspace.id, run_id=None, stage="render", amount_krw=450.0, key="k1",
                             at=now)

    mv = await money_view(ctx, workspace.id, days=3)
    assert [d["date"] for d in mv["daily"]] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    today = mv["daily"][-1]
    assert today["gmv_krw"] == 90_000 and today["commission_krw"] == 2_700 and today["cancelled_krw"] == 300
    assert today["cost_krw"] == 450 and today["net_krw"] == 2_700 - 300 - 450
    assert mv["totals"]["gmv_krw"] == 120_000 and mv["totals"]["net_krw"] == 3_600 - 300 - 450
    channel = next(c for c in mv["channels"] if c["platform"] == "threads")
    assert channel["n"] == 31 and channel["admitted"] is True
    assert channel["breakeven_views"] == pytest.approx(694.4, abs=0.1)
    assert channel["ratio"] == pytest.approx(channel["median_views"] / channel["breakeven_views"], rel=1e-3)
    ig = next(c for c in mv["channels"] if c["platform"] == "instagram")
    assert ig["admitted"] is False and ig["n"] == 0
    json.dumps(mv)  # JSON-able

    pv = await pipeline_view(ctx, workspace.id, days=60)
    assert pv["publications_by_state"] == {"PUBLISHED": 31} and pv["publish_failure_rate"] == 0.0
    rv = await risk_view(ctx, workspace.id)
    assert rv["accounts_by_status"]["active"] == len(Platform)
    lv = await learning_view(ctx, workspace.id)
    assert [t["publication_id"] for t in lv["tail_contents"]] == [str(tail_pub.id)]
    assert lv["arms"] == [] and lv["allocation"]["explore_share"] is None
    json.dumps([pv, rv, lv])
