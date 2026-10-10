"""Pipeline activities — the deterministic execution layer (§5). Each public coroutine is one Temporal
activity (and is called directly by the local runner); each is idempotent for the same arguments.

Control flow per product (Figure "명령 실행 흐름"):

    select → script (M4) → [assets + render (M5/M6) for shorts] → gate (M7) ─┬─ PASS  → links (M3) → plan (M8)
                     ↑                                    │                 ├─ FIX   → regenerate / rerender / reschedule
                     └──────────── regenerate ────────────┘                 ├─ HOLD  → review queue (human)
                                                                             └─ BLOCK → discard, reason recorded
    publish(pub): price refresh (M1) → preflight (M7) → publish/poll (M8)
    learn: insights/reports (M9) → perf.updated → posteriors (M10) / tail DNA (M2) → next selection
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property
from typing import Any

from acvm import integrations
from acvm.agents.commander import interpret
from acvm.context import AppContext
from acvm.core.enums import ContentKind, ContentStatus, Intent, Platform, PublicationState, RunStatus, Verdict
from acvm.core.errors import BudgetExceeded, NotFound, PolicyViolation
from acvm.core.events import AmplifyRequested, CommandPlanned, Event
from acvm.core.ids import activity_key, uuid7
from acvm.core.logging import get_logger
from acvm.core.metrics import COMMAND_PLAN_SECONDS, COMMAND_TO_PUBLISH_SECONDS, STAGE_DURATION_SECONDS
from acvm.domain.models import AffiliateLink, CommandRun, Content, Plan, Publication, PublishTarget
from acvm.modules.m4_script.service import ScriptEngine, ScriptRequest
from acvm.modules.m7_gate.engine import GateEngine
from acvm.orchestration.planner import build_plan
from acvm.security.audit import audit

log = get_logger(__name__)
MAX_GATE_ROUNDS = 3


@dataclass
class ProductResult:
    product_id: int
    content_id: uuid.UUID | None = None
    verdict: str = ""
    publications: list[uuid.UUID] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"product_id": self.product_id, "content_id": str(self.content_id) if self.content_id else None,
                "verdict": self.verdict, "publications": [str(p) for p in self.publications], "notes": self.notes}


class Pipeline:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ── lazily constructed module services (each module is optional at import time) ──
    @cached_property
    def products(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m1_product.service import ProductIntelligence

        return ProductIntelligence(self.ctx)

    @cached_property
    def links(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m3_link.service import LinkService

        return LinkService(self.ctx)

    @cached_property
    def assets(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m5_asset.service import AssetService

        return AssetService(self.ctx)

    @cached_property
    def renderer(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m6_render.service import RenderService

        return RenderService(self.ctx)

    @cached_property
    def dist(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m8_distribution.service import DistributionService

        return DistributionService(self.ctx)

    @cached_property
    def analytics(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m9_analytics.service import AnalyticsService

        return AnalyticsService(self.ctx)

    @cached_property
    def optimizer(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m10_optimizer import Optimizer

        return Optimizer(self.ctx)

    @cached_property
    def dna(self):  # type: ignore[no-untyped-def]
        from acvm.modules.m2_dna.service import ContentDNAService

        return ContentDNAService(self.ctx)

    @cached_property
    def scripts(self) -> ScriptEngine:
        return ScriptEngine(self.ctx)

    @cached_property
    def gate(self) -> GateEngine:
        return GateEngine(self.ctx)

    # ───────────────────────── command lifecycle ─────────────────────────

    async def submit(self, text: str, *, workspace_id: uuid.UUID, actor: str) -> CommandRun:
        """Command → slots (Commander) → plan (deterministic) → persisted run awaiting approval or approved."""
        started = time.perf_counter()
        run_id = uuid7()
        await integrations.sync_llm(self.ctx)  # a Claude/Gemini key registered in the console (any process)
        slots, ambiguities = await interpret(text, self.ctx.llm)
        plan = await build_plan(self.ctx, workspace_id=workspace_id, run_id=run_id, slots=slots,
                                ambiguities=ambiguities)
        status = RunStatus.APPROVED if plan.approval.mode == "auto" else RunStatus.AWAITING_APPROVAL
        run = CommandRun(id=run_id, workspace_id=workspace_id, raw_command=text, plan=plan, status=status,
                         approved_by="auto" if status is RunStatus.APPROVED else None, started_at=self.ctx.clock.now())
        async with self.ctx.uow() as repos:
            await repos.runs.add(run)
            await audit(repos, self.ctx.clock, actor, "command.submit", str(run_id), text=text,
                        approval=plan.approval.mode, est=plan.est_cost_krw)
            await self.ctx.emit(repos, Event.of(CommandPlanned(run_id=run_id, approval=plan.approval.mode,
                                                               est_cost_krw=plan.est_cost_krw),
                                                workspace_id=workspace_id, run_id=run_id,
                                                idempotency_key=f"{run_id}:planned"))
        COMMAND_PLAN_SECONDS.observe(time.perf_counter() - started)
        return run

    async def approve(self, run_id: uuid.UUID, *, by: str) -> CommandRun:
        async with self.ctx.uow() as repos:
            run = await repos.runs.get(run_id)
            if run is None:
                raise NotFound(f"run {run_id}")
            if run.status is RunStatus.AWAITING_APPROVAL:
                await repos.runs.update(run_id, status=RunStatus.APPROVED, approved_by=by)
                await audit(repos, self.ctx.clock, by, "command.approve", str(run_id))
                run = run.model_copy(update={"status": RunStatus.APPROVED, "approved_by": by})
        return run

    async def reject(self, run_id: uuid.UUID, *, by: str, reason: str = "") -> None:
        async with self.ctx.uow() as repos:
            await repos.runs.update(run_id, status=RunStatus.REJECTED, ended_at=self.ctx.clock.now())
            await audit(repos, self.ctx.clock, by, "command.reject", str(run_id), reason=reason)

    async def load(self, run_id: uuid.UUID) -> tuple[CommandRun, Plan]:
        async with self.ctx.uow() as repos:
            run = await repos.runs.get(run_id)
        if run is None or run.plan is None:
            raise NotFound(f"run {run_id}")
        return run, run.plan

    async def mark(self, run_id: uuid.UUID, status: RunStatus, **summary: Any) -> None:
        async with self.ctx.uow() as repos:
            fields: dict[str, Any] = {"status": status}
            if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED_BUDGET):
                fields["ended_at"] = self.ctx.clock.now()
                fields["cost_krw"] = await repos.costs.run_total(run_id)
            if summary:
                run = await repos.runs.get(run_id)
                fields["summary"] = {**(run.summary if run else {}), **summary}
            await repos.runs.update(run_id, **fields)

    # ───────────────────────── production ─────────────────────────

    async def select(self, run_id: uuid.UUID) -> list[int]:
        run, plan = await self.load(run_id)
        if run.status not in (RunStatus.APPROVED, RunStatus.RUNNING):
            raise PolicyViolation(f"run {run_id} is {run.status.value}, not approved")
        await self.mark(run_id, RunStatus.RUNNING)
        started = time.perf_counter()
        events = await self.products.select(run_id=run_id, workspace_id=plan.workspace_id, category_id=plan.category_id,
                                            n=plan.count, min_pos=plan.min_pos, keyword=plan.slots.keyword)
        STAGE_DURATION_SECONDS.labels(stage="select").observe(time.perf_counter() - started)
        ids = [e.product_id for e in sorted(events, key=lambda e: e.rank)]
        await self.mark(run_id, RunStatus.RUNNING, selected=ids)
        return ids

    async def _choose_arm(self, plan: Plan, product_id: int, rank: int) -> tuple[str | None, str | None]:
        """Hook type / template for this product from the bandit (explore/exploit), else plan defaults."""
        try:
            recs = await self.optimizer.recommend(plan.workspace_id, plan.category_id, 1,
                                                  [t.platform for t in plan.targets] or [Platform.THREADS],
                                                  seed_key=f"{plan.run_id}:{product_id}:{rank}")
        except Exception as exc:
            log.info("optimizer_unavailable", error=str(exc)[:120])
            return None, None
        if not recs:
            return None, None
        rec = recs[0]
        from acvm.modules.m4_script.templates import TEMPLATES

        tmpl = rec.template_id if TEMPLATES.get(rec.template_id) and TEMPLATES[rec.template_id].kind is plan.kind else None
        return rec.hook_type, tmpl

    async def _script(self, plan: Plan, product_id: int, variant: int, *, hook: str | None, template: str | None,
                      parent: uuid.UUID | None = None, target: PublishTarget | None = None) -> Content:
        from acvm.core.enums import HookType

        primary = target or (plan.targets[0] if plan.targets else None)
        return await self.scripts.generate(ScriptRequest(
            run_id=plan.run_id, workspace_id=plan.workspace_id, product_id=product_id, kind=plan.kind,
            persona=plan.persona, platform=primary.platform if primary else Platform.THREADS,
            link_placement=primary.link_placement if primary else "hub",
            target_account_id=primary.account_id if primary else None, template_id=template or plan.template_id,
            hook_type=HookType(hook) if hook else None, variant_index=variant, parent_content_id=parent,
            budget_cap_krw=plan.budget_cap_krw))

    async def _render(self, content: Content, *, force: bool = False) -> Content:
        if content.kind is not ContentKind.SHORT:
            return content
        async with self.ctx.uow() as repos:
            product = await repos.products.get(content.product_id)
            if force and content.render is not None:
                content = content.model_copy(update={"render": None, "status": ContentStatus.DRAFT})
                await repos.contents.save(content)
        assert product is not None
        await self.assets.ensure_product_image(product)
        content = await self.assets.enrich_ai_visuals(content, product)  # GPT Image / Higgsfield when keys are active
        await self.assets.voice_for(content.persona, voice_id=(content.render_options or {}).get("voice_id") or None)
        started = time.perf_counter()
        await self.renderer.render(content.id)
        STAGE_DURATION_SECONDS.labels(stage="render").observe(time.perf_counter() - started)
        async with self.ctx.uow() as repos:
            refreshed = await repos.contents.get(content.id)
        assert refreshed is not None
        return refreshed

    async def produce(self, run_id: uuid.UUID, product_id: int, rank: int = 0, *,
                      parent: uuid.UUID | None = None, variant_offset: int = 0) -> ProductResult:
        """One product end-to-end through the gate; schedules distribution on PASS. Idempotent per (run, product)."""
        key = activity_key(run_id, product_id, "produce", {"parent": str(parent), "v": variant_offset})
        async with self.ctx.uow() as repos:
            cached = await repos.activities.get(key)
        if cached:
            return ProductResult(**{**cached, "content_id": uuid.UUID(cached["content_id"]) if cached["content_id"]
                                    else None, "publications": [uuid.UUID(p) for p in cached["publications"]]})
        _, plan = await self.load(run_id)
        result = ProductResult(product_id=product_id)
        await integrations.sync_llm(self.ctx)
        try:
            hook, template = await self._choose_arm(plan, product_id, rank)
            variant = variant_offset
            content = await self._script(plan, product_id, variant, hook=hook, template=template, parent=parent)
            content = await self._render(content)
            verdict = Verdict.FIX
            for round_ in range(1, MAX_GATE_ROUNDS + 1):
                outcome = await self.gate.evaluate(content.id, run_id=run_id)
                verdict = outcome.verdict
                if verdict is not Verdict.FIX or outcome.actions <= {"reschedule"}:
                    break
                if "regenerate" in outcome.actions:
                    variant += 1
                    result.notes.append(f"round {round_}: regenerate ({', '.join(outcome.reasons)[:120]})")
                    content = await self._script(plan, product_id, variant, hook=None, template=template, parent=parent)
                    content = await self._render(content)
                elif "rerender" in outcome.actions:
                    result.notes.append(f"round {round_}: rerender after in-place text fixes")
                    content = await self._render(outcome.content or content, force=True)
            else:
                if verdict is Verdict.FIX:
                    await self.gate_force_hold(content, run_id, "자동 수정 한도 초과")
                    verdict = Verdict.HOLD
            result.content_id = content.id
            result.verdict = verdict.value
            if plan.intent is Intent.CREATE_AND_PUBLISH and (verdict is Verdict.PASS or
                                                             (verdict is Verdict.FIX and outcome.actions <= {"reschedule"})):
                pubs = await self.distribute(content.id, run_id=run_id)
                result.publications = [p.id for p in pubs]
        except BudgetExceeded as exc:
            result.verdict = "BUDGET"
            result.notes.append(str(exc))
            await self.mark(run_id, RunStatus.PAUSED_BUDGET)
        async with self.ctx.uow() as repos:
            await repos.activities.put(key, "produce", result.as_dict(), self.ctx.clock.now())
        return result

    async def gate_force_hold(self, content: Content, run_id: uuid.UUID, reason: str) -> None:
        from acvm.domain.models import ComplianceCheck, Finding

        now = self.ctx.clock.now()
        chk = ComplianceCheck(content_id=content.id, rule_set_version="v1", verdict=Verdict.HOLD, decided_at=now,
                              findings=[Finding(rule_id="GATE", verdict=Verdict.HOLD, message=reason)])
        async with self.ctx.uow() as repos:
            await repos.compliance.add(chk)
            await repos.compliance.enqueue_review(content.workspace_id, content.id, chk.id, [reason], now, run_id=run_id)
            await repos.contents.save(content.model_copy(update={"status": ContentStatus.GATED}))

    async def distribute(self, content_id: uuid.UUID, *, run_id: uuid.UUID | None = None,
                         targets: list[PublishTarget] | None = None) -> list[Publication]:
        """Links per target (M3) → hub pages → scheduled publications (M8). Approved content only."""
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(content_id)
            product = await repos.products.get(content.product_id) if content else None
            rid = run_id or (content.created_by_run if content else None)
            run = await repos.runs.get(rid) if rid is not None else None
        if content is None or product is None:
            raise NotFound(f"content {content_id}")
        if content.status is not ContentStatus.APPROVED:
            raise PolicyViolation(f"content {content_id} is {content.status.value}; only approved content is distributed")
        plan = run.plan if run else None
        targets = targets if targets is not None else (plan.targets if plan else [])
        if not targets:
            return []
        links: dict[Platform, AffiliateLink] = {}
        for tg in targets:
            links[tg.platform] = await self.links.create_link(product=product, content_id=content.id,
                                                              platform=tg.platform, account_id=tg.account_id)
        hub_base = self.ctx.settings.link_hub_base_url.rstrip("/")
        not_before = max(self.ctx.clock.now(), plan.start_at) if plan and plan.start_at else self.ctx.clock.now()
        pubs = await self.dist.plan(content, targets, links, not_before=not_before, run_id=run_id,
                                    hub_url_for=lambda tg: f"{hub_base}/hub/{tg.account_handle}")
        # The hub lists the account's queued/published links, so it is (re)built only after the
        # publications exist; D-04 re-checks the stored page at preflight.
        for account_id in {tg.account_id for tg in targets if tg.link_placement == "hub"}:
            await self.links.build_hub_page(account_id)
        return list(pubs)

    async def finalize_run(self, run_id: uuid.UUID, results: list[ProductResult]) -> CommandRun:
        verdicts: dict[str, int] = {}
        for r in results:
            verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
        run, _ = await self.load(run_id)
        status = RunStatus.PAUSED_BUDGET if run.status is RunStatus.PAUSED_BUDGET else RunStatus.COMPLETED
        await self.mark(run_id, status, verdicts=verdicts, products=[r.as_dict() for r in results])
        run, _ = await self.load(run_id)
        return run

    # ───────────────────────── publishing ─────────────────────────

    async def publish_publication(self, publication_id: uuid.UUID) -> Publication:
        """Due publication → fresh price (M1) → preflight (M7) → publish (M8). Safe to call repeatedly."""
        async with self.ctx.uow() as repos:
            pub = await repos.publications.get(publication_id)
        if pub is None:
            raise NotFound(f"publication {publication_id}")
        if pub.state is PublicationState.PROCESSING:
            polled: Publication = await self.dist.poll(publication_id)
            return polled
        if pub.state is not PublicationState.QUEUED or pub.scheduled_at > self.ctx.clock.now():
            return pub
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(pub.content_id)
        assert content is not None
        try:
            await self.products.refresh_price(content.product_id)
        except Exception as exc:
            log.info("price_refresh_skipped", error=str(exc)[:120])
        pre = await self.gate.preflight(publication_id)
        if pre.verdict is Verdict.BLOCK:
            async with self.ctx.uow() as repos:
                return await repos.publications.transition(publication_id, PublicationState.CANCELLED,
                                                           self.ctx.clock.now(),
                                                           error={"preflight": pre.reasons})
        if pre.verdict is Verdict.HOLD or (pre.verdict is Verdict.FIX and "reschedule" in pre.actions):
            delay = timedelta(hours=6) if pre.verdict is Verdict.HOLD else timedelta(hours=1)
            async with self.ctx.uow() as repos:
                return await repos.publications.transition(publication_id, PublicationState.QUEUED,
                                                           self.ctx.clock.now(),
                                                           scheduled_at=self.ctx.clock.now() + delay,
                                                           error={"deferred": pre.reasons})
        published: Publication = await self.dist.publish(publication_id)
        if published.state is PublicationState.PUBLISHED and content.created_by_run:
            await self._observe_latency(content.created_by_run, content.kind)
        return published

    async def _observe_latency(self, run_id: uuid.UUID, kind: ContentKind) -> None:
        async with self.ctx.uow() as repos:
            run = await repos.runs.get(run_id)
            if run is None or run.summary.get("first_publish_at"):
                return
            await repos.runs.update(run_id, summary={**run.summary, "first_publish_at": self.ctx.clock.now().isoformat()})
        COMMAND_TO_PUBLISH_SECONDS.labels(kind=kind.value).observe(
            (self.ctx.clock.now() - run.started_at).total_seconds())

    async def publish_due(self, limit: int = 50) -> list[Publication]:
        due = await self.dist.due(limit)
        out = []
        for pub in due:
            try:
                out.append(await self.publish_publication(pub.id))
            except Exception as exc:
                log.exception("publish_failed", publication=str(pub.id), error=str(exc)[:200])
        processing = await self._processing()
        for pub in processing:
            try:
                out.append(await self.dist.poll(pub.id))
            except Exception as exc:
                log.warning("poll_failed", publication=str(pub.id), error=str(exc)[:200])
        return out

    async def _processing(self) -> list[Publication]:
        from sqlalchemy import select

        from acvm.db.tables import PublicationRow

        async with self.ctx.uow() as repos:
            rows = (await repos.session.execute(select(PublicationRow).where(PublicationRow.state == "PROCESSING")
                                                .limit(100))).scalars().all()
            return [Publication.model_validate(r) for r in rows]

    async def on_review_approved(self, content_id: uuid.UUID) -> list[Publication]:
        """HOLD approved by a human → schedule distribution with the original plan's targets."""
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(content_id)
            existing = await repos.publications.by_content(content_id)
        if content is None or content.status is not ContentStatus.APPROVED or existing:
            return existing
        return await self.distribute(content_id, run_id=content.created_by_run)

    # ───────────────────────── learning loop ─────────────────────────

    async def amplify(self, event: Event) -> list[ProductResult]:
        """M10 amplification (§4 M10 증폭 규칙): 5 variants of the winning product/hook + cross-platform posts."""
        ev = event.parsed()
        assert isinstance(ev, AmplifyRequested)
        async with self.ctx.uow() as repos:
            parent = await repos.contents.get(ev.content_id)
            parent_run = await repos.runs.get(parent.created_by_run) if parent and parent.created_by_run else None
        if parent is None or parent_run is None or parent_run.plan is None:
            return []
        base = parent_run.plan
        async with self.ctx.uow() as repos:
            accounts = await repos.accounts.list(parent.workspace_id)
        from acvm.orchestration.planner import FORMAT_SHORT, PLACEMENT

        cross = []
        for p in ev.cross_platforms:
            acc = next((a for a in accounts if a.platform is p and a.status.value == "active"), None)
            if acc and (parent.kind is ContentKind.SHORT or p is Platform.THREADS):
                cross.append(PublishTarget(platform=p, account_id=acc.id, account_handle=acc.handle,
                                           link_placement=PLACEMENT[p],
                                           format="text" if parent.kind is ContentKind.TEXT else FORMAT_SHORT[p]))
        run_id = uuid7()
        plan = base.model_copy(update={"run_id": run_id, "count": ev.variants, "targets": base.targets + cross,
                                       "start_at": self.ctx.clock.now(),
                                       "explanation": f"증폭: {parent.id} 변형 {ev.variants}개 + 교차 배포"})
        run = CommandRun(id=run_id, workspace_id=parent.workspace_id, raw_command=f"amplify:{parent.id}", plan=plan,
                         status=RunStatus.APPROVED, approved_by="m10-amplify", started_at=self.ctx.clock.now())
        async with self.ctx.uow() as repos:
            await repos.runs.add(run)
            await audit(repos, self.ctx.clock, "m10", "amplify", str(parent.id), run=str(run_id))
        results = []
        for v in range(1, ev.variants + 1):
            results.append(await self.produce(run_id, parent.product_id, rank=v, parent=parent.id, variant_offset=v))
        await self.finalize_run(run_id, results)
        return results

    async def learn_hourly(self) -> dict[str, int]:
        n_ins = await self.analytics.collect_insights()
        updates = await self.analytics.attribute()
        alerts = await self.analytics.detect_invalid_clicks()
        await self.ctx.relay_outbox()
        return {"insights": n_ins, "perf_updates": len(updates), "invalid_click_alerts": len(alerts)}

    async def learn_daily(self, workspace_id: uuid.UUID) -> dict[str, int]:
        day = (self.ctx.clock.now() - timedelta(days=1)).date()
        n = await self.analytics.collect_reports(day)
        updates = await self.analytics.attribute()
        await self.ctx.relay_outbox()
        rules = await self.optimizer.refresh_playbook(workspace_id)
        return {"report_rows": n, "perf_updates": len(updates), "playbook_rules": len(rules)}


