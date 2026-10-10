"""§9.3 핵심 대시보드 — pure read functions returning JSON-able dicts (served by the console API).

* money_view    — 일별 GMV / 확정 수수료 / 취소 차감 / 순이익 + 채널별 중앙값 조회수 ÷ I* (KPI #1, §2.3).
                  GMV, commission and net are always separate fields (§2.4: never conflate GMV with revenue).
* pipeline_view — 단계별 처리량 · 대기열 길이 · 실패율 · 렌더 길이 분포.
* risk_view     — 계정 상태 (active/cooldown/restricted), 게이트 판정 분포, 무효 클릭 경보, 최근 경고.
* learning_view — arm별 사후분포, 탐색/활용 비율, 꼬리 콘텐츠 목록 (top 2 % views per platform).

Notes on scope: affiliate reports belong to the partner account wired into the deployment (one Coupang
Partners account per workspace — §부록 B "수익 수령 주체"), so ``ctx.perf.daily_money`` is the workspace's
revenue; costs come from the workspace's ``cost_ledger``. Channel admission (G3, §11) uses
``channel_admission`` on each account's last-30-day view samples against
I* = C_unit / (CTR·CVR·AOV·r) from ``settings.economics`` with C_unit weighted by the channel's content
kinds (short ⇒ c_unit_short_krw, text ⇒ c_unit_text_krw).
"""

from __future__ import annotations

import math
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from acvm.context import AppContext
from acvm.core.clock import KST, ensure_utc
from acvm.core.enums import ContentKind, Platform, PublicationState
from acvm.db import tables as t
from acvm.domain.models import ArmKey, ArmPosteriorState, Publication
from acvm.quant.bayes import BetaPosterior, GammaPosterior, RevenuePosterior
from acvm.quant.distributions import percentile_rank
from acvm.quant.economics import Funnel, MoneyView, channel_admission

TAIL_PERCENTILE = 0.98  # "꼬리 콘텐츠" = top 2 % views per platform (§4 M10 amplification bar)
ADMISSION_WINDOW = timedelta(days=30)


def _f(x: float | None, digits: int = 4) -> float | None:
    """JSON-safe float (inf/NaN → None)."""
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), digits)


def _iso(dt: datetime | None) -> str | None:
    return ensure_utc(dt).isoformat() if dt else None


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile on an ascending list."""
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[idx]


def _dist(values: Iterable[float]) -> dict[str, Any]:
    v = sorted(values)
    if not v:
        return {"count": 0, "mean": None, "p50": None, "p90": None, "min": None, "max": None}
    return {"count": len(v), "mean": _f(statistics.fmean(v), 2), "p50": _f(_quantile(v, 0.5), 2),
            "p90": _f(_quantile(v, 0.9), 2), "min": _f(v[0], 2), "max": _f(v[-1], 2)}


def _funnel(ctx: AppContext) -> Funnel:
    e = ctx.settings.economics
    return Funnel(ctr=e.ctr, cvr=e.cvr, aov_krw=e.aov_krw, commission_rate=e.commission_rate)


def unit_cost(ctx: AppContext, kinds: Iterable[str]) -> float:
    """Mean C_unit over the channel's contents (§2.5: short ≈ 150 KRW, text ≈ 30 KRW)."""
    e = ctx.settings.economics
    costs = [e.c_unit_text_krw if k == ContentKind.TEXT.value else e.c_unit_short_krw for k in kinds]
    return statistics.fmean(costs) if costs else e.c_unit_short_krw


