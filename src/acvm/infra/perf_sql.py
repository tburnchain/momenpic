"""SQL-backed PerfStore (SQLite dev / PostgreSQL single-node) with ReplacingMergeTree semantics.

§6 keeps time-series performance events in ClickHouse; this adapter is the drop-in for the phase-1
single-server deployment (§9: one Contabo box, Docker Compose) and for any environment without
ClickHouse. It reproduces `acvm.infra.perf_memory.MemoryPerfStore` *exactly*:

* ``perf_event``       PRIMARY KEY (publication_id, metric, ts) — re-inserting a key **replaces** the
                       value (upsert), mirroring ``ReplacingMergeTree ORDER BY (publication_id, metric, ts)``.
* ``affiliate_report`` PRIMARY KEY (sub_id, date) — a re-collected day replaces the previous row, so a
                       D+1 re-run never double-counts commission (§6.2, §9.2 attribution freshness).
* ``click_event``      append-only hub redirect log (hashed IPs only; raw IPs are never stored, §4 M9).

Upserts are dialect-native (``INSERT … ON CONFLICT DO UPDATE`` on SQLite ≥ 3.24 and PostgreSQL);
other dialects fall back to delete-then-insert inside one transaction. Within one batch the last row
for a key wins, exactly like the in-memory dict assignment.

The tables live in their own ``MetaData`` so the analytical schema never leaks into the
transactional ORM (`acvm.db.tables.Base`) and can be dropped/moved to ClickHouse independently.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    and_,
    delete,
    func,
    insert,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from acvm.db.session import Database
from acvm.db.types import UTCDateTime
from acvm.ports import AffiliateReportRow, ClickRow, PerfRow

perf_metadata = MetaData()

perf_event = Table(
    "perf_event",
    perf_metadata,
    Column("publication_id", Uuid, primary_key=True),
    Column("metric", String(32), primary_key=True),
    Column("ts", UTCDateTime, primary_key=True),
    Column("content_id", Uuid, nullable=False),
    Column("platform", String(16), nullable=False),
    Column("value", Float, nullable=False),
    Index("ix_perf_event_platform_metric_ts", "platform", "metric", "ts"),
)

affiliate_report = Table(
    "affiliate_report",
    perf_metadata,
    Column("sub_id", String(64), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("clicks", Integer, nullable=False, default=0),
    Column("orders", Integer, nullable=False, default=0),
    Column("gmv_krw", BigInteger, nullable=False, default=0),
    Column("commission_krw", BigInteger, nullable=False, default=0),
    Column("cancelled_krw", BigInteger, nullable=False, default=0),
    Index("ix_affiliate_report_date", "date"),
)

click_event = Table(
    "click_event",
    perf_metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("ts", UTCDateTime, nullable=False),
    Column("hub_code", String(32), nullable=False),
    Column("sub_id", String(64), nullable=False),
    Column("ip_hash", String(128), nullable=False),
    Column("ua_hash", String(128), nullable=False),
    Column("referer", Text, nullable=False, default=""),
    Index("ix_click_event_ts", "ts"),
    Index("ix_click_event_hub_ts", "hub_code", "ts"),
)

_REPORT_FIELDS = ("clicks", "orders", "gmv_krw", "commission_krw", "cancelled_krw")
_IN_CHUNK = 500  # keep IN-lists well under SQLite's bound-parameter limit


def _chunks[T](items: Sequence[T], size: int = _IN_CHUNK) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _last_wins(rows: Iterable[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
    """Collapse duplicate keys inside one batch (PostgreSQL rejects ON CONFLICT touching a row twice)."""
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        out[tuple(r[k] for k in key)] = r
    return list(out.values())


def upsert_statement(dialect: str, table: Table, key: tuple[str, ...]) -> Any | None:
    """``INSERT … ON CONFLICT (key) DO UPDATE SET <non-key> = excluded.<non-key>`` for SQLite/PostgreSQL;
    ``None`` for dialects without native upsert (caller falls back to delete + insert)."""
    non_key = [c.name for c in table.columns if c.name not in key]
    if dialect == "sqlite":
        from sqlalchemy.dialects import sqlite

        lite = sqlite.insert(table)
        return lite.on_conflict_do_update(index_elements=list(key),
                                          set_={name: lite.excluded[name] for name in non_key})
    if dialect == "postgresql":
        from sqlalchemy.dialects import postgresql

        pg = postgresql.insert(table)
        return pg.on_conflict_do_update(index_elements=list(key), set_={name: pg.excluded[name] for name in non_key})
    return None


class SqlPerfStore:
    """`acvm.ports.PerfStore` on the transactional database (SQLite via aiosqlite, or PostgreSQL)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure_schema(self) -> None:
        """Create the three analytical tables if missing (idempotent)."""
        async with self.db.engine.begin() as conn:
            await conn.run_sync(perf_metadata.create_all)

    # ───────────── writes ─────────────

    async def _upsert(self, conn: AsyncConnection, table: Table, rows: list[dict[str, Any]],
                      key: tuple[str, ...]) -> None:
        if not rows:
            return
        stmt = upsert_statement(conn.dialect.name, table, key)
        if stmt is not None:
            await conn.execute(stmt, rows)
            return
        # Portable fallback: delete the keys, then insert (same transaction ⇒ atomic replace).
        cols = [table.c[k] for k in key]
        keys = [tuple(r[k] for k in key) for r in rows]
        for chunk in _chunks(keys):
            await conn.execute(delete(table).where(tuple_(*cols).in_(list(chunk))))
        await conn.execute(insert(table), rows)

    async def insert_perf(self, rows: Sequence[PerfRow]) -> None:
        data = _last_wins(({"publication_id": r.publication_id, "metric": r.metric, "ts": r.ts,
                            "content_id": r.content_id, "platform": r.platform, "value": float(r.value)}
                           for r in rows), ("publication_id", "metric", "ts"))
        async with self.db.engine.begin() as conn:
            await self._upsert(conn, perf_event, data, ("publication_id", "metric", "ts"))

    async def insert_reports(self, rows: Sequence[AffiliateReportRow]) -> None:
        data = _last_wins(({"sub_id": r.sub_id, "date": r.date, "clicks": int(r.clicks), "orders": int(r.orders),
                            "gmv_krw": int(r.gmv_krw), "commission_krw": int(r.commission_krw),
                            "cancelled_krw": int(r.cancelled_krw)} for r in rows), ("sub_id", "date"))
        async with self.db.engine.begin() as conn:
            await self._upsert(conn, affiliate_report, data, ("sub_id", "date"))

    async def insert_clicks(self, rows: Sequence[ClickRow]) -> None:
        if not rows:
            return
        data = [{"ts": r.ts, "hub_code": r.hub_code, "sub_id": r.sub_id, "ip_hash": r.ip_hash,
                 "ua_hash": r.ua_hash, "referer": r.referer} for r in rows]
        async with self.db.engine.begin() as conn:
            await conn.execute(insert(click_event), data)

    # ───────────── reads ─────────────

    async def latest_metrics(self, publication_id: uuid.UUID) -> dict[str, float]:
        """value at max(ts) per metric — the cumulative counter's latest snapshot."""
        pe = perf_event
        latest = (select(pe.c.metric, func.max(pe.c.ts).label("mts"))
                  .where(pe.c.publication_id == publication_id)
                  .group_by(pe.c.metric).subquery())
        q = (select(pe.c.metric, pe.c.value)
             .join(latest, and_(pe.c.metric == latest.c.metric, pe.c.ts == latest.c.mts))
             .where(pe.c.publication_id == publication_id))
        async with self.db.engine.connect() as conn:
            return {m: float(v) for m, v in (await conn.execute(q)).all()}

    async def report_totals(self, sub_ids: Sequence[str], since: date | None = None) -> dict[str, dict[str, int]]:
        """Σ over dates ≥ since per sub_id; sub_ids without rows are absent (as in the memory store)."""
        wanted = list(dict.fromkeys(sub_ids))
        if not wanted:
            return {}
        ar = affiliate_report
        out: dict[str, dict[str, int]] = {}
        async with self.db.engine.connect() as conn:
            for chunk in _chunks(wanted):
                q = (select(ar.c.sub_id, *[func.sum(ar.c[f]).label(f) for f in _REPORT_FIELDS])
                     .where(ar.c.sub_id.in_(list(chunk))).group_by(ar.c.sub_id))
                if since is not None:
                    q = q.where(ar.c.date >= since)
                for row in (await conn.execute(q)).all():
                    out[row[0]] = {f: int(row[i + 1] or 0) for i, f in enumerate(_REPORT_FIELDS)}
        return out

    async def metric_population(self, platform: str, metric: str, since: datetime,
                                account_publication_ids: Sequence[uuid.UUID] | None = None) -> list[float]:
        """Latest (within ts ≥ since) value of ``metric`` for every matching publication."""
        pe = perf_event
        base = [pe.c.metric == metric, pe.c.platform == platform, pe.c.ts >= since]
        id_chunks: list[Sequence[uuid.UUID] | None]
        if account_publication_ids is None:
            id_chunks = [None]
        else:
            ids = list(dict.fromkeys(account_publication_ids))
            if not ids:
                return []
            id_chunks = list(_chunks(ids))
        values: list[float] = []
        async with self.db.engine.connect() as conn:
            for chunk in id_chunks:
                conds = list(base)
                if chunk is not None:
                    conds.append(pe.c.publication_id.in_(list(chunk)))
                latest = (select(pe.c.publication_id, func.max(pe.c.ts).label("mts"))
                          .where(*conds).group_by(pe.c.publication_id).subquery())
                q = (select(pe.c.value)
                     .join(latest, and_(pe.c.publication_id == latest.c.publication_id,
                                        pe.c.ts == latest.c.mts))
                     .where(pe.c.metric == metric, pe.c.platform == platform))
                values.extend(float(v) for (v,) in (await conn.execute(q)).all())
        return values

    async def clicks_since(self, since: datetime, hub_code: str | None = None) -> list[ClickRow]:
        ce = click_event
        q = select(ce).where(ce.c.ts >= since)
        if hub_code is not None:
            q = q.where(ce.c.hub_code == hub_code)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(q.order_by(ce.c.id))).mappings().all()
        return [ClickRow(ts=r["ts"], hub_code=r["hub_code"], sub_id=r["sub_id"], ip_hash=r["ip_hash"],
                         ua_hash=r["ua_hash"], referer=r["referer"] or "") for r in rows]

    async def daily_money(self, since: date) -> list[dict[str, Any]]:
        """Per-day Σ GMV / commission / cancellations / orders / clicks over all sub_ids (§9.3 money view)."""
        ar = affiliate_report
        q = (select(ar.c.date, func.sum(ar.c.gmv_krw), func.sum(ar.c.commission_krw),
                    func.sum(ar.c.cancelled_krw), func.sum(ar.c.orders), func.sum(ar.c.clicks))
             .where(ar.c.date >= since).group_by(ar.c.date).order_by(ar.c.date))
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(q)).all()
        return [{"date": d.isoformat(), "gmv_krw": int(g or 0), "commission_krw": int(c or 0),
                 "cancelled_krw": int(x or 0), "orders": int(o or 0), "clicks": int(k or 0)}
                for d, g, c, x, o, k in rows]