def wire_handlers(ctx: AppContext, pipeline: Pipeline) -> None:
    """Event subscriptions (§6.3 consumer column)."""

    async def m10_on_perf(ev: Event) -> None:
        await pipeline.optimizer.on_perf(ev)

    async def m2_on_perf(ev: Event) -> None:
        await pipeline.dna.on_perf(ev)

    async def m3_on_invalid(ev: Event) -> None:
        await pipeline.links.on_invalid_click(ev)

    async def on_amplify(ev: Event) -> None:
        await pipeline.amplify(ev)

    async def on_gate(ev: Event) -> None:
        payload = ev.payload
        if payload.get("verdict") == "PASS" and any(f.get("rule_id") == "HUMAN" for f in payload.get("findings", [])):
            await pipeline.on_review_approved(uuid.UUID(payload["content_id"]))

    ctx.bus.subscribe("perf.updated", "m10", m10_on_perf)
    ctx.bus.subscribe("perf.updated", "m2", m2_on_perf)
    ctx.bus.subscribe("invalid_click.detected", "m3", m3_on_invalid)
    ctx.bus.subscribe("amplify.requested", "orchestrator", on_amplify)
    ctx.bus.subscribe("gate.decided", "orchestrator", on_gate)


async def gather_limited(coros: list[Any], limit: int) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def run(c: Any) -> Any:
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))
