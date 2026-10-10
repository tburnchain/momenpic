"""M4 Script Engine (§4 M4).

``generate`` is idempotent per (run, product, variant, account): a retried activity returns the content
created by the first attempt. Inside one call the engine tries up to ``MAX_ATTEMPTS`` drafts, rotating
hook type and phrase seeds, until the draft is (a) fully grounded and (b) below the account
self-similarity bar. The writer is the LLM when configured, else the deterministic composer; the
Critic always runs (LLM large tier when configured, rules otherwise) and its deletions are applied
before the gate ever sees the script.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from acvm.context import AppContext
from acvm.core.enums import ContentKind, ContentStatus, HookType, Platform
from acvm.core.errors import NotFound, ValidationFailed
from acvm.core.events import Event, ScriptReady
from acvm.core.ids import activity_key
from acvm.core.logging import get_logger
from acvm.domain.models import Claim, Content, Product, Script
from acvm.modules.m4_script import agents as ag
from acvm.modules.m4_script.composer import CTA, ComposedScript, compose
from acvm.modules.m4_script.diversity import max_similarity, script_text
from acvm.modules.m4_script.facts import ProductFacts, build_facts
from acvm.modules.m4_script.grounding import SAFE_LINES, DraftBeatIn, DraftIn, GroundingReport, finalize
from acvm.modules.m4_script.templates import (
    DEFAULT_TEMPLATE,
    SHORT_MAX_SECONDS,
    TEMPLATES,
    Persona,
    Template,
    estimate_seconds,
    resolve_persona,
)
from acvm.policy import claims as pc
from acvm.security.prompt_guard import data_block

log = get_logger(__name__)
MAX_ATTEMPTS = 4
HOOK_ROTATION = (HookType.QUESTION, HookType.EMPATHY, HookType.PRICE_TWIST, HookType.SHOCK, HookType.BEFORE_AFTER)


@dataclass
class ScriptRequest:
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: int
    kind: ContentKind
    persona: str
    platform: Platform
    link_placement: str
    target_account_id: uuid.UUID | None = None
    template_id: str | None = None
    hook_type: HookType | None = None
    variant_index: int = 0
    dna_id: uuid.UUID | None = None
    parent_content_id: uuid.UUID | None = None
    budget_cap_krw: float | None = None
    playbook: list[str] = field(default_factory=list)


class ScriptEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ───────────────────────── public API ─────────────────────────

    async def generate(self, req: ScriptRequest) -> Content:
        key = activity_key(req.run_id, req.product_id, "script",
                           {"v": req.variant_index, "acct": str(req.target_account_id), "kind": req.kind.value,
                            "persona": req.persona, "tmpl": req.template_id, "hook": req.hook_type,
                            "parent": str(req.parent_content_id)})
        async with self.ctx.uow() as repos:
            cached = await repos.activities.get(key)
            if cached:
                existing = await repos.contents.get(uuid.UUID(cached["content_id"]))
                if existing:
                    return existing
            product = await repos.products.get(req.product_id)
            if product is None:
                raise NotFound(f"product {req.product_id}")
            dna_hints = await repos.dna.top(req.workspace_id, limit=3,
                                            hook_type=req.hook_type.value if req.hook_type else None)
            playbook = req.playbook or [r.text for r in await repos.playbook.active(req.workspace_id, limit=8)]

        template = self._template(req)
        persona = resolve_persona(req.persona)
        facts = build_facts(product, self.ctx.clock.now(), self.ctx.settings.gate.price_fresh_minutes)
        cta_line = CTA.get(req.link_placement, CTA["hub"])[0]
        disclosure = self.ctx.settings.gate.standard_disclosure

        best: tuple[float, Content, GroundingReport] | None = None
        for attempt in range(MAX_ATTEMPTS):
            hook_type = self._hook_for(req, template, attempt)
            rng = self.ctx.rng("script", req.run_id, req.product_id, req.variant_index, req.target_account_id, attempt)
            draft = await self._draft(req, product, facts, template, persona, hook_type, cta_line, attempt,
                                      dna_hints=[d.first3s_pattern for d in dna_hints], playbook=playbook, rng=rng)
            script, claims, report = finalize(draft, facts=facts, template=template, persona=persona.name,
                                              disclosure=disclosure, link_placement_cta=cta_line)
            script, claims = await self._critic(script, claims, facts, report)
            script, claims = fit_short_length(script, claims, template)
            text = script_text(script.hook, script.narration, script.body, script.caption)
            sim, _ = await max_similarity(self.ctx, workspace_id=req.workspace_id, account_id=req.target_account_id,
                                          text=text)
            content = Content(
                workspace_id=req.workspace_id, product_id=product.id, dna_id=req.dna_id or (dna_hints[0].id if dna_hints else None),
                kind=req.kind, persona=persona.name, hook_variant=f"{hook_type.value}:{attempt}",
                template_id=template.id, script=script, claims=claims, status=ContentStatus.DRAFT,
                similarity_max=sim, created_by_run=req.run_id, target_account_id=req.target_account_id,
                parent_content_id=req.parent_content_id, created_at=self.ctx.clock.now())
            if best is None or sim < best[0]:
                best = (sim, content, report)
            if sim <= self.ctx.settings.gate.account_minhash_max:
                break
            log.info("script_regenerate_similarity", sim=round(sim, 3), attempt=attempt, product=product.id)
        assert best is not None
        sim, content, report = best
        embedding = (await self.ctx.embedder.embed([script_text(content.script.hook, content.script.narration,
                                                                content.script.body, content.script.caption)]))[0]
        async with self.ctx.uow() as repos:
            await self.ctx.charge(repos, workspace_id=req.workspace_id, run_id=req.run_id, stage="script",
                                  amount_krw=self._cost(), key=f"{key}:cost", cap_krw=req.budget_cap_krw,
                                  content_id=content.id)
            await repos.contents.add(content, embedding=embedding)
            await repos.activities.put(key, "script", {"content_id": str(content.id),
                                                       "removed": report.removed_sentences[:20],
                                                       "ungrounded": report.ungrounded_claims[:20]},
                                       self.ctx.clock.now())
            await self.ctx.emit(repos, Event.of(
                ScriptReady(content_id=content.id, claims=[c.model_dump(mode="json") for c in content.claims],
                            persona=content.persona),
                workspace_id=req.workspace_id, run_id=req.run_id, idempotency_key=f"{content.id}:script.ready"))
        return content

    # ───────────────────────── internals ─────────────────────────

    def _template(self, req: ScriptRequest) -> Template:
        tid = req.template_id or DEFAULT_TEMPLATE[req.kind]
        tmpl = TEMPLATES.get(tid)
        if tmpl is None:
            raise ValidationFailed(f"unknown template {tid}")
        if tmpl.kind is not req.kind:
            tmpl = TEMPLATES[DEFAULT_TEMPLATE[req.kind]]
        return tmpl

    @staticmethod
    def _hook_for(req: ScriptRequest, template: Template, attempt: int) -> HookType:
        base = req.hook_type or template.preferred_hooks[req.variant_index % len(template.preferred_hooks)]
        if attempt == 0:
            return base
        rotation = [h for h in HOOK_ROTATION if h is not base]
        return rotation[(attempt - 1 + req.variant_index) % len(rotation)]

    def _cost(self) -> float:
        c = self.ctx.settings.costs
        return (c.llm_per_script_krw + c.llm_per_critic_krw) if self.ctx.llm is not None else 0.0

    async def _draft(self, req: ScriptRequest, product: Product, facts: ProductFacts, template: Template,
                     persona: Persona, hook_type: HookType, cta_line: str, attempt: int, *, dna_hints: list[str],
                     playbook: list[str], rng: object) -> DraftIn:
        if self.ctx.llm is not None:
            try:
                return await self._llm_draft(req, facts, template, persona, hook_type, cta_line, attempt,
                                             dna_hints=dna_hints, playbook=playbook)
            except Exception as exc:
                log.warning("writer_llm_failed_fallback", error=str(exc)[:200])
        composed = compose(facts=facts, category_id=product.category_id, template=template, persona=persona,
                           hook_type=hook_type, link_placement=req.link_placement, rng=rng)  # type: ignore[arg-type]
        return _from_composed(composed)

    async def _llm_draft(self, req: ScriptRequest, facts: ProductFacts, template: Template, persona: Persona,
                         hook_type: HookType, cta_line: str, attempt: int, *, dna_hints: list[str],
                         playbook: list[str]) -> DraftIn:
        assert self.ctx.llm is not None
        beats_spec = "\n".join(f"- role={b.role} {b.start:.0f}~{b.end:.0f}s visual={b.visual} 음절예산≈{b.syllable_budget} : "
                               f"{b.purpose}" for b in template.beats) or "- 텍스트 게시물: 훅 1줄, 공감 2줄, 근거 3개(✔), CTA"
        user = "\n\n".join([
            f"콘텐츠 종류: {req.kind.value} / 템플릿: {template.id} — {template.description}",
            f"비트 구조:\n{beats_spec}",
            f"훅 유형: {hook_type.value} (시도 {attempt + 1})",
            f"페르소나: {persona.name} — 톤 {persona.tone}, 말투 {persona.register}",
            f"링크 위치 안내 CTA(그대로 사용): {cta_line}",
            data_block("product_facts", facts.as_prompt_lines()),
            data_block("structure_hints_from_top_content", "\n".join(dna_hints) or "(없음)"),
            data_block("playbook_rules", "\n".join(playbook) or "(없음)"),
        ])
        draft = await self.ctx.llm.structured(tier="medium", system=ag.WRITER_SYSTEM, user=user, schema=ag.ScriptDraft,
                                              effort="medium", purpose="writer")
        return DraftIn(
            hooks=list(draft.hooks), chosen_hook=draft.chosen_hook, hook_type=HookType(draft.hook_type),
            beats=[DraftBeatIn(b.role, b.narration, b.on_screen_text, b.visual, list(b.claim_ids)) for b in draft.beats],
            claims=[Claim(id=c.id, text=c.text, kind=c.kind, evidence=list(c.evidence)) for c in draft.claims],
            body=draft.body, caption=draft.caption, hashtags=list(draft.hashtags), cta=cta_line, title=draft.title)

    async def _critic(self, script, claims: list[Claim], facts: ProductFacts,  # type: ignore[no-untyped-def]
                      report: GroundingReport):
        """Critic pass. Rules always run; the LLM critic (large tier, different model) adds judgement."""
        deletions: set[str] = set()
        for c in claims:
            if c.kind not in ("opinion", "cta") and not pc.check_claim(c.text, c.evidence, facts.values).ok:
                deletions.add(c.text)
        if self.ctx.llm is not None:
            try:
                user = "\n\n".join([
                    data_block("product_facts", facts.as_prompt_lines()),
                    data_block("claims", "\n".join(f"{c.id}: {c.text}  evidence={c.evidence}" for c in claims)),
                    data_block("script", "\n".join([script.hook, script.narration, script.body, script.caption])),
                ])
                rep = await self.ctx.llm.structured(tier="large", system=ag.CRITIC_SYSTEM, user=user,
                                                    schema=ag.CriticReport, effort="high", purpose="critic")
                bad_ids = {j.claim_id for j in rep.judgements if j.verdict in ("unsupported", "exaggerated")}
                deletions |= {c.text for c in claims if c.id in bad_ids}
                deletions |= {i.sentence for i in rep.issues}
            except Exception as exc:
                log.warning("critic_llm_failed", error=str(exc)[:200])
        if not deletions:
            return script, claims
        report.removed_sentences.extend(sorted(deletions))
        beats = []
        for b in script.beats:
            narration, screen, touched = b.narration, b.on_screen_text, False
            for d in deletions:
                if d and d in narration:
                    narration, touched = narration.replace(d, ""), True
                if d and d in screen:
                    screen = screen.replace(d, "")
            narration = re.sub(r"\s{2,}", " ", narration).strip(" .")
            if touched and not narration:
                narration = SAFE_LINES.get(b.role, "")
            beats.append(b.model_copy(update={"narration": narration, "on_screen_text": screen.strip(" .")}))
        body = script.body
        for d in deletions:
            body = body.replace(d, "")
        lines = [line for line in body.split("\n") if line.strip() != "✔"]
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        kept = [c for c in claims if c.text not in deletions]
        return script.model_copy(update={"beats": beats, "body": body.strip()}), kept


def _from_composed(c: ComposedScript) -> DraftIn:
    """Composer output → DraftIn: every composed claim is registered once; beats reference them by text."""
    claims: list[Claim] = []
    id_by_text: dict[str, str] = {}
    for i, cl in enumerate(c.claims, start=1):
        if cl.text in id_by_text:
            continue
        cid = f"c{i}"
        id_by_text[cl.text] = cid
        claims.append(Claim(id=cid, text=cl.text, kind=cl.kind, evidence=list(cl.evidence)))
    beats = [DraftBeatIn(b.role, b.narration, b.on_screen_text, b.visual,
                         [id_by_text[cl.text] for cl in b.claims if cl.text in id_by_text]) for b in c.beats]
    return DraftIn(hooks=c.hooks, chosen_hook=0, hook_type=c.hook_type, beats=beats, claims=claims, body=c.body,
                   caption=c.caption, hashtags=c.hashtags, cta=c.cta, title=c.title)


def fit_short_length(script: Script, claims: list[Claim], template: Template) -> tuple[Script, list[Claim]]:
    """Keep a short inside the 20–25 s window at the fast delivery rate: trim the benefits beat sentence by
    sentence (last first) until the spoken estimate fits. Hook, problem, reveal and CTA are never trimmed."""
    if template.kind is not ContentKind.SHORT or not script.beats:
        return script, claims
    beats = list(script.beats)
    idx = next((i for i, b in enumerate(beats) if b.role == "benefits"), None)
    while idx is not None and estimate_seconds([b.narration for b in beats]) > SHORT_MAX_SECONDS - 0.3:
        sentences = pc.split_sentences(beats[idx].narration)
        if len(sentences) <= 1:
            break
        kept = " ".join(s if s.endswith((".", "!", "?")) else s + "." for s in sentences[:-1])
        beats[idx] = beats[idx].model_copy(update={"narration": kept})
    text = " ".join(b.narration for b in beats)
    kept_claims = [c for c in claims if c.kind in ("cta", "opinion") or c.text.rstrip(".") in text
                   or c.text in script.body or c.text in script.caption]
    return script.model_copy(update={"beats": beats}), kept_claims
