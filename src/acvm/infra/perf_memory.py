"""In-memory PerfStore with ReplacingMergeTree semantics (tests / single-process demo)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from acvm.ports import AffiliateReportRow, ClickRow, PerfRow


class MemoryPerfStore:
    def __init__(self) -> None:
        # ReplacingMergeTree keys: (publication, metric, ts) and (sub_id, date)
        self.perf: dict[tuple[uuid.UUID, str, datetime], PerfRow] = {}
        self.reports: dict[tuple[str, date], AffiliateReportRow] = {}
        self.clicks: list[ClickRow] = []

    async def insert_perf(self, rows: Sequence[PerfRow]) -> None:
        for r in rows:
            self.perf[(r.publication_id, r.metric, r.ts)] = r

    async def insert_reports(self, rows: Sequence[AffiliateReportRow]) -> None:
        for r in rows:
            self.reports[(r.sub_id, r.date)] = r

    async def insert_clicks(self, rows: Sequence[ClickRow]) -> None:
        self.clicks.extend(rows)

    async def latest_metrics(self, publication_id: uuid.UUID) -> dict[str, float]:
        latest: dict[str, tuple[datetime, float]] = {}
        for (pid, metric, ts), row in self.perf.items():
            if pid == publication_id and (metric not in latest or ts > latest[metric][0]):
                latest[metric] = (ts, row.value)
        return {m: v for m, (_, v) in latest.items()}

    async def report_totals(self, sub_ids: Sequence[str], since: date | None = None) -> dict[str, dict[str, int]]:
        wanted = set(sub_ids)
        out: dict[str, dict[str, int]] = {}
        for (sid, d), r in self.reports.items():
            if sid not in wanted or (since and d < since):
                continue
            acc = out.setdefault(sid, {"clicks": 0, "orders": 0, "gmv_krw": 0, "commission_krw": 0,
                                       "cancelled_krw": 0})
            acc["clicks"] += r.clicks
            acc["orders"] += r.orders
            acc["gmv_krw"] += r.gmv_krw
            acc["commission_krw"] += r.commission_krw
            acc["cancelled_krw"] += r.cancelled_krw
        return out

    async def metric_population(self, platform: str, metric: str, since: datetime,
                                account_publication_ids: Sequence[uuid.UUID] | None = None) -> list[float]:
        wanted = set(account_publication_ids) if account_publication_ids is not None else None
        latest: dict[uuid.UUID, tuple[datetime, float]] = {}
        for (pid, m, ts), row in self.perf.items():
            if m != metric or row.platform != platform or ts < since:
                continue
            if wanted is not None and pid not in wanted:
                continue
            if pid not in latest or ts > latest[pid][0]:
                latest[pid] = (ts, row.value)
        return [v for _, v in latest.values()]

    async def clicks_since(self, since: datetime, hub_code: str | None = None) -> list[ClickRow]:
        return [c for c in self.clicks if c.ts >= since and (hub_code is None or c.hub_code == hub_code)]

    async def daily_money(self, since: date) -> list[dict[str, Any]]:
        days: dict[date, dict[str, Any]] = {}
        for (_, d), r in self.reports.items():
            if d < since:
                continue
            acc = days.setdefault(d, {"date": d.isoformat(), "gmv_krw": 0, "commission_krw": 0,
                                      "cancelled_krw": 0, "orders": 0, "clicks": 0})
            acc["gmv_krw"] += r.gmv_krw
            acc["commission_krw"] += r.commission_krw
            acc["cancelled_krw"] += r.cancelled_krw
            acc["orders"] += r.orders
            acc["clicks"] += r.clicks
        return [days[d] for d in sorted(days)]
