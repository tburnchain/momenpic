"""External data sources consumed by M9 and the normalisation of their payloads (§4 M9).

Modules never import adapters: platform clients arrive through ``ctx.extras["platform_clients"]``
(``dict[Platform, PlatformClient]``) and the Coupang Partners client through ``ctx.extras["coupang"]``.
The protocols below are the *minimal* surface M9 needs, so any adapter (official API, sandbox fixture,
test fake) satisfying them works.

Coupang Partners report rows (``/v2/providers/affiliate_open_api/apis/openapi/v1/reports/{kind}``) carry
``date`` (yyyyMMdd), ``subId`` and kind-specific numbers; key spelling differs between API versions and
sandbox fixtures, so every field is read through an ordered alias list:

    clicks      click | clicks | clickCount
    orders      orderCount | orders
    GMV         gmv | saleAmount | orderAmount
    commission  commission | commissionAmount
    cancels     cancelCommission | … | commission (reversed commission) — else cancelAmount | cancelledAmount

``cancelled_krw`` is in *commission* units: §2.4 nets it from confirmed commission
(MoneyView.net_commission = commission − cancelled).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from acvm.ports import AffiliateReportRow

ReportKind = Literal["clicks", "orders", "cancels", "commission"]
REPORT_KINDS: tuple[ReportKind, ...] = ("clicks", "orders", "cancels", "commission")
UNATTRIBUTED_SUB_ID = ""  # rows without subId still count in the money view (account totals)


class CoupangReports(Protocol):
    """Coupang Partners report API (HMAC-signed; implemented by the M1/M3 adapter)."""

    async def reports(self, kind: ReportKind, start: date, end: date,
                      sub_id: str | None = None) -> list[dict[str, Any]]: ...


class InsightsClient(Protocol):
    """The part of ``acvm.infra.platforms.base.PlatformClient`` M9 uses."""

    async def insights(self, post_id: str, token: str) -> Any:
        """Return ``Insights`` (``.metrics``: cumulative counters) or a plain metrics mapping."""
        ...


_SUB_ID_KEYS = ("subId", "sub_id", "subid", "subID")
_DATE_KEYS = ("date", "reportDate", "day")
_CLICK_KEYS = ("click", "clicks", "clickCount", "click_count")
_ORDER_KEYS = ("orderCount", "orders", "order_count", "orderCnt")
_GMV_KEYS = ("gmv", "saleAmount", "orderAmount", "sale_amount", "order_amount", "gmv_krw")
_COMMISSION_KEYS = ("commission", "commissionAmount", "commission_amount", "commission_krw")
_CANCEL_COMMISSION_KEYS = ("cancelCommission", "cancelledCommission", "cancel_commission", "commission",
                           "commissionAmount", "commission_amount")
_CANCEL_AMOUNT_KEYS = ("cancelAmount", "cancelledAmount", "cancel_amount", "cancelled_krw", "amount")


def access_token(secret_value: str) -> str:
    """Account secret → bearer token. Vault may hold the raw token or a JSON blob with access_token."""
    raw = secret_value.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw
        token = data.get("access_token") if isinstance(data, dict) else None
        if isinstance(token, str) and token:
            return token
    return raw


def insight_metrics(result: Any) -> dict[str, float]:
    """Finite numeric counters from an ``Insights`` object or mapping (non-numeric fields dropped)."""
    metrics = getattr(result, "metrics", result)
    out: dict[str, float] = {}
    if isinstance(metrics, Mapping):
        for name, value in metrics.items():
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                out[str(name)] = v
    return out


def _number(row: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            try:
                v = float(str(row[key]).replace(",", ""))
            except ValueError:
                continue
            if math.isfinite(v):
                return v
    return None


def report_date(row: Mapping[str, Any], default: date) -> date:
    for key in _DATE_KEYS:
        value = row.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip().replace("-", "").replace("/", "").replace(".", "")
        if len(text) >= 8 and text[:8].isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return default


def report_sub_id(row: Mapping[str, Any]) -> str:
    for key in _SUB_ID_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return UNATTRIBUTED_SUB_ID


@dataclass(slots=True)
class _DayAccumulator:
    clicks: float = 0.0
    orders: float = 0.0
    gmv: float = 0.0
    commission_from_orders: float = 0.0
    commission_report: float | None = None
    cancelled: float = 0.0
    kinds: set[str] = field(default_factory=set)
    order_ids: set[str] = field(default_factory=set)


def aggregate_reports(day: date, by_kind: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[AffiliateReportRow]:
    """Fold the four daily Coupang reports into one row per (sub_id, ``day``).

    * clicks ← clicks report; orders, GMV ← orders report;
    * commission ← commission report when it has rows for the sub_id, else Σ commission of order rows;
    * cancelled ← |reversed commission| of the cancels report (cancel amount when no commission field).

    Rows dated other than ``day`` are ignored: writing them would *replace* a complete (sub_id, date)
    row with a partial one under ReplacingMergeTree semantics.
    """
    acc: dict[str, _DayAccumulator] = {}
    for kind, rows in by_kind.items():
        for row in rows:
            if report_date(row, day) != day:
                continue
            a = acc.setdefault(report_sub_id(row), _DayAccumulator())
            a.kinds.add(kind)
            if kind == "clicks":
                a.clicks += _number(row, _CLICK_KEYS) or 0.0
            elif kind == "orders":
                count = _number(row, _ORDER_KEYS)
                if count is not None:  # aggregated report shape: one row carries the count
                    a.orders += count
                else:  # line-item shape (one row per order line): count distinct order ids
                    oid = str(row.get("orderId") or row.get("order_id") or "")
                    if oid and oid not in a.order_ids:
                        a.order_ids.add(oid)
                        a.orders += 1.0
                a.gmv += _number(row, _GMV_KEYS) or 0.0
                a.commission_from_orders += _number(row, _COMMISSION_KEYS) or 0.0
            elif kind == "cancels":
                value = _number(row, _CANCEL_COMMISSION_KEYS)
                if value is None:
                    value = _number(row, _CANCEL_AMOUNT_KEYS)
                a.cancelled += abs(value or 0.0)
            elif kind == "commission":
                a.commission_report = (a.commission_report or 0.0) + (_number(row, _COMMISSION_KEYS) or 0.0)
    out: list[AffiliateReportRow] = []
    for sub_id in sorted(acc):
        a = acc[sub_id]
        commission = a.commission_report if a.commission_report is not None else a.commission_from_orders
        out.append(AffiliateReportRow(
            date=day, sub_id=sub_id, clicks=max(round(a.clicks), 0), orders=max(round(a.orders), 0),
            gmv_krw=max(round(a.gmv), 0), commission_krw=max(round(commission), 0),
            cancelled_krw=max(round(a.cancelled), 0)))
    return out
