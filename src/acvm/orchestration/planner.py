"""Deterministic plan assembly (§5, Appendix A).

    "주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘"
      → intent create_and_publish · products M1.select(kitchen, n=5, min_pos=0.7)
      → per product: script(short, 30대 여성, hooks=3, 22s) → render(problem-solution-v3, ko-F-30s-brisk) → gate(v1)
      → publish: threads text+video link=body · instagram reels link=hub
      → schedule spread 09–23 KST ±7m · budget est 750 원 / cap 2 000 원 · approval auto|console
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from acvm.agents.commander import resolve_category
from acvm.context import AppContext
from acvm.core.clock import KST
from acvm.core.enums import AccountStatus, ContentKind, ContentStatus, Intent, Platform
from acvm.db import tables as t
from acvm.domain.models import Approval, ChannelAccount, CommandSlots, Plan, PlanStep, PublishTarget
from acvm.modules.m4_script.templates import DEFAULT_TEMPLATE, TEMPLATES, resolve_persona

PLACEMENT = {Platform.THREADS: "body", Platform.FACEBOOK: "caption_first_line", Platform.INSTAGRAM: "hub",
             Platform.TIKTOK: "hub", Platform.YOUTUBE: "hub", Platform.KAKAO: "manual"}
FORMAT_SHORT = {Platform.THREADS: "text+video", Platform.INSTAGRAM: "reels", Platform.YOUTUBE: "shorts",
                Platform.TIKTOK: "video", Platform.FACEBOOK: "reels", Platform.KAKAO: "manual"}
TEXT_CAPABLE = {Platform.THREADS, Platform.KAKAO}  # Facebook = Page Reels (video), §7
DEFAULT_PLATFORMS = {ContentKind.TEXT: [Platform.THREADS], ContentKind.SHORT: [Platform.THREADS, Platform.INSTAGRAM]}
MAX_COUNT = 50


def _start_and_window(schedule: str, now: datetime) -> tuple[datetime, tuple[int, int], str | None]:
    local = now.astimezone(KST)
    if schedule == "tonight":
        start = datetime.combine(local.date(), time(20, 0), KST)
        return max(start, local), (20, 23), None
    if schedule == "tomorrow":
        return datetime.combine(local.date() + timedelta(days=1), time(9, 0), KST), (9, 23), None
    if schedule.startswith("daily@"):
        hh, mm = (int(x) for x in schedule[6:].split(":"))
        first = datetime.combine(local.date(), time(hh, mm), KST)
        if first <= local:
            first += timedelta(days=1)
        return first, (hh, 23), f"{mm} {hh} * * *"
    return local, (9, 23), None


async def build_plan(ctx: AppContext, *, workspace_id: uuid.UUID, run_id: uuid.UUID, slots: CommandSlots,
                     ambiguities: list[str] | None = None) -> Plan:
    s = ctx.settings
    now = ctx.clock.now()
    notes: list[str] = list(ambiguities or [])
    async with ctx.uow() as repos:
        accounts = await repos.accounts.list(workspace_id)
        day_spent = await repos.costs.workspace_total(workspace_id, ctx.day_start_kst())
        day_budget = await ctx.daily_budget(repos, workspace_id)
        session = repos.session
        from sqlalchemy import func, select

        approved_by_template = dict((await session.execute(
            select(t.ContentRow.template_id, func.count()).where(t.ContentRow.workspace_id == workspace_id,
                                                                  t.ContentRow.status == ContentStatus.APPROVED.value)
            .group_by(t.ContentRow.template_id))).all())
        category_seen = set((await session.execute(
            select(t.ProductRow.category_id).join(t.ContentRow, t.ContentRow.product_id == t.ProductRow.id)
            .where(t.ContentRow.workspace_id == workspace_id,
                   t.ContentRow.status == ContentStatus.APPROVED.value))).scalars().all())

    active = [a for a in accounts if a.status is AccountStatus.ACTIVE]
    # ── category / niche ──
    category_id = slots.category
    if category_id is None:
        for a in active:
            category_id = resolve_category(a.niche)
            if category_id:
                notes.append(f"카테고리 미지정 → 계정 니치 '{a.niche}' 사용")
                break
    category_id = category_id or "1013"
    niche_accounts = [a for a in active if resolve_category(a.niche) == category_id]
    niche = niche_accounts[0].niche if niche_accounts else (active[0].niche if active else "general")

    # ── kind / platforms ──
    kind = slots.kind or (ContentKind.TEXT if slots.platforms and set(slots.platforms) <= TEXT_CAPABLE
                          else ContentKind.SHORT)
    platforms = list(slots.platforms) or DEFAULT_PLATFORMS[kind]
    if kind is ContentKind.TEXT:
        dropped = [p for p in platforms if p not in TEXT_CAPABLE]
        platforms = [p for p in platforms if p in TEXT_CAPABLE]
        if dropped:
            notes.append(f"글 콘텐츠는 {', '.join(p.value for p in dropped)}에 게시할 수 없어 제외")
    if Platform.TIKTOK in platforms and not s.platforms.tiktok_audited:
        notes.append("TikTok 앱 감사 전: 비공개(SELF_ONLY)로만 게시됩니다 (§7)")

    targets: list[PublishTarget] = []
    target_accounts: list[ChannelAccount] = []
    if slots.intent is Intent.CREATE_AND_PUBLISH:
        for p in platforms:
            pool = [a for a in niche_accounts if a.platform is p] or [a for a in active if a.platform is p]
            if not pool:
                notes.append(f"{p.value} 활성 계정 없음 → 제외")
                continue
            acc = pool[0]
            target_accounts.append(acc)
            targets.append(PublishTarget(platform=p, account_id=acc.id, account_handle=acc.handle,
                                         link_placement=PLACEMENT[p],
                                         format="text" if kind is ContentKind.TEXT else FORMAT_SHORT[p]))

    persona = resolve_persona(slots.persona or (target_accounts[0].persona_default if target_accounts else None))
    template_id = DEFAULT_TEMPLATE[kind]
    count = max(1, min(slots.count, MAX_COUNT))
    if slots.count > MAX_COUNT:
        notes.append(f"명령당 최대 {MAX_COUNT}개로 제한")
    start_at, window, cron = _start_and_window(slots.schedule, now)

    c_unit = s.economics.c_unit_short_krw if kind is ContentKind.SHORT else s.economics.c_unit_text_krw
    est = round(count * c_unit, 0) if slots.intent is not Intent.ANALYZE else 0.0
    remaining = max(0.0, day_budget - day_spent)
    cap = slots.budget_krw if slots.budget_krw else min(remaining, max(2_000.0, est * 2))

    reasons: list[str] = []
    if slots.intent is Intent.CREATE_AND_PUBLISH:
        young = [a.handle for a in target_accounts if (now - a.created_at).days < s.auto_approve_account_age_days]
        if young:
            reasons.append(f"계정 {s.auto_approve_account_age_days}일 미경과: {', '.join(young)}")
        if approved_by_template.get(template_id, 0) < s.gate.new_template_hold_count:
            reasons.append(f"템플릿 {template_id} 검증 미완료 (승인 {approved_by_template.get(template_id, 0)}건)")
        if category_id not in category_seen:
            reasons.append(f"신규 카테고리 {category_id}")
        if not targets:
            reasons.append("게시 가능한 대상 계정 없음")
    if est > cap:
        reasons.append(f"추정 비용 {est:,.0f}원 > 상한 {cap:,.0f}원")
    approval = Approval(mode="console" if reasons else "auto", reasons=reasons)

    steps = [PlanStep(id="select", module="M1", action="select",
                      params={"category_id": category_id, "n": count, "min_pos": 0.7, "keyword": slots.keyword})]
    if slots.intent is not Intent.ANALYZE:
        steps += [
            PlanStep(id="link", module="M3", action="create_links", depends_on=["select"],
                     params={"targets": [tg.platform.value for tg in targets]}),
            PlanStep(id="script", module="M4", action="generate", depends_on=["select"],
                     params={"kind": kind.value, "persona": persona.name, "hooks": 3,
                             "len": TEMPLATES[template_id].duration if kind is ContentKind.SHORT else 0}),
        ]
        prev = "script"
        if kind is ContentKind.SHORT:
            steps.append(PlanStep(id="render", module="M6", action="render", depends_on=["script"],
                                  params={"template": template_id, "voice": persona.voice_hint}))
            prev = "render"
        steps.append(PlanStep(id="gate", module="M7", action="check", depends_on=[prev, "link"],
                              params={"rule_set": s.gate.rule_set_version}))
        if slots.intent is Intent.CREATE_AND_PUBLISH:
            steps.append(PlanStep(id="publish", module="M8", action="publish", depends_on=["gate"],
                                  params={tg.platform.value: {"format": tg.format, "link": tg.link_placement,
                                                              "account": tg.account_handle} for tg in targets}))
    else:
        steps.append(PlanStep(id="analyze", module="M9", action="report", depends_on=["select"]))

    explanation = (f"{count}개 상품({category_id}) · {kind.value} · {persona.name} · "
                   f"{' / '.join(f'{tg.platform.value}({tg.link_placement})' for tg in targets) or '게시 없음'} · "
                   f"{window[0]:02d}–{window[1]:02d}시 분산 ±{s.quota.jitter_minutes}분 · 추정 {est:,.0f}원/상한 {cap:,.0f}원 · "
                   f"승인 {'자동' if approval.mode == 'auto' else '콘솔'}")
    if notes:
        explanation += " · " + " · ".join(notes)
    return Plan(run_id=run_id, workspace_id=workspace_id, intent=slots.intent, slots=slots, category_id=category_id,
                niche=niche, count=count, kind=kind, persona=persona.name, template_id=template_id,
                voice_id=persona.voice_hint, rule_set=s.gate.rule_set_version, targets=targets,
                schedule_window=window, jitter_minutes=s.quota.jitter_minutes, recurring_cron=cron,
                start_at=start_at, est_cost_krw=est, budget_cap_krw=cap, approval=approval, steps=steps,
                explanation=explanation)
