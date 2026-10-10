"""Shorts Studio — the reference product's five manual steps, automated (설계서 §1 #9: "수동 GUI 단계를
API 파라미터로 자동화").

    ① 영상소스   M5 rights ledger: own footage first, then the product image, licensed stock, generated cards
    ② 대본       M4 grounded script (3 hooks) — editable; every edit is linted and auto-grounded live
    ③ 워터마크   account handle by default
    ④ 보이스     persona → voice (M6 catalog); overridable
    ⑤ 자막       style / position / on-off; the 광고 disclosure band is locked and cannot be turned off

`auto()` runs all five plus render → gate → schedule with no human input ("전부 자동");
every intermediate state is still inspectable and editable in the console.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from acvm import integrations
from acvm.context import AppContext
from acvm.core.enums import (
    AccountStatus,
    ContentKind,
    ContentStatus,
    HookType,
    Intent,
    Platform,
    RunStatus,
    Verdict,
)
from acvm.core.errors import NotFound, PolicyViolation, ValidationFailed
from acvm.core.ids import uuid7
from acvm.core.logging import get_logger
from acvm.domain.models import (
    Approval,
    Claim,
    CommandRun,
    CommandSlots,
    Content,
    Plan,
    PlanStep,
    Product,
    PublishTarget,
)
from acvm.modules.m4_script.service import ScriptRequest
from acvm.modules.m4_script.templates import DEFAULT_TEMPLATE, TEMPLATES, estimate_seconds, resolve_persona
from acvm.orchestration.pipeline import Pipeline
from acvm.orchestration.planner import FORMAT_SHORT, PLACEMENT, TEXT_CAPABLE
from acvm.policy.facts import build_facts
from acvm.policy.lint import LintResult, is_covered, lint_text
from acvm.security.audit import audit

log = get_logger(__name__)
RENDER_STATE_TTL = timedelta(days=7)
AUTOLOAD_CATEGORIES = ("1013", "1018", "1014")  # seeded niches (kitchen · car · storage) for the "all" tab
OPTION_KEYS = {"voice_id", "caption_style", "caption_position", "captions_enabled", "watermark", "beat_assets"}


class Studio:
    def __init__(self, ctx: AppContext, pipeline: Pipeline) -> None:
        self.ctx = ctx
        self.p = pipeline
        self._tasks: set[asyncio.Task[Any]] = set()

    # ───────────────────────── products ─────────────────────────

    async def products(self, workspace_id: uuid.UUID, *, category_id: str | None, q: str | None,
                       limit: int = 30, refresh: bool = False) -> list[dict[str, Any]]:
        """Candidate products with their latest POS. ``refresh`` asks M1 for fresh candidates (uses the
        rate-limited Coupang budget, so the console only does it on explicit request).

        First run: a category (or, for the "all" tab, the seeded niche categories) with no stored product at all is
        loaded once automatically, so a fresh install never greets the operator with an empty list. The attempt is
        remembered for an hour (KV) — a category that is genuinely empty does not spend the Coupang budget again.
        """
        if refresh and category_id:
            await self._select(workspace_id, category_id, limit, q)
        out = await self._stored_products(category_id, q)
        if not out and not refresh and not q:
            for cat in ([category_id] if category_id else list(AUTOLOAD_CATEGORIES)):
                if await self._autoload_once(workspace_id, cat, limit):
                    out = await self._stored_products(category_id, q)
        out.sort(key=lambda x: -float(x["pos"] or 0.0))
        return out[:limit]

    async def _select(self, workspace_id: uuid.UUID, category_id: str, limit: int, q: str | None) -> None:
        await self.p.products.select(run_id=uuid7(), workspace_id=workspace_id, category_id=category_id,
                                     n=limit, min_pos=0.0, keyword=q)

    async def _autoload_once(self, workspace_id: uuid.UUID, category_id: str, limit: int) -> bool:
        key = f"studio:catalog_autoload:{category_id}"
        async with self.ctx.uow() as repos:
            if await repos.kv.get(key, self.ctx.clock.now()):
                return False
            await repos.kv.put(key, {"at": self.ctx.clock.now().isoformat()}, timedelta(hours=1), self.ctx.clock.now())
        try:
            await self._select(workspace_id, category_id, limit, None)
        except Exception as exc:  # quota/breaker/outage: the operator can still press 새로고침 later
            log.warning("catalog_autoload_failed", category=category_id, error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return False
        log.info("catalog_autoloaded", category=category_id)
        return True

    async def _stored_products(self, category_id: str | None, q: str | None) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from acvm.db import tables as t

        async with self.ctx.uow() as repos:
            stmt = select(t.ProductRow).where(t.ProductRow.regulated.is_(False))
            if category_id:
                stmt = stmt.where(t.ProductRow.category_id == category_id)
            if q:
                stmt = stmt.where(t.ProductRow.name.contains(q))
            rows = (await repos.session.execute(stmt.order_by(t.ProductRow.last_seen_at.desc()).limit(200))).scalars().all()
            out: list[dict[str, Any]] = []
            for r in rows:
                score = await repos.products.latest_score(r.id)
                out.append({"id": r.id, "name": r.name, "price": r.price, "image_url": r.image_url,
                            "category_id": r.category_id, "category_name": r.category_name, "is_rocket": r.is_rocket,
                            "rating": r.rating, "review_count": r.review_count,
                            "pos": round(score.pos, 3) if score else None,
                            "price_checked_at": r.price_checked_at.isoformat() if r.price_checked_at else None})
        return out

    # ───────────────────────── drafts ─────────────────────────

    async def _account(self, workspace_id: uuid.UUID, account_id: uuid.UUID | None, platform: Platform | None,
                       niche_category: str) -> Any:
        from acvm.agents.commander import resolve_category

        async with self.ctx.uow() as repos:
            accounts = [a for a in await repos.accounts.list(workspace_id) if a.status is AccountStatus.ACTIVE]
        if account_id is not None:
            acc = next((a for a in accounts if a.id == account_id), None)
            if acc is None:
                raise NotFound("active account not found")
            return acc
        pool = [a for a in accounts if (platform is None or a.platform is platform)]
        same_niche = [a for a in pool if resolve_category(a.niche) == niche_category]
        candidates = same_niche or pool
        return candidates[0] if candidates else None

    async def create_draft(self, workspace_id: uuid.UUID, *, actor: str, product_id: int,
                           kind: ContentKind = ContentKind.SHORT, persona: str | None = None,
                           account_id: uuid.UUID | None = None, platform: Platform | None = None,
                           template_id: str | None = None, hook_type: HookType | None = None) -> Content:
        """① + ② + ③ + ④ + ⑤ auto-filled: one grounded script and default render options."""
        await integrations.sync_llm(self.ctx)
        async with self.ctx.uow() as repos:
            product = await repos.products.get(product_id)
        if product is None:
            raise NotFound(f"product {product_id}")
        if product.regulated:
            raise PolicyViolation("규제 카테고리 상품은 제작할 수 없습니다 (C-04)")
        if kind is ContentKind.TEXT and platform is not None and platform not in TEXT_CAPABLE:
            raise ValidationFailed(f"{platform.value}는 영상만 게시할 수 있습니다")
        acc = await self._account(workspace_id, account_id, platform, product.category_id)
        persona_obj = resolve_persona(persona or (acc.persona_default if acc else None))
        tmpl = template_id if template_id in TEMPLATES and TEMPLATES[template_id].kind is kind else DEFAULT_TEMPLATE[kind]
        run = await self._studio_run(workspace_id, actor, product, kind, persona_obj.name, tmpl, acc)
        content = await self.p.scripts.generate(ScriptRequest(
            run_id=run.id, workspace_id=workspace_id, product_id=product.id, kind=kind, persona=persona_obj.name,
            platform=acc.platform if acc else Platform.THREADS,
            link_placement=PLACEMENT[acc.platform] if acc else "hub", target_account_id=acc.id if acc else None,
            template_id=tmpl, hook_type=hook_type, budget_cap_krw=run.plan.budget_cap_krw if run.plan else None))
        options = await self.default_options(content, acc)
        content = content.model_copy(update={"render_options": options})
        async with self.ctx.uow() as repos:
            await repos.contents.save(content)
            await audit(repos, self.ctx.clock, actor, "studio.draft", str(content.id), product=product.id)
        return content

    async def default_options(self, content: Content, account: Any) -> dict[str, Any]:
        from acvm.media.voices import voice_id_for_persona

        voice_id = voice_id_for_persona(content.persona)
        return {"voice_id": voice_id or None, "caption_style": "classic", "caption_position": "bottom",
                "captions_enabled": True, "watermark": f"@{account.handle}" if account else "", "beat_assets": {}}

    async def _studio_run(self, workspace_id: uuid.UUID, actor: str, product: Product, kind: ContentKind,
                          persona: str, template_id: str, account: Any) -> CommandRun:
        s = self.ctx.settings
        targets = []
        if account is not None:
            targets.append(PublishTarget(platform=account.platform, account_id=account.id, account_handle=account.handle,
                                         link_placement=PLACEMENT[account.platform],
                                         format="text" if kind is ContentKind.TEXT else FORMAT_SHORT[account.platform]))
        run_id = uuid7()
        est = s.economics.c_unit_short_krw if kind is ContentKind.SHORT else s.economics.c_unit_text_krw
        plan = Plan(run_id=run_id, workspace_id=workspace_id, intent=Intent.CREATE_AND_PUBLISH,
                    slots=CommandSlots(intent=Intent.CREATE_AND_PUBLISH, category=product.category_id, count=1, kind=kind,
                                       persona=persona, platforms=[t.platform for t in targets]),
                    category_id=product.category_id, niche=account.niche if account else "", count=1, kind=kind,
                    persona=persona, template_id=template_id, targets=targets, est_cost_krw=est,
                    budget_cap_krw=max(est * 10, 1_000.0), start_at=self.ctx.clock.now(),
                    approval=Approval(mode="console", reasons=[f"스튜디오 작업 ({actor})"]),
                    steps=[PlanStep(id="studio", module="M4", action="studio_draft", params={"product_id": product.id})],
                    explanation=f"스튜디오: {product.name}")
        run = CommandRun(id=run_id, workspace_id=workspace_id, raw_command=f"studio: {product.name}", plan=plan,
                         status=RunStatus.RUNNING, approved_by=actor, started_at=self.ctx.clock.now())
        async with self.ctx.uow() as repos:
            await repos.runs.add(run)
        return run

    async def get(self, content_id: uuid.UUID, workspace_id: uuid.UUID) -> Content:
        async with self.ctx.uow() as repos:
            c = await repos.contents.get(content_id)
        if c is None or c.workspace_id != workspace_id:
            raise NotFound("draft not found")
        return c

    async def lint(self, product_id: int, text: str) -> LintResult:
        async with self.ctx.uow() as repos:
            product = await repos.products.get(product_id)
        if product is None:
            raise NotFound(f"product {product_id}")
        facts = build_facts(product, self.ctx.clock.now(), self.ctx.settings.gate.price_fresh_minutes)
        return lint_text(text, facts)

    async def update(self, content_id: uuid.UUID, workspace_id: uuid.UUID, edits: dict[str, Any],
                     actor: str) -> tuple[Content, LintResult]:
        """Apply editor changes; re-ground every edited sentence; invalidate the render if anything changed."""
        c = await self.get(content_id, workspace_id)
        if c.status in (ContentStatus.APPROVED, ContentStatus.BLOCKED):
            raise ValidationFailed("승인·차단된 콘텐츠는 수정할 수 없습니다 — 새 초안을 만드세요")
        sc = c.script
        beats = list(sc.beats)
        for i, b in enumerate(edits.get("beats") or []):
            if i >= len(beats) or b is None:
                continue
            beats[i] = beats[i].model_copy(update={k: str(v)[:400] for k, v in b.items()
                                                   if k in ("narration", "on_screen_text") and v is not None})
        hooks = [str(h)[:120] for h in edits.get("hooks") or sc.hooks][:3]
        chosen = int(edits.get("chosen_hook", sc.chosen_hook))
        if not 0 <= chosen < len(hooks):
            raise ValidationFailed("chosen_hook out of range")
        if beats and beats[0].role == "hook" and ("hooks" in edits or "chosen_hook" in edits):
            beats[0] = beats[0].model_copy(update={"narration": hooks[chosen]})
        script = sc.model_copy(update={
            "beats": beats, "hooks": hooks, "chosen_hook": chosen,
            "caption": str(edits.get("caption", sc.caption))[:2000], "body": str(edits.get("body", sc.body))[:4000],
            "title": str(edits.get("title", sc.title))[:100],
            "hashtags": [str(h).lstrip("#")[:30] for h in edits.get("hashtags", sc.hashtags)][:10]})
        options = dict(c.render_options)
        for k, v in (edits.get("render_options") or {}).items():
            if k not in OPTION_KEYS:
                raise ValidationFailed(f"unknown render option {k}")
            options[k] = v
        if options.get("watermark"):
            options["watermark"] = str(options["watermark"])[:24]
        text = "\n".join([*(b.narration for b in beats), *(b.on_screen_text for b in beats), script.body, script.caption])
        async with self.ctx.uow() as repos:
            product = await repos.products.get(c.product_id)
        assert product is not None
        facts = build_facts(product, self.ctx.clock.now(), self.ctx.settings.gate.price_fresh_minutes)
        lint = lint_text(text, facts)
        kept = [cl for cl in c.claims if cl.text in text or cl.kind in ("cta", "opinion")]
        claims: list[Claim] = list(kept)
        for cl in lint.claims:
            if not is_covered(cl.text, [k.text for k in claims]):
                claims.append(cl.model_copy(update={"id": f"s{len(claims) + 1}"}))
        changed = script != sc or options != c.render_options
        updated = c.model_copy(update={"script": script, "render_options": options, "claims": claims,
                                       **({"render": None, "status": ContentStatus.DRAFT} if changed else {})})
        async with self.ctx.uow() as repos:
            await repos.contents.save(updated)
            await audit(repos, self.ctx.clock, actor, "studio.edit", str(content_id),
                        fields=sorted(k for k in edits if k != "render_options"),
                        options=sorted((edits.get("render_options") or {}).keys()))
        return updated, lint

    # ───────────────────────── render ─────────────────────────

    async def render_state(self, content_id: uuid.UUID) -> dict[str, Any]:
        async with self.ctx.uow() as repos:
            st = await repos.kv.get_stale(f"studio:render:{content_id}")
        return st or {"state": "idle"}

    async def _set_state(self, content_id: uuid.UUID, **state: Any) -> None:
        async with self.ctx.uow() as repos:
            await repos.kv.put(f"studio:render:{content_id}", {**state, "at": self.ctx.clock.now().isoformat()},
                               RENDER_STATE_TTL, self.ctx.clock.now())

    def assert_fits(self, c: Content) -> None:
        """Shorts are 20–25 s product pitches at the fast delivery rate: a longer script is refused up front."""
        est = estimate_seconds([b.narration for b in c.script.beats])
        limit = self.ctx.settings.render.max_seconds
        if est > limit:
            raise ValidationFailed(f"대본이 너무 깁니다: 예상 {est:.1f}초 (쇼츠는 {limit:g}초 이내, 빠른 속도 기준) "
                                   "— 문장을 줄여 주세요", estimated_seconds=round(est, 1), limit=limit)

    async def render_now(self, content_id: uuid.UUID, workspace_id: uuid.UUID) -> Content:
        c = await self.get(content_id, workspace_id)
        if c.kind is not ContentKind.SHORT:
            raise ValidationFailed("글 콘텐츠는 렌더가 필요 없습니다")
        self.assert_fits(c)
        await self._set_state(content_id, state="rendering")
        try:
            out = await self.p._render(c)
        except Exception as exc:
            await self._set_state(content_id, state="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            raise
        await self._set_state(content_id, state="done", media_uri=out.render.media_uri if out.render else None)
        return out

    def render_in_background(self, content_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        async def go() -> None:
            try:
                await self.render_now(content_id, workspace_id)
            except Exception:
                log.exception("studio_render_failed", content=str(content_id))

        task = asyncio.create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ───────────────────────── gate + publish ─────────────────────────

    async def publish(self, content_id: uuid.UUID, workspace_id: uuid.UUID, *, actor: str,
                      targets: list[dict[str, Any]] | None = None, auto_fix: bool = True) -> dict[str, Any]:
        """Gate (M7) → on PASS schedule (M3 links + M8). FIX actions are performed automatically (bounded)."""
        c = await self.get(content_id, workspace_id)
        # validate + persist the chosen targets on the run *before* the gate: if the verdict is HOLD, the later
        # human approval schedules from the run plan (pipeline.on_review_approved), so the choice must live there
        tgs = await self._targets(workspace_id, c, targets)
        if targets and c.created_by_run:
            async with self.ctx.uow() as repos:
                run = await repos.runs.get(c.created_by_run)
                if run is not None and run.plan is not None:
                    await repos.runs.update(run.id, plan=run.plan.model_copy(update={"targets": tgs}))
        if c.kind is ContentKind.SHORT and c.render is None:
            c = await self.render_now(content_id, workspace_id)
        outcome = await self.p.gate.evaluate(content_id, run_id=c.created_by_run)
        rounds = 0
        while auto_fix and outcome.verdict is Verdict.FIX and rounds < 2 and not outcome.actions <= {"reschedule"}:
            rounds += 1
            if "rerender" in outcome.actions and outcome.content is not None:
                await self.p._render(outcome.content, force=True)
            elif "regenerate" in outcome.actions:
                break  # a rewrite is the operator's call in the Studio — surface it instead of silently replacing
            outcome = await self.p.gate.evaluate(content_id, run_id=c.created_by_run)
        result: dict[str, Any] = {"verdict": outcome.verdict.value, "actions": sorted(outcome.actions),
                                  "findings": [f.model_dump(mode="json") for f in outcome.check.findings],
                                  "publications": []}
        approved = outcome.verdict is Verdict.PASS or (outcome.verdict is Verdict.FIX and outcome.actions <= {"reschedule"})
        if approved:
            pubs = await self.p.distribute(content_id, run_id=c.created_by_run, targets=tgs)
            result["publications"] = [{"id": str(p.id), "platform": p.platform.value, "state": p.state.value,
                                       "scheduled_at": p.scheduled_at.isoformat()} for p in pubs]
            if c.created_by_run:
                await self.p.mark(c.created_by_run, RunStatus.COMPLETED, studio={"verdict": outcome.verdict.value})
        async with self.ctx.uow() as repos:
            await audit(repos, self.ctx.clock, actor, "studio.publish", str(content_id), verdict=outcome.verdict.value,
                        publications=len(result["publications"]))
        await self.ctx.relay_outbox()
        return result

    async def _targets(self, workspace_id: uuid.UUID, c: Content, requested: list[dict[str, Any]] | None) -> list[PublishTarget]:
        async with self.ctx.uow() as repos:
            accounts = {a.id: a for a in await repos.accounts.list(workspace_id)}
            run = await repos.runs.get(c.created_by_run) if c.created_by_run else None
        if not requested:
            return list(run.plan.targets) if run and run.plan else []
        chosen = [accounts.get(uuid.UUID(str(r["account_id"]))) for r in requested]
        platforms = [a.platform for a in chosen if a is not None]
        if len(platforms) != len(set(platforms)):
            raise ValidationFailed("한 콘텐츠는 플랫폼마다 한 계정에만 게시할 수 있습니다 (설계서 §7.1 #4: 계정 간 동일 콘텐츠 동시 게시 금지)")
        out = []
        for r in requested:
            acc = accounts.get(uuid.UUID(str(r["account_id"])))
            if acc is None or acc.status is not AccountStatus.ACTIVE:
                raise ValidationFailed("대상 계정이 없거나 비활성입니다", account_id=str(r.get("account_id")))
            if c.kind is ContentKind.TEXT and acc.platform not in TEXT_CAPABLE:
                raise ValidationFailed(f"{acc.platform.value}에는 글을 게시할 수 없습니다")
            out.append(PublishTarget(platform=acc.platform, account_id=acc.id, account_handle=acc.handle,
                                     link_placement=PLACEMENT[acc.platform],
                                     format="text" if c.kind is ContentKind.TEXT else FORMAT_SHORT[acc.platform]))
        return out

    # ───────────────────────── 전부 자동 ─────────────────────────

    async def auto(self, workspace_id: uuid.UUID, *, actor: str, product_id: int, kind: ContentKind = ContentKind.SHORT,
                   persona: str | None = None, targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Every Studio step with no human input: draft → render → gate (auto-fix) → schedule."""
        first_account = None
        if targets:
            first_account = uuid.UUID(str(targets[0]["account_id"]))
        c = await self.create_draft(workspace_id, actor=actor, product_id=product_id, kind=kind, persona=persona,
                                    account_id=first_account)
        result = await self.publish(c.id, workspace_id, actor=actor, targets=targets)
        result["content_id"] = str(c.id)
        return result