async def _content_kinds(ctx: AppContext, content_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not content_ids:
        return {}
    async with ctx.uow() as r:
        rows = (await r.session.execute(select(t.ContentRow.id, t.ContentRow.kind)
                                        .where(t.ContentRow.id.in_(list(set(content_ids)))))).all()
    return {cid: kind for cid, kind in rows}


def _posterior(state: ArmPosteriorState) -> RevenuePosterior:
    return RevenuePosterior(
        ctr=BetaPosterior(state.ctr_alpha, state.ctr_beta),
        orders_per_click=GammaPosterior(state.order_shape, state.order_rate),
        commission_rate_param=GammaPosterior(state.comm_shape, state.comm_rate),
        impressions=state.impressions, clicks=state.clicks, orders=state.orders,
        commission_krw=state.commission_krw, trials=state.trials)


# ───────────────────────────── money view ─────────────────────────────


async def channel_kpis(ctx: AppContext, workspace_id: uuid.UUID, now: datetime | None = None) -> list[dict[str, Any]]:
    """Per-account G3 admission: median views (80 % CI) vs I*, ratio = median / I* (KPI #1)."""
    now = ensure_utc(now or ctx.clock.now())
    since = now - ADMISSION_WINDOW
    async with ctx.uow() as r:
        accounts = await r.accounts.list(workspace_id)
        pubs = await r.publications.published_since(since, workspace_id)
    kinds = await _content_kinds(ctx, [p.content_id for p in pubs])
    by_account: dict[uuid.UUID, list[Publication]] = defaultdict(list)
    for p in pubs:
        by_account[p.channel_account_id].append(p)
    funnel = _funnel(ctx)
    out: list[dict[str, Any]] = []
    for acc in accounts:
        mine = by_account.get(acc.id, [])
        samples = (await ctx.perf.metric_population(acc.platform.value, "views", since, [p.id for p in mine])
                   if mine else [])
        c_unit = unit_cost(ctx, [kinds[p.content_id] for p in mine if p.content_id in kinds])
        d = channel_admission(samples, funnel, c_unit)
        out.append({
            "account_id": str(acc.id), "platform": acc.platform.value, "handle": acc.handle,
            "status": acc.status.value, "n": d.n, "median_views": _f(d.median_views, 1),
            "median_ci": [_f(d.median_ci[0], 1), _f(d.median_ci[1], 1)],
            "breakeven_views": _f(d.breakeven_views, 1), "ratio": _f(d.ratio, 3), "admitted": d.admitted,
            "c_unit_krw": _f(c_unit, 2), "reason": d.reason,
        })
    out.sort(key=lambda c: (list(Platform).index(Platform(c["platform"])), c["handle"]))
    return out


async def money_view(ctx: AppContext, workspace_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    """Daily GMV / commission / cancellations / cost / net (dense, oldest first) + totals + channels."""
    days = max(1, int(days))
    now = ensure_utc(ctx.clock.now())
    today = now.astimezone(KST).date()
    start = today - timedelta(days=days - 1)
    revenue = {row["date"]: row for row in await ctx.perf.daily_money(start)}
    start_at = datetime.combine(start, time(0, 0), KST)
    async with ctx.uow() as r:
        cost_rows = (await r.session.execute(
            select(t.CostLedgerRow.at, t.CostLedgerRow.amount_krw)
            .where(t.CostLedgerRow.workspace_id == workspace_id, t.CostLedgerRow.at >= start_at))).all()
    costs: dict[date, float] = defaultdict(float)
    for at, amount in cost_rows:
        costs[ensure_utc(at).astimezone(KST).date()] += float(amount)

    daily: list[dict[str, Any]] = []
    sums: defaultdict[str, float] = defaultdict(float)
    for i in range(days):
        d = start + timedelta(days=i)
        rev = revenue.get(d.isoformat(), {})
        mv = MoneyView(gmv_krw=float(rev.get("gmv_krw", 0)), commission_krw=float(rev.get("commission_krw", 0)),
                       cancelled_krw=float(rev.get("cancelled_krw", 0)), cost_krw=costs.get(d, 0.0))
        row = {"date": d.isoformat(), "gmv_krw": _f(mv.gmv_krw, 0), "commission_krw": _f(mv.commission_krw, 0),
               "cancelled_krw": _f(mv.cancelled_krw, 0), "net_commission_krw": _f(mv.net_commission_krw, 0),
               "cost_krw": _f(mv.cost_krw, 0), "net_krw": _f(mv.net_profit_krw, 0),
               "orders": int(rev.get("orders", 0)), "clicks": int(rev.get("clicks", 0))}
        daily.append(row)
        for k in ("gmv_krw", "commission_krw", "cancelled_krw", "orders", "clicks"):
            sums[k] += float(rev.get(k, 0))
        sums["cost_krw"] += mv.cost_krw
    total = MoneyView(gmv_krw=sums["gmv_krw"], commission_krw=sums["commission_krw"],
                      cancelled_krw=sums["cancelled_krw"], cost_krw=sums["cost_krw"])
    e = ctx.settings.economics
    return {
        "workspace_id": str(workspace_id), "from": start.isoformat(), "to": today.isoformat(), "days": days,
        "daily": daily,
        "totals": {"gmv_krw": _f(total.gmv_krw, 0), "commission_krw": _f(total.commission_krw, 0),
                   "cancelled_krw": _f(total.cancelled_krw, 0),
                   "net_commission_krw": _f(total.net_commission_krw, 0), "cost_krw": _f(total.cost_krw, 0),
                   "net_krw": _f(total.net_profit_krw, 0), "effective_rate": _f(total.effective_rate, 5),
                   "orders": int(sums["orders"]), "clicks": int(sums["clicks"])},
        "channels": await channel_kpis(ctx, workspace_id, now),
        "assumptions": {"ctr": e.ctr, "cvr": e.cvr, "aov_krw": e.aov_krw, "commission_rate": e.commission_rate,
                        "c_unit_short_krw": e.c_unit_short_krw, "c_unit_text_krw": e.c_unit_text_krw},
    }


# ───────────────────────────── pipeline view ─────────────────────────────


async def pipeline_view(ctx: AppContext, workspace_id: uuid.UUID, days: int = 1) -> dict[str, Any]:
    """Throughput by stage, queue depths, publish failure rate and render length distribution."""
    now = ensure_utc(ctx.clock.now())
    since = now - timedelta(days=max(1, int(days)))
    pub_ws = (select(t.PublicationRow.state, func.count())
              .join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id)
              .where(t.ContentRow.workspace_id == workspace_id))
    async with ctx.uow() as r:
        s = r.session
        pub_states = dict((await s.execute(pub_ws.where(t.PublicationRow.scheduled_at >= since)
                                           .group_by(t.PublicationRow.state))).all())
        contents = dict((await s.execute(
            select(t.ContentRow.status, func.count())
            .where(t.ContentRow.workspace_id == workspace_id, t.ContentRow.created_at >= since)
            .group_by(t.ContentRow.status))).all())
        runs = dict((await s.execute(
            select(t.CommandRunRow.status, func.count())
            .where(t.CommandRunRow.workspace_id == workspace_id, t.CommandRunRow.started_at >= since)
            .group_by(t.CommandRunRow.status))).all())
        verdicts = dict((await s.execute(
            select(t.ComplianceCheckRow.verdict, func.count())
            .join(t.ContentRow, t.ContentRow.id == t.ComplianceCheckRow.content_id)
            .where(t.ContentRow.workspace_id == workspace_id, t.ComplianceCheckRow.decided_at >= since)
            .group_by(t.ComplianceCheckRow.verdict))).all())
        renders = (await s.execute(
            select(t.ContentRow.render)
            .where(t.ContentRow.workspace_id == workspace_id, t.ContentRow.created_at >= since,
                   t.ContentRow.render.is_not(None)))).scalars().all()
        queued_all = dict((await s.execute(
            pub_ws.where(t.PublicationRow.state.in_((PublicationState.QUEUED.value,
                                                     PublicationState.MANUAL_PENDING.value)))
            .group_by(t.PublicationRow.state))).all())
        due = int((await s.execute(
            select(func.count()).select_from(t.PublicationRow)
            .join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id)
            .where(t.ContentRow.workspace_id == workspace_id,
                   t.PublicationRow.state == PublicationState.QUEUED.value,
                   t.PublicationRow.scheduled_at <= now))).scalar_one())
        reviews = int((await s.execute(
            select(func.count()).select_from(t.ReviewItemRow)
            .where(t.ReviewItemRow.workspace_id == workspace_id, t.ReviewItemRow.status == "pending"))).scalar_one())
        outbox = int((await s.execute(select(func.count()).select_from(t.OutboxRow)
                                      .where(t.OutboxRow.published_at.is_(None)))).scalar_one())
    published = int(pub_states.get(PublicationState.PUBLISHED.value, 0))
    failed = int(pub_states.get(PublicationState.FAILED.value, 0)) + int(
        pub_states.get(PublicationState.EXPIRED.value, 0))
    durations = [float(rd["duration_sec"]) for rd in renders
                 if isinstance(rd, dict) and isinstance(rd.get("duration_sec"), (int, float))]
    return {
        "workspace_id": str(workspace_id), "since": since.isoformat(),
        "contents_by_status": {k: int(v) for k, v in sorted(contents.items())},
        "publications_by_state": {k: int(v) for k, v in sorted(pub_states.items())},
        "publish_failure_rate": _f(failed / (published + failed), 4) if published + failed else None,
        "runs_by_status": {k: int(v) for k, v in sorted(runs.items())},
        "gate_verdicts": {k: int(v) for k, v in sorted(verdicts.items())},
        "render_video_seconds": _dist(durations),
        "queues": {"publish_due": due,
                   "publish_queued": int(queued_all.get(PublicationState.QUEUED.value, 0)),
                   "manual_pending": int(queued_all.get(PublicationState.MANUAL_PENDING.value, 0)),
                   "review_pending": reviews, "outbox_pending": outbox},
    }


