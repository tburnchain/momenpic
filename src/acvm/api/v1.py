"""/api/v1 — the console contract (console/src/lib/api.ts mirrors these shapes)."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from acvm.api.auth import ctx_of, get_principal, require
from acvm.context import AppContext
from acvm.core.enums import AccountStatus, Platform, RunStatus
from acvm.core.ids import uuid7
from acvm.db import tables as t
from acvm.domain.models import ChannelAccount, Content
from acvm.modules.m7_gate.ruleset import describe as describe_rules
from acvm.orchestration.cron import Cron
from acvm.quant.distributions import LogNormal
from acvm.quant.economics import (
    Funnel,
    breakeven_impressions,
    content_economics,
    prob_at_least_one_tail,
    trials_for_tail,
)
from acvm.security.audit import audit
from acvm.security.rbac import Perm, Principal

router = APIRouter(prefix="/api/v1")


def _ws(p: Principal) -> uuid.UUID:
    assert p.workspace_id is not None
    return uuid.UUID(p.workspace_id)


async def _signed(ctx: AppContext, uri: str | None) -> str | None:
    if not uri:
        return None
    try:
        return await ctx.objects.signed_url(uri, ctx.settings.objects.signed_url_ttl_seconds)
    except Exception:
        return None


# ───────────────────────── identity ─────────────────────────


@router.get("/me")
async def me(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return {"subject": p.subject, "email": p.subject if "@" in p.subject else None, "name": p.name,
            "role": p.role.value, "workspace_id": p.workspace_id, "user_id": p.user_id, "auth": p.auth}


# ───────────────────────── commands & runs ─────────────────────────


class CommandIn(BaseModel):
    text: str = Field(min_length=2, max_length=500)


@router.post("/commands")
async def submit_command(body: CommandIn, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    run = await request.app.state.gateway.submit(body.text, workspace_id=_ws(p), actor=p.subject)
    plan = run.plan
    return {"run_id": str(run.id), "status": run.status.value, "plan": plan.model_dump(mode="json") if plan else None,
            "approval": plan.approval.model_dump(mode="json") if plan else None,
            "explanation": plan.explanation if plan else ""}


def _run_row(r: t.CommandRunRow) -> dict[str, Any]:
    return {"id": str(r.id), "raw_command": r.raw_command, "status": r.status, "est_cost_krw": r.est_cost_krw,
            "cost_krw": r.cost_krw, "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None, "summary": r.summary or {}}


@router.get("/runs")
async def list_runs(request: Request, limit: int = Query(50, le=200),
                    p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    async with ctx_of(request).uow() as repos:
        rows = await repos.runs.recent(_ws(p), limit)
    return {"items": [_run_row(r) for r in rows]}


async def _content_card(ctx: AppContext, c: Content) -> dict[str, Any]:
    async with ctx.uow() as repos:
        product = await repos.products.get(c.product_id)
        check = await repos.compliance.latest(c.id)
        pubs = await repos.publications.by_content(c.id)
        out_pubs = []
        for pub in pubs:
            acc = await repos.accounts.get(pub.channel_account_id)
            link = await repos.links.get(pub.link_id)
            out_pubs.append({"id": str(pub.id), "platform": pub.platform.value, "account_handle": acc.handle if acc else "",
                             "state": pub.state.value, "scheduled_at": pub.scheduled_at.isoformat(),
                             "published_at": pub.published_at.isoformat() if pub.published_at else None,
                             "permalink": (pub.error or {}).get("permalink") if pub.error else None,
                             "sub_id": link.sub_id if link else None})
    r = c.render
    return {
        "id": str(c.id), "kind": c.kind.value, "status": c.status.value, "hook": c.script.hook, "persona": c.persona,
        "template_id": c.template_id,
        "product": {"id": product.id, "name": product.name, "price": product.price, "image_url": product.image_url,
                    "category_id": product.category_id} if product else None,
        "verdict": check.verdict.value if check else None,
        "findings": [{"rule_id": f.rule_id, "verdict": f.verdict.value, "message": f.message, "fixed": f.fixed}
                     for f in (check.findings if check else [])],
        "media_url": await _signed(ctx, r.media_uri if r else None),
        "thumbnail_url": await _signed(ctx, r.thumbnail_uri if r else None),
        "publications": out_pubs,
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        row = await repos.session.get(t.CommandRunRow, run_id)
        if row is None or row.workspace_id != _ws(p):
            raise HTTPException(404, "run not found")
        contents = await repos.contents.by_run(run_id)
        spent = await repos.costs.run_total(run_id)
    run = _run_row(row) | {"plan": row.plan}
    return {"run": run, "contents": [await _content_card(ctx, c) for c in contents],
            "cost": {"spent_krw": spent, "cap_krw": (row.plan or {}).get("budget_cap_krw", 0)}}


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.APPROVE_PLAN))) -> dict[str, Any]:
    run = await request.app.state.gateway.approve(run_id, by=p.subject)
    return {"run_id": str(run_id), "status": run.status.value}


class RejectIn(BaseModel):
    reason: str = ""


@router.post("/runs/{run_id}/reject")
async def reject_run(run_id: uuid.UUID, body: RejectIn, request: Request,
                     p: Principal = Depends(require(Perm.APPROVE_PLAN))) -> dict[str, Any]:
    await request.app.state.gateway.reject(run_id, by=p.subject, reason=body.reason)
    return {"run_id": str(run_id), "status": RunStatus.REJECTED.value}


# ───────────────────────── HOLD queue ─────────────────────────


@router.get("/holds")
async def list_holds(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        items = await repos.compliance.pending_reviews(_ws(p))
        out = []
        for it in items:
            c = await repos.contents.get(it.content_id)
            check = await repos.compliance.latest(it.content_id)
            product = await repos.products.get(c.product_id) if c else None
            if c is None:
                continue
            sc = c.script
            out.append({
                "id": str(it.id), "content_id": str(c.id), "run_id": str(it.run_id) if it.run_id else None,
                "created_at": it.created_at.isoformat(), "reasons": it.reasons,
                "product": {"name": product.name, "price": product.price, "image_url": product.image_url} if product else None,
                "kind": c.kind.value, "hook": sc.hook,
                "script_text": "\n".join(b.narration for b in sc.beats) or sc.body,
                "caption_preview": f"{sc.disclosure}\n{sc.caption}" if sc.disclosure and not sc.caption.startswith(sc.disclosure)
                else sc.caption,
                "media_url": await _signed(ctx, c.render.media_uri if c.render else None),
                "thumbnail_url": await _signed(ctx, c.render.thumbnail_uri if c.render else None),
                "findings": [f.model_dump(mode="json") for f in (check.findings if check else [])],
            })
    return {"items": out}


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


@router.post("/holds/{item_id}/decision")
async def decide_hold(item_id: uuid.UUID, body: DecisionIn, request: Request,
                      p: Principal = Depends(require(Perm.REVIEW_HOLD))) -> dict[str, Any]:
    gw = request.app.state.gateway
    content = await gw.pipeline.gate.decide_review(item_id, approve=body.approve, by=p.subject, note=body.note)
    await ctx_of(request).relay_outbox()
    return {"status": content.status.value}


# ───────────────────────── dashboards (§9.3) ─────────────────────────


@router.get("/dashboards/money")
async def money(request: Request, days: int = Query(30, ge=1, le=365), p: Principal = Depends(require(Perm.VIEW))) -> Any:
    from acvm.modules.m9_analytics import dashboards

    return await dashboards.money_view(ctx_of(request), _ws(p), days)


@router.get("/dashboards/pipeline")
async def pipeline_view(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> Any:
    from acvm.modules.m9_analytics import dashboards

    return await dashboards.pipeline_view(ctx_of(request), _ws(p))


@router.get("/dashboards/risk")
async def risk(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> Any:
    from acvm.modules.m9_analytics import dashboards

    return await dashboards.risk_view(ctx_of(request), _ws(p))


@router.get("/dashboards/learning")
async def learning(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> Any:
    from acvm.modules.m9_analytics import dashboards

    ctx = ctx_of(request)
    view = await dashboards.learning_view(ctx, _ws(p))
    gw = request.app.state.gateway
    try:
        view.setdefault("admission", await gw.pipeline.optimizer.platform_admission(_ws(p)))
    except Exception:
        view.setdefault("admission", {})
    async with ctx.uow() as repos:
        rules = await repos.playbook.active(_ws(p), 20)
    view.setdefault("playbook", [{"text": r.text, "confidence": r.confidence, "lift": r.lift} for r in rules])
    return view


# ───────────────────────── accounts ─────────────────────────


def _acc(a: ChannelAccount) -> dict[str, Any]:
    return {"id": str(a.id), "platform": a.platform.value, "handle": a.handle, "niche": a.niche, "status": a.status.value,
            "daily_cap": a.daily_cap, "cooldown_until": a.cooldown_until.isoformat() if a.cooldown_until else None,
            "created_at": a.created_at.isoformat()}


@router.get("/accounts")
async def list_accounts(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    async with ctx_of(request).uow() as repos:
        return {"items": [_acc(a) for a in await repos.accounts.list(_ws(p))]}


class AccountIn(BaseModel):
    platform: Platform
    handle: str
    niche: str
    token_ref: str
    daily_cap: int = Field(ge=1, le=50)
    persona_default: str = "30대 여성 직장인"
    platform_user_id: str = ""


@router.post("/accounts")
async def create_account(body: AccountIn, request: Request, p: Principal = Depends(require(Perm.MANAGE_ACCOUNTS))) -> dict[str, Any]:
    ctx = ctx_of(request)
    acc = ChannelAccount(workspace_id=_ws(p), created_at=ctx.clock.now(), **body.model_dump())
    async with ctx.uow() as repos:
        await repos.accounts.add(acc)
        await audit(repos, ctx.clock, p.subject, "account.create", str(acc.id), platform=acc.platform.value,
                    handle=acc.handle)
    return _acc(acc)


class AccountPatch(BaseModel):
    status: AccountStatus | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=50)


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: uuid.UUID, body: AccountPatch, request: Request,
                        p: Principal = Depends(require(Perm.MANAGE_ACCOUNTS))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        row = await repos.session.get(t.ChannelAccountRow, account_id)
        if row is None or row.workspace_id != _ws(p):
            raise HTTPException(404, "account not found")
        if body.status is not None:
            row.status = body.status.value
            row.cooldown_until = None if body.status is AccountStatus.ACTIVE else row.cooldown_until
        if body.daily_cap is not None:
            row.daily_cap = body.daily_cap
        await audit(repos, ctx.clock, p.subject, "account.update", str(account_id), **body.model_dump(exclude_none=True))
        acc = await repos.accounts.get(account_id)
    assert acc is not None
    return _acc(acc)


# ───────────────────────── schedules (§5.2 예약 실행) ─────────────────────────


@router.get("/schedules")
async def list_schedules(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    async with ctx_of(request).uow() as repos:
        rows = (await repos.session.execute(select(t.RecurringCommandRow).where(
            t.RecurringCommandRow.workspace_id == _ws(p)))).scalars().all()
    return {"items": [{"id": str(r.id), "raw_command": r.raw_command, "cron": r.cron, "active": r.active,
                       "created_by": r.created_by} for r in rows]}


class ScheduleIn(BaseModel):
    text: str
    cron: str


@router.post("/schedules")
async def create_schedule(body: ScheduleIn, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    try:
        Cron(body.cron)
    except ValueError as exc:
        raise HTTPException(422, f"invalid cron: {exc}") from exc
    ctx = ctx_of(request)
    sid = uuid7()
    async with ctx.uow() as repos:
        repos.session.add(t.RecurringCommandRow(id=sid, workspace_id=_ws(p), raw_command=body.text, cron=body.cron,
                                                active=True, created_by=p.subject, created_at=ctx.clock.now()))
        await audit(repos, ctx.clock, p.subject, "schedule.create", str(sid), cron=body.cron, text=body.text)
    return {"id": str(sid), "raw_command": body.text, "cron": body.cron, "active": True, "created_by": p.subject}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: uuid.UUID, request: Request, p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    ctx = ctx_of(request)
    async with ctx.uow() as repos:
        row = await repos.session.get(t.RecurringCommandRow, schedule_id)
        if row is None or row.workspace_id != _ws(p):
            raise HTTPException(404, "schedule not found")
        row.active = False
        await audit(repos, ctx.clock, p.subject, "schedule.disable", str(schedule_id))
    return {"id": str(schedule_id), "active": False}


# ───────────────────────── Kakao manual queue (§7) ─────────────────────────


@router.get("/manual-queue")
async def manual_queue(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    items = await request.app.state.gateway.pipeline.dist.manual_queue(_ws(p))
    return {"items": items}


class ManualDoneIn(BaseModel):
    permalink: str = Field(min_length=8, max_length=500)


@router.post("/manual-queue/{publication_id}/done")
async def manual_done(publication_id: uuid.UUID, body: ManualDoneIn, request: Request,
                      p: Principal = Depends(require(Perm.COMMAND))) -> dict[str, Any]:
    pub = await request.app.state.gateway.pipeline.dist.complete_manual(publication_id, body.permalink, p.subject)
    return {"publication_id": str(pub.id), "state": pub.state.value}


# ───────────────────────── rules & audit ─────────────────────────


@router.get("/rules")
async def rules(request: Request, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    g = ctx_of(request).settings.gate
    return {"version": g.rule_set_version, "rules": describe_rules(),
            "disclosure": {"text": g.standard_disclosure, "video_text": g.video_disclosure}}


@router.get("/audit")
async def audit_log(request: Request, limit: int = Query(200, le=1000), p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    async with ctx_of(request).uow() as repos:
        items = await repos.audit.recent(limit)
    return {"items": [i.model_dump(mode="json") for i in items]}


# ───────────────────────── quant (§2) ─────────────────────────


@router.get("/quant/breakeven")
async def quant_breakeven(c_unit: float = 150, ctr: float = 0.008, cvr: float = 0.03, aov: float = 30_000,
                          r: float = 0.03, p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    f = Funnel(ctr, cvr, aov, r)
    return {"breakeven_views": breakeven_impressions(c_unit, f), "revenue_per_impression": f.revenue_per_impression,
            "epc": f.epc, "commission_per_order": f.commission_per_order}


@router.get("/quant/simulate")
async def quant_simulate(median: float = Query(600, gt=0), sigma: float = Query(2.0, gt=0, le=5), c_unit: float = 150,
                         ctr: float = 0.008, cvr: float = 0.03, aov: float = 30_000, r: float = 0.03,
                         n: int = Query(300, ge=1, le=100_000), q: float = Query(0.01, gt=0, lt=1),
                         p: Principal = Depends(require(Perm.VIEW))) -> dict[str, Any]:
    dist = LogNormal.from_median(median, sigma)
    ce = content_economics(dist, Funnel(ctr, cvr, aov, r), c_unit)
    return {"mean_views": dist.mean, "p_breakeven": ce.p_breakeven, "expected_profit_per_content": ce.expected_profit,
            "breakeven_views": ce.breakeven_views if math.isfinite(ce.breakeven_views) else None,
            "tail_share_top1": ce.tail_share_top1, "tail_share_top5": ce.tail_share_top5,
            "prob_tail_hit": {"q": q, "n": n, "p": prob_at_least_one_tail(q, n)},
            "trials_for_95": trials_for_tail(q, 0.95)}


__all__ = ["UTC", "date", "datetime", "router", "timedelta"]
