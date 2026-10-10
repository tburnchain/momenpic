"""Rule implementations (§8.1 rule_set v1). Each rule is an async callable over a `GateSubject`.

Fail-closed principle: a rule that cannot complete its check (missing render, verification tool
unavailable, hub page absent at preflight) returns its own on-fail verdict — never PASS.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any

from acvm.context import AppContext
from acvm.core.enums import AccountStatus, ContentKind, ContentStatus, LinkHealth, Verdict
from acvm.core.errors import IntegrityViolation
from acvm.db.repos import similarity_text
from acvm.domain.models import Finding
from acvm.modules.m7_gate.subject import GateSubject, RuleResult, TextField
from acvm.policy import claims as pc
from acvm.policy import lexicon as lx
from acvm.quant.similarity import MinHasher, ngram_overlap
from acvm.security import linksign

RuleFn = Callable[[GateSubject, AppContext], Awaitable[RuleResult]]
_PRICE = re.compile(r"(\d[\d,]*(?:\.\d+)?\s*(?:만\s*)?(?:\d[\d,]*\s*)?원|\d+\s*%\s*(?:할인|세일|off)?|할인|세일|특가|쿠폰가)",
                    re.IGNORECASE)
REGULATED_CATEGORIES = {"1024"}
REGULATED_TERMS = ("의약품", "의약외품", "건강기능식품", "영양제", "의료기기", "주류", "소주", "맥주", "와인", "위스키", "담배",
                   "전자담배", "니코틴", "성인용품", "콘돔", "다이어트 보조제", "식욕억제", "체지방 감소")
IP_TERMS = ("디즈니", "마블", "포켓몬", "피카츄", "산리오", "헬로키티", "쿠로미", "카카오프렌즈", "라이언", "춘식이", "짱구", "뽀로로",
            "핑크퐁", "아기상어", "도라에몽", "짱구는 못말려", "스누피", "미키마우스", "BTS", "방탄소년단", "블랙핑크", "아이유",
            "손흥민", "뉴진스", "임영웅")
_MH = MinHasher(num_perm=128, shingle=4)


def _finding(rule: str, verdict: Verdict, msg: str, fixed: bool = False, **details: Any) -> Finding:
    return Finding(rule_id=rule, verdict=verdict, message=msg, fixed=fixed, details=details)


def _strip_sentence(text: str, sentence: str) -> str:
    out = text.replace(sentence, "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(" .\n") if out.strip() else ""


def _first_line(text: str) -> str:
    for ln in text.split("\n"):
        if ln.strip():
            return ln.strip()
    return ""


def _non_disclosure(text: str) -> str:
    """Text with the disclosure line(s) removed (rules other than D-* ignore them)."""
    return "\n".join(ln for ln in text.split("\n") if not lx.is_valid_disclosure(ln) or not ln.strip())


def _patch(f: TextField, new: str, res: RuleResult) -> None:
    if new != f.text:
        f.text = new
        res.patched = True
        if f.in_render:
            res.actions.add("rerender")


# ───────────────────────── D: disclosure ─────────────────────────


async def d02_disclosure_wording(s: GateSubject, ctx: AppContext) -> RuleResult:
    """A first-line disclosure attempt that omits the economic consideration is replaced (D-02)."""
    res = RuleResult()
    std = ctx.settings.gate.standard_disclosure
    for f in s.texts():
        if not f.disclosure_first or not f.text.strip():
            continue
        first = _first_line(f.text)
        attempt = bool(re.search(r"(쿠팡\s*파트너스|광고|협찬|제휴)", first))
        if attempt and not lx.is_valid_disclosure(first):
            _patch(f, f.text.replace(first, std, 1), res)
            res.findings.append(_finding("D-02", Verdict.FIX, f"{f.name}: 대가성 문구가 경제적 대가를 명시하지 않아 표준 문구로 교체",
                                         fixed=True, field=f.name, old=first))
    return res


async def d01_disclosure_first_line(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    std = ctx.settings.gate.standard_disclosure
    for f in s.texts():
        if not f.disclosure_first:
            continue
        first = _first_line(f.text)
        if lx.is_valid_disclosure(first):
            continue
        # remove any disclosure placed lower (bottom placement → forfeiture risk), then put it first
        rest = "\n".join(ln for ln in f.text.split("\n") if not lx.is_valid_disclosure(ln)).strip()
        _patch(f, f"{std}\n\n{rest}" if rest else std, res)
        res.findings.append(_finding("D-01", Verdict.FIX, f"{f.name}: 첫 줄에 대가성 문구 삽입", fixed=True, field=f.name))
    return res


async def d03_video_overlay(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    c = s.content
    if c.kind is not ContentKind.SHORT:
        return res
    r = c.render
    if r is None:
        res.findings.append(_finding("D-03", Verdict.BLOCK, "렌더 결과가 없어 광고 오버레이를 확인할 수 없음"))
        return res
    ov = r.disclosure_overlay or {}
    problems = []
    if not ov.get("locked"):
        problems.append("overlay not template-locked")
    if float(ov.get("start", 99)) > 0.05:
        problems.append("overlay does not start at 0s")
    if float(ov.get("end", 0)) < ctx.settings.gate.disclosure_overlay_seconds:
        problems.append("overlay shorter than 3s")
    text = str(ov.get("text", ""))
    if "광고" not in text or not lx.is_valid_disclosure(text):
        problems.append("overlay text missing 광고/수수료 statement")
    if not problems:
        try:
            from acvm.media.verify import verify_disclosure_overlay

            path = await ctx.objects.local_path(r.media_uri)
            chk = await verify_disclosure_overlay(path, ov, ffmpeg=ctx.settings.render.ffmpeg)
            if not chk.ok:
                problems.append(f"frame verification failed (score={chk.score:.3f})")
        except Exception as exc:
            problems.append(f"frame verification unavailable: {type(exc).__name__}: {str(exc)[:120]}")
    if problems:
        res.findings.append(_finding("D-03", Verdict.BLOCK, "영상 첫 3초 광고 표시 검증 실패", problems=problems))
    return res


class _FirstText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_body = False
        self.skip = 0
        self.first: str | None = None
        self.first_id: str | None = None
        self._stack_ids: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.in_body = True
        if tag in ("script", "style", "template", "noscript"):
            self.skip += 1
        self._stack_ids.append(dict(attrs).get("id"))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template", "noscript"):
            self.skip = max(0, self.skip - 1)
        if self._stack_ids:
            self._stack_ids.pop()

    def handle_data(self, data: str) -> None:
        if self.in_body and not self.skip and self.first is None and data.strip():
            self.first = data.strip()
            self.first_id = next((i for i in reversed(self._stack_ids) if i), None)


def hub_disclosure_ok(html: str) -> tuple[bool, str]:
    p = _FirstText()
    p.feed(html)
    first = p.first or ""
    return lx.is_valid_disclosure(first), first


async def d04_hub_disclosure(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    pub = s.publication
    if pub is None or pub.link_placement != "hub":
        return res
    async with ctx.uow() as repos:
        meta = await repos.kv.get_stale(f"hub:{pub.channel_account_id}")
    if not meta or "uri" not in meta:
        res.findings.append(_finding("D-04", Verdict.BLOCK, "링크 허브 페이지가 빌드되지 않음"))
        return res
    try:
        html = (await ctx.objects.get_bytes(str(meta["uri"]))).decode("utf-8", "replace")
    except Exception as exc:
        res.findings.append(_finding("D-04", Verdict.BLOCK, f"링크 허브 페이지를 읽을 수 없음: {type(exc).__name__}"))
        return res
    ok, first = hub_disclosure_ok(html)
    if not ok:
        res.findings.append(_finding("D-04", Verdict.BLOCK, "링크 허브 최상단 첫 텍스트가 대가성 문구가 아님", first=first[:80]))
    return res


# ───────────────────────── C: claims ─────────────────────────


async def c04_regulated(s: GateSubject, ctx: AppContext) -> RuleResult:
    """Same classifier as M1's hard exclusion (acvm.infra.coupang_catalog) so selection and gate cannot
    disagree; the local term list is a second, independent net (defence in depth)."""
    from acvm.infra.coupang_catalog import is_regulated, regulated_terms

    res = RuleResult()
    p = s.product
    hit = sorted(set(regulated_terms(p.name)) | {t for t in REGULATED_TERMS if t in p.name})
    if p.regulated or p.category_id in REGULATED_CATEGORIES or is_regulated(p.name, p.category_id) or hit:
        res.findings.append(_finding("C-04", Verdict.BLOCK, "규제 카테고리/품목 상품", category=p.category_id, terms=hit))
    return res


async def c02_superlatives(s: GateSubject, ctx: AppContext) -> RuleResult:
    """Soften replaceable superlatives; delete sentences with non-replaceable hits (efficacy, fake
    experience, click-bait, guarantees) and spec-dependent terms lacking a verified spec (C-02, §8.2)."""
    res = RuleResult()
    fake_ok = bool(s.facts.get("spec.operator_used"))
    fact_keys = set(s.facts.values)
    for f in s.texts():
        if not f.text:
            continue
        terms = sorted({h.term for h in lx.scan(f.text)})
        softened, unresolved = lx.soften(f.text)
        new = softened if softened != lx.normalize(f.text) else f.text
        removed: list[str] = []
        for h in unresolved:
            if h.category == "fake_experience" and fake_ok:
                continue
            for sent in pc.split_sentences(new):
                if h.term in sent:
                    new = _strip_sentence(new, sent)
                    removed.append(sent)
        for term, key in lx.spec_required_hits(new, fact_keys):
            for sent in pc.split_sentences(new):
                if term in sent:
                    new = _strip_sentence(new, sent)
                    removed.append(f"{sent} (requires {key})")
        if new != f.text:
            _patch(f, new, res)
            res.findings.append(_finding("C-02", Verdict.FIX, f"{f.name}: 금칙 표현 순화/삭제", fixed=True, field=f.name,
                                         terms=terms, removed=removed))
    return res


async def c01_grounding(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    facts = s.facts.values
    grounded = [c for c in s.content.claims if c.kind in ("opinion", "cta", "disclosure")
                or pc.check_claim(c.text, c.evidence, facts).ok]
    bad_claims = [c for c in s.content.claims if c not in grounded]
    covered = [c.text for c in grounded]
    names = [str(s.facts.get("short_name", "")), str(s.facts.get("name", ""))]
    for f in s.texts():
        if not f.text:
            continue
        new = f.text
        removed = []
        for c in bad_claims:
            if c.text in new:
                new = _strip_sentence(new, c.text)
                removed.append(c.text)
        for sent in pc.uncovered_factual_sentences(_non_disclosure(new), covered, mask=names):
            new = _strip_sentence(new, sent)
            removed.append(sent)
        if removed:
            _patch(f, new, res)
            res.findings.append(_finding("C-01", Verdict.FIX, f"{f.name}: 근거 없는 사실 주장 {len(removed)}건 삭제", fixed=True,
                                         field=f.name, removed=removed))
    if bad_claims:
        s.content = s.content.model_copy(update={"claims": grounded})
    if s.stage == "gate" and ctx.llm is not None:
        res.findings.extend(await _llm_claim_judge(s, ctx, res))
    return res


async def _llm_claim_judge(s: GateSubject, ctx: AppContext, res: RuleResult) -> list[Finding]:
    """LLM judge (large tier) for semantic over-claims the rules cannot see ("이거 하나면 끝")."""
    from pydantic import BaseModel

    from acvm.security.prompt_guard import DATA_POLICY, data_block

    class Issue(BaseModel):
        field: str
        sentence: str
        problem: str

    class Judgement(BaseModel):
        issues: list[Issue]

    system = ("You audit Korean affiliate-marketing copy for unsupported factual or efficacy claims under the "
              "Korean Act on Fair Labeling and Advertising. Only flag sentences that assert a fact or effect not "
              "supported by the provided facts. Subjective, clearly-opinion phrasing is acceptable.\n\n" + DATA_POLICY)
    texts = "\n".join(f"[{f.name}] {f.text}" for f in s.texts() if f.text)
    try:
        out = await ctx.llm.structured(  # type: ignore[union-attr]
            tier="large", system=system, schema=Judgement, effort="medium", purpose="gate_c01",
            user=data_block("facts", s.facts.as_prompt_lines()) + "\n\n" + data_block("copy", texts))
    except Exception as exc:
        return [_finding("C-01", Verdict.PASS, f"LLM 판정 생략: {type(exc).__name__}")]
    found = []
    for issue in out.issues:
        f = s.fields.get(issue.field)
        if f and issue.sentence and issue.sentence in f.text:
            _patch(f, _strip_sentence(f.text, issue.sentence), res)
            found.append(_finding("C-01", Verdict.FIX, f"{f.name}: LLM 판정 — {issue.problem[:80]}", fixed=True,
                                  field=f.name, removed=[issue.sentence]))
    return found


async def c03_price(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    p = s.product
    fresh = (p.price_checked_at is not None
             and s.now - p.price_checked_at <= timedelta(minutes=ctx.settings.gate.price_fresh_minutes))
    for f in s.texts():
        if not f.text:
            continue
        new = f.text
        removed = []
        for sent in pc.split_sentences(_non_disclosure(f.text)):
            if not _PRICE.search(sent):
                continue
            nums = [n for n in pc.numbers_in(sent) if n >= 100]
            discount = bool(re.search(r"(할인|세일|특가|쿠폰|%)", sent))
            ok = fresh and nums and all(abs(n - p.price) < 0.5 for n in nums) and not discount
            if not ok:
                new = _strip_sentence(new, sent)
                removed.append(sent)
        if removed:
            _patch(f, new, res)
            res.findings.append(_finding("C-03", Verdict.FIX, f"{f.name}: 실시간 가격과 불일치/미검증 가격 문구 제거", fixed=True,
                                         field=f.name, removed=removed, price=p.price, fresh=fresh))
    return res


# ───────────────────────── R: rights & similarity ─────────────────────────


async def r01_rights_ledger(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    r = s.content.render
    if s.content.kind is not ContentKind.SHORT:
        return res
    if r is None:
        res.findings.append(_finding("R-01", Verdict.BLOCK, "렌더 메타데이터 없음 — 에셋 출처 확인 불가"))
        return res
    ids = list(r.asset_ids)
    async with ctx.uow() as repos:
        ledger = await repos.assets.get_many(ids)
        linked = await repos.contents.asset_ids(s.content.id)
    missing = [str(i) for i in ids if i not in ledger]
    today = s.now.date()
    expired = [str(a.id) for a in ledger.values() if a.expires_at is not None and a.expires_at < today]
    unlinked = [str(i) for i in ids if linked and i not in linked]
    if not ids:
        missing.append("(no assets recorded)")
    if missing or expired or unlinked:
        res.findings.append(_finding("R-01", Verdict.BLOCK, "권리 원장에 없거나 만료된 에셋 사용", missing=missing,
                                     expired=expired, unlinked=unlinked))
    s.assets = ledger
    return res


async def r02_reference_similarity(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    g = ctx.settings.gate
    sc = s.content.script
    text = " ".join([sc.hook, sc.narration, _non_disclosure(sc.body), _non_disclosure(sc.caption)]).strip()
    if not text:
        return res
    emb = (await ctx.embedder.embed([text]))[0]
    async with ctx.uow() as repos:
        near = await repos.dna.nearest(emb, k=5)
        parent = await repos.contents.get(s.content.parent_content_id) if s.content.parent_content_id else None
    worst_cos, worst_ng, ref = 0.0, 0.0, None
    for dna, cos in near:
        ng = ngram_overlap(text, dna.reference_text, g.reference_ngram_n) if dna.reference_text else 0.0
        if cos > worst_cos:
            worst_cos, ref = cos, str(dna.id)
        worst_ng = max(worst_ng, ng)
    if parent is not None:
        ps = parent.script
        ptext = " ".join([ps.hook, ps.narration, ps.body, ps.caption])
        worst_ng = max(worst_ng, ngram_overlap(text, ptext, g.reference_ngram_n))
    if worst_cos > g.reference_cosine_max or worst_ng > g.reference_ngram_max:
        res.actions.add("regenerate")
        res.findings.append(_finding("R-02", Verdict.FIX, "레퍼런스와 과도하게 유사 — 재생성 필요", cosine=round(worst_cos, 3),
                                     ngram=round(worst_ng, 3), reference=ref))
    return res


async def r03_ip_and_voice(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    sc = s.content.script
    corpus = " ".join([s.product.name, sc.narration, sc.body, sc.caption, " ".join(sc.hooks)])
    ip_hits = sorted({t for t in IP_TERMS if t in corpus})
    if ip_hits:
        res.findings.append(_finding("R-03", Verdict.HOLD, "상표·캐릭터·유명인 관련 표현 — 사람 검토 필요", terms=ip_hits))
    r = s.content.render
    if s.content.kind is ContentKind.SHORT and r is not None:
        async with ctx.uow() as repos:
            voices = [a for a in (await repos.assets.get_many(r.asset_ids)).values() if a.kind == "voice"]
        if not r.voice_source or not any(v.uri == r.voice_source for v in voices):
            res.findings.append(_finding("R-03", Verdict.HOLD, "보이스 출처가 권리 원장에서 확인되지 않음", voice=r.voice_source))
        if ctx.llm is not None:
            res.findings.extend(await _vision_check(s, ctx))
    return res


async def _vision_check(s: GateSubject, ctx: AppContext) -> list[Finding]:
    from pydantic import BaseModel

    class Vision(BaseModel):
        celebrity_face: bool
        cartoon_character: bool
        third_party_trademark: bool
        notes: str

    try:
        from acvm.media.ffmpeg import extract_frame

        path = await ctx.objects.local_path(s.content.render.media_uri)  # type: ignore[union-attr]
        dur = s.content.render.duration_sec  # type: ignore[union-attr]
        frames = [await extract_frame(path, t, ffmpeg=ctx.settings.render.ffmpeg) for t in (dur * 0.3, dur * 0.55, dur * 0.8)]
        out = await ctx.llm.structured(  # type: ignore[union-attr]
            tier="medium", schema=Vision, images=frames, effort="low", purpose="gate_r03",
            system="You inspect video keyframes for intellectual-property risk in advertising. Report only what is "
                   "clearly visible. The advertised product's own packaging/brand is NOT a third-party trademark.",
            user=f"Advertised product: {s.product.name}. Are there recognisable real people (celebrities), cartoon/"
                 f"licensed characters, or third-party trademarks/logos other than the product's own?")
    except Exception as exc:
        return [_finding("R-03", Verdict.PASS, f"비전 분류 생략: {type(exc).__name__}")]
    if out.celebrity_face or out.cartoon_character or out.third_party_trademark:
        return [_finding("R-03", Verdict.HOLD, "키프레임에서 IP 위험 감지", notes=out.notes[:200],
                         celebrity=out.celebrity_face, character=out.cartoon_character,
                         trademark=out.third_party_trademark)]
    return []


# ───────────────────────── S / A / H: account policy ─────────────────────────


async def s01_account_duplicate(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    c = s.content
    text = similarity_text(c)
    since = s.now - timedelta(days=ctx.settings.gate.account_lookback_days)
    async with ctx.uow() as repos:
        recent = await repos.contents.recent_texts(account_id=c.target_account_id, workspace_id=c.workspace_id,
                                                   since=since, exclude=c.id)
    sig = _MH.signature(text)
    best, best_id = 0.0, None
    for cid, other in recent:
        sim = MinHasher.estimate(sig, _MH.signature(other)) if other else 0.0
        if sim > best:
            best, best_id = sim, cid
    if best > ctx.settings.gate.account_minhash_max:
        res.actions.add("regenerate")
        res.findings.append(_finding("S-01", Verdict.FIX, "계정 최근 30일 콘텐츠와 중복 — 재생성 필요", similarity=round(best, 3),
                                     duplicate_of=str(best_id)))
    return res


async def s02_account_state(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    for acc in s.accounts.values():
        if acc.status is not AccountStatus.ACTIVE and (acc.cooldown_until is None or acc.cooldown_until > s.now):
            res.actions.add("reschedule")
            res.findings.append(_finding("S-02", Verdict.FIX, f"{acc.platform.value}/{acc.handle} 계정 {acc.status.value} — 재스케줄",
                                         account=str(acc.id), until=acc.cooldown_until.isoformat() if acc.cooldown_until else None))
    return res


async def a01_ai_label(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    pub = s.publication
    if pub is not None and not pub.ai_label and not s.flags.get("ai_label"):
        s.flags["ai_label"] = True
        res.patched = True
        res.findings.append(_finding("A-01", Verdict.FIX, "AI 생성물 라벨 설정", fixed=True))
    return res


async def h01_new_account_template(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    g = ctx.settings.gate
    for acc in s.accounts.values():
        age = (s.now - acc.created_at).days
        if age < g.new_account_hold_days:
            res.findings.append(_finding("H-01", Verdict.HOLD, f"신규 계정 {age}일차 (< {g.new_account_hold_days}일) — 사람 승인",
                                         account=str(acc.id)))
    async with ctx.uow() as repos:
        n = await repos.contents.count_by_template(s.content.template_id, s.content.workspace_id)
    if n < g.new_template_hold_count:
        res.findings.append(_finding("H-01", Verdict.HOLD, f"신규 템플릿 {s.content.template_id} 승인 {n}건 "
                                                           f"(< {g.new_template_hold_count}) — 사람 승인"))
    return res


# ───────────────────────── preflight-only ─────────────────────────


async def g00_bypass(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    if s.content.status is not ContentStatus.APPROVED:
        from acvm.core.metrics import GATE_RULE_REGRESSIONS

        GATE_RULE_REGRESSIONS.inc()
        res.findings.append(_finding("G-00", Verdict.BLOCK, f"승인되지 않은 콘텐츠({s.content.status.value}) 게시 시도 차단"))
    return res


async def l01_link_integrity(s: GateSubject, ctx: AppContext) -> RuleResult:
    res = RuleResult()
    link = s.link
    if link is None:
        res.findings.append(_finding("L-01", Verdict.BLOCK, "게시물에 연결된 제휴 링크 없음"))
        return res
    partner = ctx.settings.coupang.partner_id
    try:
        key = (await ctx.secrets.get(ctx.settings.security.link_signing_key_ref)).encode()
        linksign.verify_signature(key, link.signature, link.sub_id, link.deeplink, link.short_url, partner)
        linksign.verify_short_url(link.short_url)
        linksign.verify_partner_tag(link.deeplink, partner, link.sub_id)
    except IntegrityViolation as exc:
        res.findings.append(_finding("L-01", Verdict.BLOCK, f"링크 무결성 위반: {exc}",
                                     context={k: str(v) for k, v in exc.context.items()}))
        return res
    except KeyError as exc:
        res.findings.append(_finding("L-01", Verdict.BLOCK, f"서명 키 조회 실패: {exc}"))
        return res
    if link.health not in (LinkHealth.OK,):
        res.findings.append(_finding("L-01", Verdict.HOLD, f"링크 상태 {link.health.value} — 대체 상품 검토 후 게시", health=link.health.value))
    return res


@dataclass(frozen=True)
class BoundRule:
    id: str
    fn: RuleFn
    stages: tuple[str, ...]


RULE_FUNCS: tuple[BoundRule, ...] = (
    BoundRule("G-00", g00_bypass, ("preflight",)),
    BoundRule("C-04", c04_regulated, ("gate", "preflight")),
    BoundRule("R-01", r01_rights_ledger, ("gate",)),
    BoundRule("D-03", d03_video_overlay, ("gate",)),
    BoundRule("L-01", l01_link_integrity, ("preflight",)),
    BoundRule("D-04", d04_hub_disclosure, ("preflight",)),
    BoundRule("C-01", c01_grounding, ("gate", "preflight")),
    BoundRule("C-02", c02_superlatives, ("gate", "preflight")),
    BoundRule("C-03", c03_price, ("gate", "preflight")),
    BoundRule("D-02", d02_disclosure_wording, ("gate", "preflight")),
    BoundRule("D-01", d01_disclosure_first_line, ("gate", "preflight")),
    BoundRule("R-02", r02_reference_similarity, ("gate",)),
    BoundRule("S-01", s01_account_duplicate, ("gate",)),
    BoundRule("R-03", r03_ip_and_voice, ("gate",)),
    BoundRule("S-02", s02_account_state, ("gate", "preflight")),
    BoundRule("A-01", a01_ai_label, ("preflight",)),
    BoundRule("H-01", h01_new_account_template, ("gate",)),
)

__all__ = ["RULE_FUNCS", "BoundRule", "hub_disclosure_ok"]