# ───────────────────────────── risk view ─────────────────────────────


async def risk_view(ctx: AppContext, workspace_id: uuid.UUID, days: int = 7) -> dict[str, Any]:
    """Account states, gate verdict distribution, invalid-click alerts and account warnings."""
    now = ensure_utc(ctx.clock.now())
    since = now - timedelta(days=max(1, int(days)))
    async with ctx.uow() as r:
        s = r.session
        accounts = await r.accounts.list(workspace_id)
        verdicts = dict((await s.execute(
            select(t.ComplianceCheckRow.verdict, func.count())
            .join(t.ContentRow, t.ContentRow.id == t.ComplianceCheckRow.content_id)
            .where(t.ContentRow.workspace_id == workspace_id, t.ComplianceCheckRow.decided_at >= since)
            .group_by(t.ComplianceCheckRow.verdict))).all())
        sub_ids = set((await s.execute(
            select(t.AffiliateLinkRow.sub_id)
            .join(t.PublicationRow, t.PublicationRow.link_id == t.AffiliateLinkRow.id)
            .join(t.ContentRow, t.ContentRow.id == t.PublicationRow.content_id)
            .where(t.ContentRow.workspace_id == workspace_id))).scalars().all())
        alerts = [a for a in await r.audit.recent(limit=500, action="invalid_click.detected")
                  if ensure_utc(a.at) >= since and a.target in sub_ids]
        acc_ids = [a.id for a in accounts]
        warnings = (await s.execute(
            select(t.AccountHealthEventRow)
            .where(t.AccountHealthEventRow.account_id.in_(acc_ids), t.AccountHealthEventRow.kind == "warning",
                   t.AccountHealthEventRow.at >= since)
            .order_by(t.AccountHealthEventRow.at.desc()).limit(100))).scalars().all() if acc_ids else []
    by_id = {a.id: a for a in accounts}
    status_counts = Counter(a.status.value for a in accounts)
    total_verdicts = sum(int(v) for v in verdicts.values())
    return {
        "workspace_id": str(workspace_id), "since": since.isoformat(),
        "accounts_by_status": {s: int(status_counts.get(s, 0)) for s in ("active", "cooldown", "restricted")},
        "accounts": [{"account_id": str(a.id), "platform": a.platform.value, "handle": a.handle,
                      "status": a.status.value, "cooldown_until": _iso(a.cooldown_until), "niche": a.niche}
                     for a in accounts],
        "gate_verdicts": {k: int(v) for k, v in sorted(verdicts.items())},
        "gate_verdict_share": {k: _f(int(v) / total_verdicts, 4) for k, v in sorted(verdicts.items())}
        if total_verdicts else {},
        "invalid_click_alerts": [{"sub_id": a.target, "at": _iso(a.at), "reason": a.payload.get("reason"),
                                  "score": a.payload.get("score"), "link_id": a.payload.get("link_id")}
                                 for a in alerts],
        "warnings": [{"account_id": str(w.account_id),
                      "handle": by_id[w.account_id].handle if w.account_id in by_id else "",
                      "platform": by_id[w.account_id].platform.value if w.account_id in by_id else "",
                      "at": _iso(w.at), "detail": w.detail} for w in warnings],
    }


