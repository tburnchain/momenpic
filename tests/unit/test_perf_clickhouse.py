"""ClickHousePerfStore: DDL ↔ deploy/clickhouse/schema.sql parity and SQL generation (no server)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from acvm.infra import perf_clickhouse as ch
from acvm.infra.perf_clickhouse import ClickHousePerfStore
from acvm.ports import AffiliateReportRow, ClickRow, PerfRow

SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "clickhouse" / "schema.sql"


def _norm(sql: str) -> str:
    sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return re.sub(r"\s+", " ", sql).strip()


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClient:
    """Sync clickhouse-connect stand-in: records calls, answers queries from a routing table."""

    def __init__(self, answers: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self.answers = answers or {}
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[list[Any]], list[str]]] = []
        self.commands: list[str] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        self.queries.append((sql, parameters or {}))
        for marker, rows in self.answers.items():
            if marker in sql:
                return _Result(rows)
        return _Result([])

    def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:
        self.inserts.append((table, data, column_names))

    def command(self, sql: str) -> None:
        self.commands.append(sql)


class FakeAsyncClient(FakeClient):
    async def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:  # type: ignore[override]
        return FakeClient.query(self, sql, parameters)

    async def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:  # type: ignore[override]
        FakeClient.insert(self, table, data, column_names)

    async def command(self, sql: str) -> None:  # type: ignore[override]
        FakeClient.command(self, sql)


def test_schema_file_matches_embedded_ddl() -> None:
    text = "\n".join(line for line in SCHEMA.read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("--"))
    statements = [_norm(s) for s in text.split(";") if _norm(s)]
    assert statements[:2] == ["CREATE DATABASE IF NOT EXISTS acvm", "USE acvm"]
    assert statements[2:] == [_norm(s) for s in ch.SCHEMA_STATEMENTS]


def test_ddl_follows_design_6_2() -> None:
    pe, ar, ce, pl, roi = (_norm(s) for s in ch.SCHEMA_STATEMENTS)
    assert "ENGINE = ReplacingMergeTree(version)" in pe and "ORDER BY (publication_id, metric, ts)" in pe
    assert "metric LowCardinality(String)" in pe and "platform LowCardinality(String)" in pe
    assert "ENGINE = ReplacingMergeTree(version)" in ar and "ORDER BY (sub_id, date)" in ar
    for col in ("clicks UInt32", "orders UInt32", "gmv_krw UInt64", "commission_krw UInt64", "cancelled_krw UInt64"):
        assert col in ar
    assert "ENGINE = MergeTree" in ce and "TTL toDateTime(ts) + INTERVAL 400 DAY" in ce
    assert "publication_id UUID, content_id UUID, sub_id String, platform LowCardinality(String)" in pl
    assert "CREATE VIEW IF NOT EXISTS content_roi" in roi and "affiliate_report FINAL" in roi
    assert "argMax(value, (ts, version))" in roi and "rt.sub_id = pl.sub_id" in roi


def test_query_builders_respect_replacing_merge_tree() -> None:
    pid = uuid.uuid4()
    sql, params = ch.latest_metrics_query(pid)
    assert "argMax(value, (ts, version))" in sql and "{publication_id:UUID}" in sql
    assert params == {"publication_id": str(pid)}

    sql, params = ch.report_totals_query(["a", "b"], date(2026, 10, 1))
    assert "FROM affiliate_report FINAL" in sql and "sub_id IN {sub_ids:Array(String)}" in sql
    assert "date >= toDate({since:String})" in sql and params == {"sub_ids": ["a", "b"], "since": "2026-10-01"}
    sql, params = ch.report_totals_query(["a"], None)
    assert "since" not in params and "date >=" not in sql

    since = datetime(2026, 10, 8, 9, 30, tzinfo=ch.UTC).astimezone()  # any tz in → UTC literal out
    sql, params = ch.metric_population_query("threads", "views", since, [pid])
    assert "argMax(value, (ts, version))" in sql and "GROUP BY publication_id" in sql
    assert "publication_id IN {ids:Array(UUID)}" in sql and params["ids"] == [str(pid)]
    assert params["since"] == "2026-10-08 09:30:00" and "toDateTime({since:String}, 'UTC')" in sql
    sql, params = ch.metric_population_query("threads", "views", since, None)
    assert "ids" not in params and "IN {ids" not in sql

    sql, params = ch.clicks_since_query(since, "h1")
    assert "toDateTime64({since:String}, 3, 'UTC')" in sql and params["since"] == "2026-10-08 09:30:00.000"
    assert params["hub_code"] == "h1" and sql.endswith("ORDER BY ts")

    sql, params = ch.daily_money_query(date(2026, 9, 1))
    assert "FROM affiliate_report FINAL" in sql and "GROUP BY date" in sql and params == {"since": "2026-09-01"}


async def test_store_reads_and_writes_through_sync_client() -> None:
    pid, cid = uuid.uuid4(), uuid.uuid4()
    ts = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
    client = FakeClient({
        "GROUP BY metric": [("views", 1200.0), ("link_clicks", 14.0)],
        "GROUP BY sub_id": [("s1", 10, 1, 30_000, 900, 0)],
        "GROUP BY publication_id": [(pid, 1200.0), (uuid.uuid4(), 80.0)],
        "FROM click_event": [(ts.replace(tzinfo=None), "h1", "s1", "ip", "ua", None)],
        "GROUP BY date": [(date(2026, 10, 7), 30_000, 900, 0, 1, 10)],
    })
    store = ClickHousePerfStore("clickhouse://user:pw@localhost:8123/acvm", client=client)

    await store.insert_perf([PerfRow(ts, cid, pid, "threads", "views", 10.0),
                             PerfRow(ts, cid, pid, "threads", "views", 12.0)])
    table, data, cols = client.inserts[-1]
    assert table == "perf_event" and cols == list(ch.PERF_COLUMNS)
    assert data[0][-1] < data[1][-1]  # strictly increasing versions ⇒ last write wins
    await store.insert_perf([PerfRow(ts, cid, pid, "threads", "views", 13.0)])
    assert client.inserts[-1][1][0][-1] > data[1][-1]  # across batches too
    await store.insert_reports([AffiliateReportRow(date(2026, 10, 7), "s1", 10, 1, 30_000, 900, 0)])
    assert client.inserts[-1][0] == "affiliate_report" and client.inserts[-1][2] == list(ch.REPORT_COLUMNS)
    await store.insert_clicks([ClickRow(ts, "h1", "s1", "ip", "ua")])
    assert client.inserts[-1][0] == "click_event"
    await store.insert_perf([])  # no-op, no round trip
    n_inserts = len(client.inserts)
    await store.upsert_publication_links([{"publication_id": pid, "content_id": cid, "sub_id": "s1",
                                           "platform": "threads"}], ts)
    assert len(client.inserts) == n_inserts + 1 and client.inserts[-1][0] == "publication_link"

    assert await store.latest_metrics(pid) == {"views": 1200.0, "link_clicks": 14.0}
    assert await store.report_totals(["s1", "s1"]) == {"s1": {"clicks": 10, "orders": 1, "gmv_krw": 30_000,
                                                              "commission_krw": 900, "cancelled_krw": 0}}
    assert client.queries[-1][1]["sub_ids"] == ["s1"]
    assert await store.report_totals([]) == {}
    assert sorted(await store.metric_population("threads", "views", ts, [pid])) == [80.0, 1200.0]
    n_queries = len(client.queries)
    assert await store.metric_population("threads", "views", ts, []) == []
    assert len(client.queries) == n_queries
    clicks = await store.clicks_since(ts)
    assert clicks == [ClickRow(ts, "h1", "s1", "ip", "ua", "")] and clicks[0].ts.tzinfo is not None
    assert await store.daily_money(date(2026, 10, 1)) == [{"date": "2026-10-07", "gmv_krw": 30_000,
                                                          "commission_krw": 900, "cancelled_krw": 0,
                                                          "orders": 1, "clicks": 10}]
    await store.ensure_schema()
    assert client.commands == list(ch.SCHEMA_STATEMENTS)


async def test_store_uses_native_async_client_when_available() -> None:
    client = FakeAsyncClient({"GROUP BY metric": [("views", 5.0)]})
    store = ClickHousePerfStore("clickhouse://localhost/acvm", client=client)
    assert await store.latest_metrics(uuid.uuid4()) == {"views": 5.0}
    await store.ensure_schema()
    assert len(client.commands) == len(ch.SCHEMA_STATEMENTS)


def test_constructor_is_lazy() -> None:
    store = ClickHousePerfStore("clickhouse://nowhere.invalid:8123/acvm")  # must not connect
    assert store._client is None
