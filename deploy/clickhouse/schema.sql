-- ACVM analytical schema (ClickHouse) — design §6.2 성과 이벤트, §9.1 분석 DB.
--
-- Apply:  clickhouse-client --multiquery < deploy/clickhouse/schema.sql
-- The same statements are embedded in acvm.infra.perf_clickhouse (ClickHousePerfStore.ensure_schema);
-- tests/unit/test_perf_clickhouse.py fails if the two drift apart.
--
-- Idempotency (ReplacingMergeTree): re-inserting the same (publication_id, metric, ts) or (sub_id, date)
-- replaces the previous row. `version` is a monotonic ns counter assigned by the writer; reads never rely
-- on merges having happened: perf reads use argMax(value, (ts, version)), report reads use FINAL.
--
-- subId <-> publication mapping (§6: 1:1, publication.link_id -> affiliate_link.sub_id) lives in
-- PostgreSQL. M9 pushes it every attribution run into `publication_link` (ReplacingMergeTree on
-- synced_at, so re-pushes are idempotent). Deployments that prefer a pull model can instead define a
-- ClickHouse DICTIONARY with SOURCE(POSTGRESQL(...)) over the same four columns — credentials for that
-- source must come from a named collection, never from this file.
--
-- click_event keeps hashed IP/UA only (raw IPs are never stored) and expires after 400 days
-- (> the 1-year audit horizon of §10, enough for year-over-year invalid-click baselines).

CREATE DATABASE IF NOT EXISTS acvm;

USE acvm;

CREATE TABLE IF NOT EXISTS perf_event
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
ORDER BY (publication_id, metric, ts);

CREATE TABLE IF NOT EXISTS affiliate_report
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
ORDER BY (sub_id, date);

CREATE TABLE IF NOT EXISTS click_event
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
TTL toDateTime(ts) + INTERVAL 400 DAY;

CREATE TABLE IF NOT EXISTS publication_link
(
    publication_id UUID,
    content_id     UUID,
    sub_id         String,
    platform       LowCardinality(String),
    synced_at      DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(synced_at)
ORDER BY publication_id;

CREATE VIEW IF NOT EXISTS content_roi AS
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
) AS rt ON rt.sub_id = pl.sub_id;