# ───────────────────────────── learning view ─────────────────────────────


async def learning_view(ctx: AppContext, workspace_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    """Arm posteriors, explore/exploit split of M10 allocations, tail contents, active playbook rules."""
    now = ensure_utc(ctx.clock.now())
    since = now - timedelta(days=max(1, int(days)))
    async with ctx.uow() as r:
        states = await r.arms.all(workspace_id)
        allocs = (await r.session.execute(
            select(t.ActivityResultRow.result)
            .where(t.ActivityResultRow.stage == "m10_recommend", t.ActivityResultRow.created_at >= since)
        )).scalars().all()
        pubs = await r.publications.published_since(since, workspace_id)
        rules = await r.playbook.active(workspace_id, limit=50)

    arms: list[dict[str, Any]] = []
    for st in states:
        post = _posterior(st)
        lo, hi = post.ctr.interval(0.9)
        try:
            key: dict[str, str] = ArmKey.parse(st.arm_key).model_dump()
        except ValueError:
            key = {}
        arms.append({"arm_key": st.arm_key, **key, "trials": st.trials, "impressions": _f(st.impressions, 0),
                     "clicks": _f(st.clicks, 0), "orders": _f(st.orders, 0),
                     "commission_krw": _f(st.commission_krw, 0), "ctr_mean": _f(post.ctr.mean, 6),
                     "ctr_90": [_f(lo, 6), _f(hi, 6)], "epc_mean": _f(post.mean_epc, 2),
                     "rpi_mean": _f(post.mean_rpi, 4), "retired": st.retired, "updated_at": _iso(st.updated_at)})
    arms.sort(key=lambda a: (-(a["rpi_mean"] or 0.0), a["arm_key"]))

    modes = Counter[str]()
    for res in allocs:
        if isinstance(res, dict) and res.get("workspace_id") == str(workspace_id):
            for mode, n in (res.get("modes") or {}).items():
                modes[str(mode)] += int(n)
    n_modes = modes["explore"] + modes["exploit"]

    views: dict[uuid.UUID, float] = {}
    for p in pubs:
        m = await ctx.perf.latest_metrics(p.id)
        views[p.id] = m.get("views", m.get("impressions", 0.0))
    by_platform: dict[str, list[Publication]] = defaultdict(list)
    for p in pubs:
        by_platform[p.platform.value].append(p)
    tail: list[dict[str, Any]] = []
    for platform, group in by_platform.items():
        population = [views[p.id] for p in group]
        for p in group:
            pct = percentile_rank(views[p.id], population)
            if pct >= TAIL_PERCENTILE and views[p.id] > 0:
                tail.append({"content_id": str(p.content_id), "publication_id": str(p.id), "platform": platform,
                             "views": _f(views[p.id], 0), "percentile": _f(pct, 4),
                             "published_at": _iso(p.published_at)})
    tail.sort(key=lambda x: -(x["views"] or 0.0))

    return {
        "workspace_id": str(workspace_id), "since": since.isoformat(),
        "arms": arms, "arms_retired": sum(1 for a in arms if a["retired"]),
        "allocation": {"explore": modes["explore"], "exploit": modes["exploit"],
                       "explore_share": _f(modes["explore"] / n_modes, 4) if n_modes else None},
        "tail_contents": tail,
        "playbook": [{"text": rule.text, "confidence": _f(rule.confidence, 3), "lift": _f(rule.lift, 3),
                      "scope": rule.scope} for rule in rules],
    }
