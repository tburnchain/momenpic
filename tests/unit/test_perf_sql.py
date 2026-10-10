"""SqlPerfStore must be observationally identical to MemoryPerfStore (ReplacingMergeTree semantics)."""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from acvm.db.session import Database
from acvm.infra.perf_memory import MemoryPerfStore
from acvm.infra.perf_sql import SqlPerfStore, affiliate_report, perf_event, upsert_statement
from acvm.ports import AffiliateReportRow, ClickRow, PerfRow

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
async def sql_store(tmp_path) -> AsyncIterator[SqlPerfStore]:  # type: ignore[no-untyped-def]
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'perf.db'}")
    store = SqlPerfStore(db)
    await store.ensure_schema()
    await store.ensure_schema()  # idempotent
    yield store
    await db.dispose()


def _workload(seed: int = 7) -> tuple[list[list[PerfRow]], list[list[AffiliateReportRow]], list[ClickRow],
                                      list[uuid.UUID], list[str]]:
    rng = random.Random(seed)
    pubs = [uuid.UUID(int=rng.getrandbits(128)) for _ in range(6)]
    contents = {p: uuid.UUID(int=rng.getrandbits(128)) for p in pubs}
    platform = {p: ("threads" if i % 2 == 0 else "instagram") for i, p in enumerate(pubs)}
    perf_batches: list[list[PerfRow]] = []
    for _ in range(4):
        batch = []
        for _ in range(25):
            p = rng.choice(pubs)
            ts = T0 + timedelta(hours=rng.randrange(0, 72))
            metric = rng.choice(["views", "link_clicks", "shares"])
            batch.append(PerfRow(ts, contents[p], p, platform[p], metric, float(rng.randrange(0, 5000))))
        perf_batches.append(batch)
    subs = [f"sub{i:02d}" for i in range(5)]
    report_batches: list[list[AffiliateReportRow]] = []
    for _ in range(3):  # re-collections of overlapping days replace, never add
        batch = [AffiliateReportRow(date(2026, 10, 1) + timedelta(days=rng.randrange(0, 4)), rng.choice(subs),
                                    rng.randrange(0, 50), rng.randrange(0, 5), rng.randrange(0, 200_000),
                                    rng.randrange(0, 6_000), rng.randrange(0, 900)) for _ in range(12)]
        report_batches.append(batch)
    clicks = [ClickRow(T0 + timedelta(minutes=rng.randrange(0, 600)), rng.choice(["h1", "h2"]), rng.choice(subs),
                       f"ip{rng.randrange(0, 4)}", "ua", "") for _ in range(30)]
    return perf_batches, report_batches, clicks, pubs, subs


async def test_parity_with_memory_store(sql_store: SqlPerfStore) -> None:
    mem = MemoryPerfStore()
    perf_batches, report_batches, clicks, pubs, subs = _workload()
    for store in (mem, sql_store):
        for b in perf_batches:
            await store.insert_perf(b)
        for rb in report_batches:
            await store.insert_reports(rb)
        await store.insert_clicks(clicks)

    for p in pubs:
        assert await sql_store.latest_metrics(p) == await mem.latest_metrics(p)
    assert await sql_store.latest_metrics(uuid.uuid4()) == {}
    for since in (None, date(2026, 10, 2), date(2030, 1, 1)):
        assert await sql_store.report_totals([*subs, "missing"], since) == await mem.report_totals(
            [*subs, "missing"], since)
    assert await sql_store.report_totals([]) == {}
    for platform in ("threads", "instagram", "youtube"):
        for metric in ("views", "link_clicks"):
            for since in (T0, T0 + timedelta(hours=36)):
                for ids in (None, pubs[:3], []):
                    got = await sql_store.metric_population(platform, metric, since, ids)
                    want = await mem.metric_population(platform, metric, since, ids)
                    assert sorted(got) == sorted(want)
    for hub in (None, "h1"):
        since = T0 + timedelta(minutes=120)
        assert await sql_store.clicks_since(since, hub) == await mem.clicks_since(since, hub)
    assert await sql_store.daily_money(date(2026, 10, 2)) == await mem.daily_money(date(2026, 10, 2))


async def test_upsert_replaces_instead_of_accumulating(sql_store: SqlPerfStore) -> None:
    pid, cid = uuid.uuid4(), uuid.uuid4()
    ts = T0 + timedelta(hours=5)
    await sql_store.insert_perf([PerfRow(ts, cid, pid, "threads", "views", 100.0)])
    await sql_store.insert_perf([PerfRow(ts, cid, pid, "threads", "views", 250.0),
                                 PerfRow(ts, cid, pid, "threads", "views", 300.0)])  # last in batch wins
    assert await sql_store.latest_metrics(pid) == {"views": 300.0}
    assert await sql_store.metric_population("threads", "views", T0) == [300.0]

    row = AffiliateReportRow(date(2026, 10, 3), "abc", 10, 1, 30_000, 900, 0)
    await sql_store.insert_reports([row])
    await sql_store.insert_reports([AffiliateReportRow(date(2026, 10, 3), "abc", 12, 2, 60_000, 1_800, 900)])
    assert await sql_store.report_totals(["abc"]) == {"abc": {"clicks": 12, "orders": 2, "gmv_krw": 60_000,
                                                              "commission_krw": 1_800, "cancelled_krw": 900}}
    money = await sql_store.daily_money(date(2026, 10, 1))
    assert money == [{"date": "2026-10-03", "gmv_krw": 60_000, "commission_krw": 1_800, "cancelled_krw": 900,
                      "orders": 2, "clicks": 12}]


async def test_clicks_are_append_only_and_timezone_aware(sql_store: SqlPerfStore) -> None:
    c = ClickRow(T0, "h1", "s1", "ip", "ua", "https://threads.net")
    await sql_store.insert_clicks([c, c])
    got = await sql_store.clicks_since(T0 - timedelta(seconds=1))
    assert got == [c, c] and got[0].ts.tzinfo is not None


def test_postgresql_upsert_statement_compiles() -> None:
    stmt = upsert_statement("postgresql", perf_event, ("publication_id", "metric", "ts"))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (publication_id, metric, ts) DO UPDATE SET" in sql
    assert "value = excluded.value" in sql and "metric = excluded" not in sql
    sql2 = str(upsert_statement("postgresql", affiliate_report, ("sub_id", "date")).compile(
        dialect=postgresql.dialect()))
    assert "ON CONFLICT (sub_id, date) DO UPDATE SET" in sql2 and "commission_krw = excluded.commission_krw" in sql2
    assert upsert_statement("mysql", perf_event, ("publication_id",)) is None
