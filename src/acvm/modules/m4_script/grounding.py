"""Turn a draft (LLM or composer) into a grounded `Script` + `Claim` list (§4 M4 주장 근거화).

Pipeline: time beats to the template → verify every registered claim against facts → delete
ungrounded claims → soften replaceable superlatives → delete sentences with non-replaceable hits
(efficacy, fake experience, click-bait, guarantees) → delete factual sentences no claim covers →
refill emptied beats with safe lines. Every deletion is recorded so the gate and operators can see
what the engine refused to say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from acvm.core.enums import HookType
from acvm.domain.models import Claim, Script, ScriptBeat
from acvm.modules.m4_script.facts import ProductFacts
from acvm.modules.m4_script.templates import Template
from acvm.policy import claims as pc
from acvm.policy import lexicon as lx

SAFE_LINES = {
    "hook": "이거 한번 보세요",
    "problem": "매일 쓰는 물건인데 은근히 불편하죠",
    "amplify": "작은 불편이라도 매일 쌓이면 스트레스예요",
    "reveal": "그래서 소개하는 게 바로 이 제품이에요",
    "benefits": "자세한 구성은 상품 페이지에서 확인해보세요",
    "cta": "제품 정보는 프로필 링크에서 확인하세요",
}


@dataclass
class DraftBeatIn:
    role: str
    narration: str
    on_screen_text: str
    visual: str
    claim_ids: list[str]


@dataclass
class DraftIn:
    hooks: list[str]
    chosen_hook: int
    hook_type: HookType
    beats: list[DraftBeatIn]
    claims: list[Claim]
    body: str
    caption: str
    hashtags: list[str]
    cta: str
    title: str


@dataclass
class GroundingReport:
    removed_sentences: list[str] = field(default_factory=list)
    softened: list[str] = field(default_factory=list)
    ungrounded_claims: list[str] = field(default_factory=list)


def _remove_sentence(text: str, sentence: str) -> str:
    out = text.replace(sentence, "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(" .\n")


def _clean_text(text: str, facts: ProductFacts, covered: list[str], report: GroundingReport) -> str:
    softened, _ = lx.soften(text)
    if softened != lx.normalize(text):
        report.softened.append(text)
    text = softened
    for sent in pc.split_sentences(text):
        hits = [h for h in lx.scan(sent) if h.replacement is None]
        spec_missing = lx.spec_required_hits(sent, set(facts.values))
        fake_ok = bool(facts.get("spec.operator_used"))
        hits = [h for h in hits if not (h.category == "fake_experience" and fake_ok)]
        if hits or spec_missing:
            report.removed_sentences.append(sent)
            text = _remove_sentence(text, sent)
    names = [str(facts.get("short_name", "")), str(facts.get("name", ""))]
    for sent in pc.uncovered_factual_sentences(text, covered, mask=names):
        report.removed_sentences.append(sent)
        text = _remove_sentence(text, sent)
    return text


def finalize(draft: DraftIn, *, facts: ProductFacts, template: Template, persona: str, disclosure: str,
             link_placement_cta: str) -> tuple[Script, list[Claim], GroundingReport]:
    report = GroundingReport()
    grounded: list[Claim] = []
    for c in draft.claims:
        if c.kind in ("opinion", "cta"):
            hits = [h for h in lx.scan(c.text) if h.replacement is None]
            if not hits and not pc.is_factual(c.text.replace(link_placement_cta, "")):
                grounded.append(c.model_copy(update={"grounded": True}))
                continue
        chk = pc.check_claim(c.text, list(c.evidence), facts.values)
        if chk.ok:
            grounded.append(c.model_copy(update={"grounded": True, "text": lx.soften(c.text)[0]}))
        else:
            report.ungrounded_claims.append(f"{c.text} — {'; '.join(chk.reasons)}")
    covered = [c.text for c in grounded]
    ungrounded_texts = [c.text for c in draft.claims if c.text not in covered]

    beats: list[ScriptBeat] = []
    specs = list(template.beats)
    by_role = {b.role: b for b in draft.beats}
    for spec in specs:
        b = by_role.get(spec.role) or by_role.get("problem" if spec.role == "amplify" else "amplify")
        narration = b.narration if b else ""
        screen = b.on_screen_text if b else ""
        for t in ungrounded_texts:
            narration = _remove_sentence(narration, t)
            screen = _remove_sentence(screen, t)
        narration = _clean_text(narration, facts, covered, report)
        screen = _clean_text(screen, facts, covered, report) if screen else ""
        if not narration:
            narration = link_placement_cta if spec.role == "cta" else SAFE_LINES.get(spec.role, "")
        ids = [cid for cid in (b.claim_ids if b else []) if any(g.id == cid for g in grounded)]
        beats.append(ScriptBeat(start_sec=spec.start, end_sec=spec.end, role=spec.role,
                                narration=narration, on_screen_text=screen, visual=spec.visual, claim_ids=ids))

    hooks = []
    for h in draft.hooks[:3]:
        cleaned = _clean_text(h, facts, covered, report)
        hooks.append(cleaned or SAFE_LINES["hook"])
    while len(hooks) < 3:
        hooks.append(SAFE_LINES["hook"])
    chosen = min(max(draft.chosen_hook, 0), 2)
    if beats and beats[0].role == "hook":
        beats[0] = beats[0].model_copy(update={"narration": hooks[chosen],
                                               "on_screen_text": beats[0].on_screen_text or hooks[chosen]})

    body = draft.body
    for t in ungrounded_texts:
        body = _remove_sentence(body, t)
    body = "\n".join(_clean_text(line, facts, covered, report) if line.strip() else ""
                     for line in body.split("\n")).strip()
    caption = _clean_text(draft.caption, facts, covered, report)
    title = _clean_text(draft.title, facts, covered, report) or hooks[chosen]
    tags = [re.sub(r"[^\w가-힣]", "", t.lstrip("#")) for t in draft.hashtags]
    tags = [t for t in tags if t and not lx.scan(t)]
    if "광고" not in tags:
        tags.insert(0, "광고")

    script = Script(
        hooks=hooks, chosen_hook=chosen, hook_type=draft.hook_type, beats=beats, body=body, caption=caption,
        hashtags=tags[:8], cta=link_placement_cta, disclosure=disclosure, title=title[:100],
        duration_sec=template.duration, persona=persona, template_id=template.id)
    return script, grounded, report
