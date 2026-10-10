"""M9 분석·어트리뷰션 (design §4 M9, §6.2, §6.3, §9.2).

Pipeline (each step idempotent, safe to re-run):

1. :meth:`AnalyticsService.collect_insights` — hourly. Platform insights for PUBLISHED publications are
   written as ``perf_event`` rows stamped with the *hour bucket* ⌊now⌋ₕ, so a re-run inside the same hour
   replaces instead of duplicating (ReplacingMergeTree key (publication_id, metric, ts)).
   Cadence: age ≤ 7 d → every run; 7 d < age ≤ 30 d → only when ⌊now⌋ₕ.hour ≡ 0 (mod 6); older → never.
2. :meth:`AnalyticsService.collect_reports` — daily (D+1). The four Coupang reports fold into one
   ``affiliate_report`` row per (sub_id, date); re-collection replaces.
3. :meth:`AnalyticsService.attribute` — joins both by subId (publication.link_id → affiliate_link.sub_id,
   1:1 by construction, §6) and emits ``perf.updated`` (content_id, epc, percentile) for M10/M2:

       EPC        = commission / clicks          (clicks = Coupang report clicks, else platform link clicks;
                                                  0 when no clicks)
       percentile = mid-rank of the publication's views among the same workspace's publications on the
                    same platform with a views snapshot in the last 30 days.

   The envelope idempotency key is ``{publication_id}:{yyyymmddHH}`` — one event per publication per hour.
4. :meth:`AnalyticsService.detect_invalid_clicks` — the §4 M9 invalid-click watch:
   (a) hub redirect log bursts: same hashed IP > 3 clicks on one link within 60 s;
   (b) CTR spikes: link_clicks k out of views n tested against the Beta-Binomial predictive of a
       Beta(α, β) prior fitted by method of moments on sibling CTRs (same workspace & platform,
       leave-one-out), alarm at p = P(K ≥ k) < 1e-4.
   One alert per link per KST day (persisted marker), + ``acvm_invalid_click_alerts_total`` + audit log.

Secrets: account tokens are resolved per call from ``ctx.secrets`` and never logged; adapter errors are
logged by exception *type* only because HTTP errors can embed ``access_token`` query parameters.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from acvm.context import AppContext
from acvm.core.clock import KST, ensure_utc
from acvm.core.enums import PublicationState
from acvm.core.events import Event, InvalidClickDetected, PerfUpdated
from acvm.core.logging import get_logger
from acvm.core.metrics import ATTRIBUTION_LAG_SECONDS, INVALID_CLICK_ALERTS
from acvm.db import tables as t
from acvm.domain.models import Publication
from acvm.modules.m9_analytics.sources import (
    REPORT_KINDS,
    CoupangReports,
    InsightsClient,
    access_token,
    aggregate_reports,
    insight_metrics,
)
from acvm.ports import PerfRow
from acvm.quant.anomaly import ctr_spike_pvalue, repeated_click_bursts
from acvm.quant.bayes import BetaPosterior, fit_beta_prior
from acvm.quant.distributions import percentile_rank
from acvm.security.audit import audit

log = get_logger(__name__)

FRESH_WINDOW = timedelta(days=7)  # hourly collection
TRACK_WINDOW = timedelta(days=30)  # 6-hourly collection, attribution population window
SLOW_CADENCE_HOURS = 6
INSIGHTS_CONCURRENCY = 8
BURST_WINDOW_SECONDS = 60.0
BURST_MAX_CLICKS = 3  # > 3 clicks per ip_hash per link per 60 s ⇒ burst
CTR_ALARM_P = 1e-4
CTR_MIN_VIEWS = 100  # sibling CTRs from fewer views are mostly binomial noise
CTR_MIN_SIBLINGS = 5
LOO_MAX_POPULATION = 500  # above this a single outlier cannot move the moment fit; skip leave-one-out
VIEW_METRICS = ("views", "impressions")


def hour_bucket(now: datetime) -> datetime:
    return ensure_utc(now).replace(minute=0, second=0, microsecond=0)


def views_of(metrics: dict[str, float]) -> float:
    for name in VIEW_METRICS:
        if name in metrics:
            return metrics[name]
    return 0.0


@dataclass(frozen=True, slots=True)
class PublishedRef:
    """A PUBLISHED publication with the keys M9 joins on (one SQL round-trip for the whole batch)."""

    publication: Publication
    workspace_id: uuid.UUID
    sub_id: str
    hub_code: str
    token_ref: str


async def load_published(ctx: AppContext, since: datetime) -> list[PublishedRef]:
    """publication ⋈ content (workspace) ⋈ affiliate_link (subId) ⋈ channel_account (token_ref)."""
    q = (select(t.PublicationRow, t.ContentRow.workspace_id, t.AffiliateLinkRow.sub_id,
                t.AffiliateLinkRow.hub_code, t.ChannelAccountRow.token_ref)
         .join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id)
         .join(t.AffiliateLinkRow, t.AffiliateLinkRow.id == t.PublicationRow.link_id)
         .join(t.ChannelAccountRow, t.ChannelAccountRow.id == t.PublicationRow.channel_account_id)
         .where(t.PublicationRow.state == PublicationState.PUBLISHED.value,
                t.PublicationRow.published_at >= since)
         .order_by(t.PublicationRow.published_at, t.PublicationRow.id))
    async with ctx.uow() as r:
        rows = (await r.session.execute(q)).all()
    return [PublishedRef(Publication.model_validate(row), ws, sub_id, hub_code or "", token_ref)
            for row, ws, sub_id, hub_code, token_ref in rows]


class AnalyticsService:
    """M9 service; all methods take ``now`` for deterministic replays (defaults to ``ctx.clock``)."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _now(self, now: datetime | None) -> datetime:
        return ensure_utc(now or self.ctx.clock.now())

    # ───────────────────────────── 1. platform insights ─────────────────────────────

    @staticmethod
    def due_for_collection(published_at: datetime, now: datetime) -> bool:
        age = now - ensure_utc(published_at)
        if age < timedelta(0) or age > TRACK_WINDOW:
            return False
        return age <= FRESH_WINDOW or hour_bucket(now).hour % SLOW_CADENCE_HOURS == 0

    async def collect_insights(self, now: datetime | None = None) -> int:
        """Fetch insights for due publications; returns the number of publications collected."""
        now = self._now(now)
        bucket = hour_bucket(now)
        clients: dict[Any, InsightsClient] = self.ctx.extras.get("platform_clients") or {}
        if not clients:
            log.info("insights_skipped_no_clients")
            return 0
        refs = [ref for ref in await load_published(self.ctx, now - TRACK_WINDOW)
                if ref.publication.published_at is not None
                and self.due_for_collection(ref.publication.published_at, now)]
        tokens: dict[str, str] = {}
        sem = asyncio.Semaphore(INSIGHTS_CONCURRENCY)

        async def token_for(ref: PublishedRef) -> str | None:
            if ref.token_ref not in tokens:
                try:
                    tokens[ref.token_ref] = access_token(await self.ctx.secrets.get(ref.token_ref))
                except KeyError:
                    log.warning("insights_token_missing", account_id=str(ref.publication.channel_account_id))
                    return None
            return tokens[ref.token_ref]

        async def fetch(ref: PublishedRef) -> list[PerfRow] | None:
            pub = ref.publication
            client = clients.get(pub.platform) or clients.get(pub.platform.value)
            if client is None or not pub.platform_post_id:
                return None
            token = await token_for(ref)
            if token is None:
                return None
            async with sem:
                try:
                    result = await client.insights(pub.platform_post_id, token)
                except Exception as exc:  # adapter errors may embed the token in URLs → log the type only
                    log.warning("insights_failed", publication_id=str(pub.id), platform=pub.platform.value,
                                error=type(exc).__name__)
                    return None
            return [PerfRow(ts=bucket, content_id=pub.content_id, publication_id=pub.id,
                            platform=pub.platform.value, metric=name, value=value)
                    for name, value in sorted(insight_metrics(result).items())]

        results = await asyncio.gather(*(fetch(ref) for ref in refs))
        rows = [row for batch in results if batch for row in batch]
        collected = sum(1 for batch in results if batch is not None)
        if rows:
            await self.ctx.perf.insert_perf(rows)
        if collected:
            ATTRIBUTION_LAG_SECONDS.labels(source="platform").set((now - bucket).total_seconds())
        log.info("insights_collected", publications=collected, rows=len(rows), due=len(refs))
        return collected

    # ───────────────────────────── 2. Coupang reports ─────────────────────────────

    async def collect_reports(self, day: date) -> int:
        """Collect the four Coupang reports for ``day``; returns rows written (one per subId).

        All four kinds are fetched before anything is written: a partial write would *replace* a
        complete (sub_id, date) row with an incomplete one. Errors propagate for the scheduler to retry.
        """
        coupang: CoupangReports | None = self.ctx.extras.get("coupang")
        if coupang is None:
            log.info("reports_skipped_no_coupang_client")
            return 0
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for kind in REPORT_KINDS:
            by_kind[kind] = list(await coupang.reports(kind, day, day))
        rows = aggregate_reports(day, by_kind)
        if rows:
            await self.ctx.perf.insert_reports(rows)
        day_end = datetime.combine(day + timedelta(days=1), time(0, 0), KST)
        lag = (self._now(None) - day_end.astimezone(UTC)).total_seconds()
        ATTRIBUTION_LAG_SECONDS.labels(source="coupang").set(max(lag, 0.0))
        log.info("reports_collected", day=day.isoformat(), rows=len(rows),
                 raw={k: len(v) for k, v in by_kind.items()})
        return len(rows)

    # ───────────────────────────── 3. attribution ─────────────────────────────

    async def _metrics_for(self, refs: Sequence[PublishedRef]) -> dict[uuid.UUID, dict[str, float]]:
        sem = asyncio.Semaphore(INSIGHTS_CONCURRENCY)

        async def one(pid: uuid.UUID) -> tuple[uuid.UUID, dict[str, float]]:
            async with sem:
                return pid, await self.ctx.perf.latest_metrics(pid)

        return dict(await asyncio.gather(*(one(ref.publication.id) for ref in refs)))

    async def _view_populations(self, refs: Sequence[PublishedRef], since: datetime
                                ) -> dict[tuple[uuid.UUID, str], list[float]]:
        groups: dict[tuple[uuid.UUID, str], list[uuid.UUID]] = defaultdict(list)
        for ref in refs:
            groups[(ref.workspace_id, ref.publication.platform.value)].append(ref.publication.id)
        out: dict[tuple[uuid.UUID, str], list[float]] = {}
        for (ws, platform), ids in groups.items():
            values: list[float] = []
            for metric in VIEW_METRICS:
                values = await self.ctx.perf.metric_population(platform, metric, since, ids)
                if values:
                    break
            out[(ws, platform)] = values
        return out

    async def attribute(self, now: datetime | None = None) -> list[PerfUpdated]:
        """Join platform metrics with Coupang totals per publication and emit ``perf.updated``."""
        now = self._now(now)
        since = now - TRACK_WINDOW
        refs = await load_published(self.ctx, since)
        if not refs:
            return []
        sync_links = getattr(self.ctx.perf, "upsert_publication_links", None)
        if sync_links is not None:  # ClickHouse content_roi view needs the subId ↔ publication map
            await sync_links([{"publication_id": r.publication.id, "content_id": r.publication.content_id,
                               "sub_id": r.sub_id, "platform": r.publication.platform.value} for r in refs], now)
        totals = await self.ctx.perf.report_totals([r.sub_id for r in refs])
        metrics = await self._metrics_for(refs)
        populations = await self._view_populations(refs, since)
        bucket = hour_bucket(now).strftime("%Y%m%d%H")

        out: list[tuple[PerfUpdated, PublishedRef]] = []
        for ref in refs:
            pub = ref.publication
            m = metrics.get(pub.id, {})
            rep = totals.get(ref.sub_id)
            if not m and not rep:
                continue
            views = views_of(m)
            report_clicks = (rep or {}).get("clicks", 0)
            clicks = report_clicks if report_clicks > 0 else m.get("link_clicks", 0.0)
            commission = (rep or {}).get("commission_krw", 0)
            epc = commission / clicks if clicks > 0 else 0.0
            percentile = percentile_rank(views, populations.get((ref.workspace_id, pub.platform.value), []))
            out.append((PerfUpdated(content_id=pub.content_id, epc=epc, percentile=percentile,
                                    platform=pub.platform), ref))
        if out:
            async with self.ctx.uow() as r:
                for payload, ref in out:
                    await self.ctx.emit(r, Event.of(payload, workspace_id=ref.workspace_id,
                                                    idempotency_key=f"{ref.publication.id}:{bucket}"))
        log.info("attribution_done", publications=len(refs), events=len(out))
        return [p for p, _ in out]

    # ───────────────────────────── 4. invalid clicks ─────────────────────────────

    async def _ctr_spikes(self, now: datetime) -> list[tuple[str, str, float, dict[str, Any]]]:
        refs = await load_published(self.ctx, now - TRACK_WINDOW)
        metrics = await self._metrics_for(refs)
        siblings: dict[tuple[uuid.UUID, str], list[tuple[uuid.UUID, float]]] = defaultdict(list)
        for ref in refs:
            m = metrics.get(ref.publication.id, {})
            views = views_of(m)
            if views >= CTR_MIN_VIEWS and "link_clicks" in m:
                siblings[(ref.workspace_id, ref.publication.platform.value)].append(
                    (ref.publication.id, min(m["link_clicks"] / views, 1.0)))
        default = BetaPosterior.from_mean(self.ctx.settings.economics.ctr, 50.0)
        found: list[tuple[str, str, float, dict[str, Any]]] = []
        for ref in refs:
            pub = ref.publication
            if pub.published_at is None or now - ensure_utc(pub.published_at) > FRESH_WINDOW:
                continue
            m = metrics.get(pub.id, {})
            views, clicks = views_of(m), m.get("link_clicks", 0.0)
            if views <= 0 or clicks <= 0:
                continue
            group = siblings.get((ref.workspace_id, pub.platform.value), [])
            others = ([rate for pid, rate in group if pid != pub.id] if len(group) <= LOO_MAX_POPULATION
                      else [rate for _, rate in group])
            prior = fit_beta_prior(others) if len(others) >= CTR_MIN_SIBLINGS else default
            p = ctr_spike_pvalue(int(clicks), int(views), prior.alpha, prior.beta)
            if p < CTR_ALARM_P:
                score = -math.log10(max(p, 1e-300))
                found.append((ref.sub_id, "ctr_spike", score, {
                    "publication_id": str(pub.id), "views": views, "link_clicks": clicks, "p_value": p,
                    "prior_ctr": prior.mean}))
        return found

    async def detect_invalid_clicks(self, now: datetime | None = None,
                                    lookback: timedelta = timedelta(hours=24)) -> list[InvalidClickDetected]:
        """Run both detectors; emit at most one ``invalid_click.detected`` per link per KST day."""
        now = self._now(now)
        today = now.astimezone(KST).date()
        clicks = await self.ctx.perf.clicks_since(now - lookback)
        events = [(ensure_utc(c.ts).timestamp(), c.ip_hash, c.sub_id or f"hub:{c.hub_code}") for c in clicks]
        # link_key → (reason, score, day, detail); the strongest signal per link wins
        candidates: dict[str, tuple[str, float, date, dict[str, Any]]] = {}
        for burst in repeated_click_bursts(events, window_seconds=BURST_WINDOW_SECONDS, max_clicks=BURST_MAX_CLICKS):
            day = datetime.fromtimestamp(burst.first_ts, UTC).astimezone(KST).date()
            detail = {"ip_hash": burst.ip_hash, "clicks": burst.clicks, "window_s": burst.last_ts - burst.first_ts}
            prev = candidates.get(burst.link_key)
            if prev is None or float(burst.clicks) > prev[1]:
                candidates[burst.link_key] = ("click_burst", float(burst.clicks), day, detail)
        for sub_id, reason, score, detail in await self._ctr_spikes(now):
            if sub_id not in candidates:
                candidates[sub_id] = (reason, score, today, detail)

        emitted: list[InvalidClickDetected] = []
        if not candidates:
            return emitted
        async with self.ctx.uow() as r:
            for link_key in sorted(candidates):
                reason, score, day, detail = candidates[link_key]
                if link_key.startswith("hub:"):
                    link = await r.links.by_hub_code(link_key[4:])
                else:
                    link = await r.links.by_sub_id(link_key)
                marker = f"m9:invalid_click:{link.id if link else link_key}:{day.isoformat()}"
                if await r.activities.get(marker) is not None:
                    continue
                workspace_id: uuid.UUID | None = None
                if link is not None:
                    pub = await r.publications.by_link(link.id)
                    content = await r.contents.get(pub.content_id) if pub else None
                    workspace_id = content.workspace_id if content else None
                sub_id = link.sub_id if link else ("" if link_key.startswith("hub:") else link_key)
                payload = InvalidClickDetected(link_id=link.id if link else None, sub_id=sub_id, reason=reason,
                                               score=round(score, 4))
                await self.ctx.emit(r, Event.of(payload, workspace_id=workspace_id,
                                                idempotency_key=f"invalid_click:{marker}"))
                await r.activities.put(marker, "m9_invalid_click",
                                       {"reason": reason, "score": score, "detail": detail}, now)
                await audit(r, self.ctx.clock, "m9", "invalid_click.detected", sub_id or link_key,
                            reason=reason, score=round(score, 4), link_id=str(link.id) if link else None,
                            day=day.isoformat(), **{k: v for k, v in detail.items() if k != "ip_hash"})
                INVALID_CLICK_ALERTS.labels(reason=reason).inc()
                emitted.append(payload)
        if emitted:
            log.warning("invalid_clicks_detected", count=len(emitted), reasons=sorted({e.reason for e in emitted}))
        return emitted
