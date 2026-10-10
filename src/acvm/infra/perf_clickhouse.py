"""ClickHouse PerfStore — the production analytical store of §6.2 / §9.1.

Tables (DDL below is byte-for-byte the content of ``deploy/clickhouse/schema.sql``; a unit test keeps
the two from drifting):

* ``perf_event``        ReplacingMergeTree(version) ORDER BY (publication_id, metric, ts)
* ``affiliate_report``  ReplacingMergeTree(version) ORDER BY (sub_id, date)
* ``click_event``       MergeTree, TTL 400 days (hub redirect log, hashed IPs only)
* ``publication_link``  ReplacingMergeTree(synced_at) ORDER BY publication_id — the subId ↔ publication
                        1:1 map (§6: "수익 귀속은 subId ↔ publication 1:1 매핑으로 보장"). The source of truth
                        is PostgreSQL (``publication.link_id → affiliate_link.sub_id``); M9 pushes the rows
                        it attributes through :meth:`ClickHousePerfStore.upsert_publication_links` every run,
                        so the mapping in ClickHouse is at most one attribution cycle (1 h) stale.
* ``content_roi`` VIEW  publication_link ⟕ latest perf metrics ⟕ affiliate report totals → per-publication
                        views, clicks, orders, GMV, commission, net commission, EPC and RPI.

ReplacingMergeTree only de-duplicates at merge time, so every read must be merge-independent:

* perf reads pick the latest snapshot with ``argMax(value, (ts, version))`` — the tuple makes a
  re-inserted (publication, metric, ts) resolve to the newest insert even before the merge;
* report reads use ``FINAL`` so a re-collected (sub_id, date) is counted once in ``sum``.

``version`` is a monotonic nanosecond counter assigned by this adapter, so "last write wins" holds
both inside one batch and across batches — the exact semantics of `MemoryPerfStore`.

The client is clickhouse-connect: the native async client when its optional ``aiohttp`` dependency is
installed, otherwise the synchronous client driven through ``asyncio.to_thread``. The connection is
opened lazily on first use, so constructing the store (in ``build_context``) never blocks or fails.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from acvm.core.logging import get_logger
from acvm.ports import AffiliateReportRow, ClickRow, PerfRow

log = get_logger(__name__)

# ───────────────────────────── DDL (mirrors deploy/clickhouse/schema.sql) ─────────────────────────────

DDL_PERF_EVENT = """CREATE TABLE IF NOT EXISTS perf_event
(
    ts             DateTime('UTC'),
    content_id     UUID,
    publication_id UUID,
    platform       LowCardinality(String),
    metric         LowCardinality(String),
    value          Float64,
    version        UInt64
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (publication_id, metric, ts)"""

DDL_AFFILIATE_REPORT = """CREATE TABLE IF NOT EXISTS affiliate_report
(
    date           Date,
    sub_id         String,
    clicks         UInt32,
    orders         UInt32,
    gmv_krw        UInt64,
    commission_krw UInt64,
    cancelled_krw  UInt64,
    version        UInt64
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(date)
ORDER BY (sub_id, date)"""

DDL_CLICK_EVENT = """CREATE TABLE IF NOT EXISTS click_event
(
    ts       DateTime64(3, 'UTC'),
    hub_code LowCardinality(String),
    sub_id   String,
    ip_hash  String,
    ua_hash  String,
    referer  String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (hub_code, ts)
TTL toDateTime(ts) + INTERVAL 400 DAY"""

DDL_PUBLICATION_LINK = """CREATE TABLE IF NOT EXISTS publication_link
(
    publication_id UUID,
    content_id     UUID,
    sub_id         String,
    platform       LowCardinality(String),
    synced_at      DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(synced_at)
ORDER BY publication_id"""

DDL_CONTENT_ROI = """CREATE VIEW IF NOT EXISTS content_roi AS
SELECT
    pl.content_id                                                   AS content_id,
    pl.publication_id                                               AS publication_id,
    pl.platform                                                     AS platform,
    pl.sub_id                                                       AS sub_id,
    pm.views                                                        AS views,
    pm.link_clicks                                                  AS link_clicks,
    rt.clicks                                                       AS report_clicks,
    rt.orders                                                       AS orders,
    rt.gmv_krw                                                      AS gmv_krw,
    rt.commission_krw                                               AS commission_krw,
    rt.cancelled_krw                                                AS cancelled_krw,
    toInt64(rt.commission_krw) - toInt64(rt.cancelled_krw)          AS net_commission_krw,
    if(rt.clicks > 0, rt.commission_krw / rt.clicks, 0)             AS epc_krw,
    if(pm.views > 0, rt.commission_krw / pm.views, 0)               AS rpi_krw
FROM (SELECT publication_id, content_id, sub_id, platform FROM publication_link FINAL) AS pl
LEFT JOIN
(
    SELECT
        publication_id,
        maxIf(value, metric = 'views')       AS views,
        maxIf(value, metric = 'link_clicks') AS link_clicks
    FROM
    (
        SELECT publication_id, metric, argMax(value, (ts, version)) AS value
        FROM perf_event
        GROUP BY publication_id, metric
    )
    GROUP BY publication_id
) AS pm ON pm.publication_id = pl.publication_id
LEFT JOIN
(
    SELECT
        sub_id,
        sum(clicks)         AS clicks,
        sum(orders)         AS orders,
        sum(gmv_krw)        AS gmv_krw,
        sum(commission_krw) AS commission_krw,
        sum(cancelled_krw)  AS cancelled_krw
    FROM affiliate_report FINAL
    GROUP BY sub_id
) AS rt ON rt.sub_id = pl.sub_id"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    DDL_PERF_EVENT, DDL_AFFILIATE_REPORT, DDL_CLICK_EVENT, DDL_PUBLICATION_LINK, DDL_CONTENT_ROI,
)

PERF_COLUMNS = ("ts", "content_id", "publication_id", "platform", "metric", "value", "version")
REPORT_COLUMNS = ("date", "sub_id", "clicks", "orders", "gmv_krw", "commission_krw", "cancelled_krw", "version")
CLICK_COLUMNS = ("ts", "hub_code", "sub_id", "ip_hash", "ua_hash", "referer")
PUBLICATION_LINK_COLUMNS = ("publication_id", "content_id", "sub_id", "platform", "synced_at")
_REPORT_FIELDS = ("clicks", "orders", "gmv_krw", "commission_krw", "cancelled_krw")

# ───────────────────────────── query builders (pure — unit-tested without a server) ─────────────────────────────


def _dt_param(dt: datetime, *, millis: bool = False) -> str:
    """UTC wall-clock literal for server-side binding (avoids session-timezone ambiguity)."""
    utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if millis else utc.strftime("%Y-%m-%d %H:%M:%S")


def latest_metrics_query(publication_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    sql = ("SELECT metric, argMax(value, (ts, version)) AS value\n"
           "FROM perf_event\n"
           "WHERE publication_id = {publication_id:UUID}\n"
           "GROUP BY metric")
    return sql, {"publication_id": str(publication_id)}


def report_totals_query(sub_ids: Sequence[str], since: date | None) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"sub_ids": list(sub_ids)}
    where = "sub_id IN {sub_ids:Array(String)}"
    if since is not None:
        where += " AND date >= toDate({since:String})"
        params["since"] = since.isoformat()
    sums = ", ".join(f"sum({f}) AS {f}" for f in _REPORT_FIELDS)
    sql = f"SELECT sub_id, {sums}\nFROM affiliate_report FINAL\nWHERE {where}\nGROUP BY sub_id"
    return sql, params


def metric_population_query(platform: str, metric: str, since: datetime,
                            publication_ids: Sequence[uuid.UUID] | None) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"platform": platform, "metric": metric, "since": _dt_param(since)}
    where = ("platform = {platform:String} AND metric = {metric:String} "
             "AND ts >= toDateTime({since:String}, 'UTC')")
    if publication_ids is not None:
        where += " AND publication_id IN {ids:Array(UUID)}"
        params["ids"] = [str(i) for i in publication_ids]
    sql = ("SELECT publication_id, argMax(value, (ts, version)) AS value\n"
           f"FROM perf_event\nWHERE {where}\nGROUP BY publication_id")
    return sql, params


def clicks_since_query(since: datetime, hub_code: str | None) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"since": _dt_param(since, millis=True)}
    where = "ts >= toDateTime64({since:String}, 3, 'UTC')"
    if hub_code is not None:
        where += " AND hub_code = {hub_code:String}"
        params["hub_code"] = hub_code
    sql = f"SELECT {', '.join(CLICK_COLUMNS)}\nFROM click_event\nWHERE {where}\nORDER BY ts"
    return sql, params


def daily_money_query(since: date) -> tuple[str, dict[str, Any]]:
    sql = ("SELECT date, sum(gmv_krw) AS gmv_krw, sum(commission_krw) AS commission_krw,\n"
           "       sum(cancelled_krw) AS cancelled_krw, sum(orders) AS orders, sum(clicks) AS clicks\n"
           "FROM affiliate_report FINAL\n"
           "WHERE date >= toDate({since:String})\n"
           "GROUP BY date\nORDER BY date")
    return sql, {"since": since.isoformat()}


@dataclass(frozen=True, slots=True)
class PublicationLinkRow:
    """One subId ↔ publication mapping row pushed from PostgreSQL (see module docstring)."""

    publication_id: uuid.UUID
    content_id: uuid.UUID
    sub_id: str
    platform: str


# ───────────────────────────── client adapter ─────────────────────────────


def _rows(result: Any) -> list[Sequence[Any]]:
    rows = getattr(result, "result_rows", result)
    return list(rows or [])


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    raise TypeError(f"expected datetime, got {type(value).__name__}")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class ClickHousePerfStore:
    """`acvm.ports.PerfStore` on ClickHouse (§6.2). ``url`` is a clickhouse-connect DSN, e.g.
    ``clickhouse://acvm:***@clickhouse:8123/acvm`` — credentials stay in the DSN/env, never in logs."""

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self._url = url
        self._client: Any | None = client
        self._lock = asyncio.Lock()
        self._last_version = 0

    # ── plumbing ──
    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                import clickhouse_connect

                try:
                    self._client = await clickhouse_connect.get_async_client(dsn=self._url)
                except ImportError:  # native async client needs aiohttp; fall back to the sync client
                    self._client = await asyncio.to_thread(clickhouse_connect.get_client, dsn=self._url)
                log.info("clickhouse_connected", async_client=inspect.iscoroutinefunction(
                    getattr(self._client, "query", None)))
        return self._client

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = await self._get_client()
        fn = getattr(client, method)
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _query(self, sql: str, params: dict[str, Any]) -> list[Sequence[Any]]:
        return _rows(await self._call("query", sql, parameters=params))

    async def _insert(self, table: str, data: list[list[Any]], columns: Sequence[str]) -> None:
        if data:
            await self._call("insert", table, data, column_names=list(columns))

    def _next_versions(self, n: int) -> list[int]:
        """Strictly increasing ns versions (ReplacingMergeTree keeps the max ⇒ last write wins)."""
        base = max(time.time_ns(), self._last_version + 1)
        self._last_version = base + n - 1
        return list(range(base, base + n))

    async def ensure_schema(self) -> None:
        for stmt in SCHEMA_STATEMENTS:
            await self._call("command", stmt)

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._call("close")
            self._client = None

    # ── writes ──
    async def insert_perf(self, rows: Sequence[PerfRow]) -> None:
        versions = self._next_versions(len(rows))
        data = [[_as_utc(r.ts), r.content_id, r.publication_id, r.platform, r.metric, float(r.value), v]
                for r, v in zip(rows, versions, strict=True)]
        await self._insert("perf_event", data, PERF_COLUMNS)

    async def insert_reports(self, rows: Sequence[AffiliateReportRow]) -> None:
        versions = self._next_versions(len(rows))
        data = [[r.date, r.sub_id, int(r.clicks), int(r.orders), int(r.gmv_krw), int(r.commission_krw),
                 int(r.cancelled_krw), v] for r, v in zip(rows, versions, strict=True)]
        await self._insert("affiliate_report", data, REPORT_COLUMNS)

    async def insert_clicks(self, rows: Sequence[ClickRow]) -> None:
        data = [[_as_utc(r.ts), r.hub_code, r.sub_id, r.ip_hash, r.ua_hash, r.referer] for r in rows]
        await self._insert("click_event", data, CLICK_COLUMNS)

    async def upsert_publication_links(self, rows: Sequence[PublicationLinkRow | Mapping[str, Any]],
                                       at: datetime) -> None:
        """Sync the PostgreSQL subId ↔ publication map used by the ``content_roi`` view.

        Rows are :class:`PublicationLinkRow` or plain mappings with the same four keys (modules call this
        through duck typing and never import infra). Re-pushing is idempotent (ReplacingMergeTree).
        """
        data: list[list[Any]] = []
        for r in rows:
            m = r if isinstance(r, Mapping) else {f: getattr(r, f) for f in PUBLICATION_LINK_COLUMNS[:4]}
            data.append([uuid.UUID(str(m["publication_id"])), uuid.UUID(str(m["content_id"])), str(m["sub_id"]),
                         str(m["platform"]), _as_utc(at)])
        await self._insert("publication_link", data, PUBLICATION_LINK_COLUMNS)

    # ── reads ──
    async def latest_metrics(self, publication_id: uuid.UUID) -> dict[str, float]:
        sql, params = latest_metrics_query(publication_id)
        return {str(m): float(v) for m, v in await self._query(sql, params)}

    async def report_totals(self, sub_ids: Sequence[str], since: date | None = None) -> dict[str, dict[str, int]]:
        wanted = list(dict.fromkeys(sub_ids))
        if not wanted:
            return {}
        sql, params = report_totals_query(wanted, since)
        return {str(row[0]): {f: int(row[i + 1] or 0) for i, f in enumerate(_REPORT_FIELDS)}
                for row in await self._query(sql, params)}

    async def metric_population(self, platform: str, metric: str, since: datetime,
                                account_publication_ids: Sequence[uuid.UUID] | None = None) -> list[float]:
        ids = None if account_publication_ids is None else list(dict.fromkeys(account_publication_ids))
        if ids is not None and not ids:
            return []
        sql, params = metric_population_query(platform, metric, since, ids)
        return [float(row[1]) for row in await self._query(sql, params)]

    async def clicks_since(self, since: datetime, hub_code: str | None = None) -> list[ClickRow]:
        sql, params = clicks_since_query(since, hub_code)
        return [ClickRow(ts=_as_utc(ts), hub_code=str(hc), sub_id=str(sid), ip_hash=str(ip), ua_hash=str(ua),
                         referer=str(ref or "")) for ts, hc, sid, ip, ua, ref in await self._query(sql, params)]

    async def daily_money(self, since: date) -> list[dict[str, Any]]:
        sql, params = daily_money_query(since)
        return [{"date": _as_date(d).isoformat(), "gmv_krw": int(g or 0), "commission_krw": int(c or 0),
                 "cancelled_krw": int(x or 0), "orders": int(o or 0), "clicks": int(k or 0)}
                for d, g, c, x, o, k in await self._query(sql, params)]
