"""Content DNA extraction and storage (design §4 M2, §6.1 ``content_dna``, §8.2).

DNA schema (§4 M2): hook_type (질문/충격/전후비교/가격반전/공감), first3s_pattern, 컷 수, 평균 컷 길이,
자막 밀도 (자/초), CTA 위치, 감정 곡선 (불편 → 발견 → 해소), 길이.

Sources, in priority order:

1. **own** — the channel's own tail content. M9 emits ``perf.updated`` with the views percentile of a
   publication; at percentile ≥ 0.9 (:data:`TAIL_PERCENTILE`) the content's structure is learned once.
   Only for own content is ``reference_text`` filled (own narration, or the own post body for text posts),
   so the R-02 similarity gate can stop the engine from cloning our own winners verbatim.
2. **external_meta / operator_note** — official-API metadata (title, duration, views, likes, posted_at,
   platform) plus the operator's observation note. §8.2 forbids storing anyone else's media, audio or
   subtitles, so ``reference_text`` is always ``""`` and only *derived* structural features are kept:
   the hook class, an abstract first-3-seconds pattern, cut count / CTA second parsed from the note.
   Raw note text is never persisted (it only shapes the embedding), because operators may paste
   third-party captions into a note by accident.

Metrics for a short (beats b₁…bₙ with [start, end) seconds, total length D):

    cut_count        = n
    avg_cut_sec      = Σ (endᵢ − startᵢ) / n
    caption_cps      = (non-whitespace narration characters) / D      — burned-in captions = narration
    cta_position_sec = start of the first ``cta`` beat (else of the last beat)

Text posts have no timeline; they are mapped onto one with a silent-reading speed of
:data:`READ_CPS` characters per second (paragraph = cut), so text and short DNA stay comparable.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from acvm.context import AppContext
from acvm.core.enums import HookType, Platform, PublicationState
from acvm.core.events import Event, PerfUpdated
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.db import tables as t
from acvm.domain.models import Content, ContentDNA
from acvm.quant.distributions import percentile_rank
from acvm.quant.similarity import normalize
from acvm.security.audit import audit
from acvm.security.prompt_guard import DATA_POLICY, data_block

log = get_logger(__name__)

TAIL_PERCENTILE = 0.90  # "꼬리 성과물" threshold for own-content DNA learning
READ_CPS = 10.0  # Korean silent reading ≈ 600 characters / minute
EMBEDDING_DIM = 1024  # content_dna.embedding vector(1024), §6.1
_DNA_NAMESPACE = uuid.UUID("6f1c2a52-5d0e-4c55-9b0e-acd0d2a00002")

EXTERNAL_META_KEYS = ("title", "duration", "views", "likes", "posted_at", "platform")
_META_ALIASES = {"duration_sec": "duration", "view_count": "views", "like_count": "likes",
                 "published_at": "posted_at"}

HOOK_LABEL: dict[HookType, str] = {
    HookType.QUESTION: "질문형",
    HookType.SHOCK: "충격형",
    HookType.BEFORE_AFTER: "전후비교형",
    HookType.PRICE_TWIST: "가격반전형",
    HookType.EMPATHY: "공감형",
}
HOOK_MOVE: dict[HookType, str] = {
    HookType.QUESTION: "질문형 문제제기",
    HookType.SHOCK: "충격형 사실 제시",
    HookType.BEFORE_AFTER: "전후비교형 비포 장면",
    HookType.PRICE_TWIST: "가격반전형 가격 제시",
    HookType.EMPATHY: "공감형 상황 묘사",
}
_ROLE_MOVE = {"hook": "문제제기", "problem": "문제제기", "amplify": "불편 증폭", "reveal": "제품 공개",
              "benefits": "장점 제시", "cta": "구매 유도"}
_HOOK_EMOTION = {HookType.QUESTION: "호기심", HookType.SHOCK: "놀람", HookType.BEFORE_AFTER: "불편",
                 HookType.PRICE_TWIST: "놀람", HookType.EMPATHY: "공감"}
_ROLE_EMOTION = {"problem": "불편", "amplify": "불편", "reveal": "발견", "benefits": "해소", "cta": "해소"}
EMOTION_VOCAB = frozenset({"호기심", "놀람", "불편", "공감", "발견", "해소", "기대"})
_PRODUCT_VISUAL = re.compile(r"product|제품|상품|item", re.IGNORECASE)

# Lexicon for hook inference on titles / notes (counts of cue matches; ties broken by list order).
_HOOK_LEXICON: tuple[tuple[HookType, re.Pattern[str]], ...] = (
    (HookType.PRICE_TWIST, re.compile(r"\d[\d,]*\s*(?:원|만원|천원)|가격|가성비|최저가|반값|할인|이\s*돈|싸다|저렴")),
    (HookType.BEFORE_AFTER, re.compile(r"전후|비포|애프터|before|after|달라진|변화|확\s*달라|→", re.IGNORECASE)),
    (HookType.QUESTION, re.compile(r"\?|？|왜\s|어떻게|뭐가|무엇|아세요|있나요|일까|할까|인가요|나요")),
    (HookType.SHOCK, re.compile(r"충격|경악|실화|미쳤|역대급|소름|대박|말도\s*안|!!|ㄷㄷ")),
    (HookType.EMPATHY, re.compile(r"공감|나만|다들|우리|ㅠ|힘들|귀찮|짜증|불편|자취|매일|맨날")),
)
_CUTS_RE = re.compile(r"(\d{1,3})\s*(?:컷|cuts?\b)", re.IGNORECASE)
_CTA_RE = re.compile(r"CTA\D{0,8}?(\d{1,3}(?:\.\d+)?)\s*(?:초|s\b|sec)", re.IGNORECASE)
_HIDDEN_RE = re.compile(r"제품\s*(?:미노출|안\s*보|없이|숨)")
_SHOWN_RE = re.compile(r"제품\s*(?:노출|등장|먼저|바로)")


def own_dna_id(content_id: uuid.UUID) -> uuid.UUID:
    """Deterministic DNA id for own content ⇒ "ingest once" holds across retries and replays."""
    return uuid.uuid5(_DNA_NAMESPACE, f"own:{content_id}")


def external_dna_id(workspace_id: uuid.UUID, meta: Mapping[str, Any]) -> uuid.UUID:
    """Deterministic id for one external reference (same post metadata ⇒ same DNA row)."""
    ident = {"ws": str(workspace_id), "platform": str(meta.get("platform", "")),
             "title": str(meta.get("title", "")), "posted_at": str(meta.get("posted_at", ""))}
    return uuid.uuid5(_DNA_NAMESPACE, "ext:" + stable_hash(ident))


def infer_hook_type(*texts: str) -> HookType:
    """Lexicon classifier: the hook class with most cue matches; QUESTION when nothing matches
    (the 29-second template opens with a problem question, §4 M4)."""
    blob = " ".join(x for x in texts if x)
    best, best_hits = HookType.QUESTION, 0
    for hook, pattern in _HOOK_LEXICON:
        hits = len(pattern.findall(blob))
        if hits > best_hits:
            best, best_hits = hook, hits
    return best


def _collapse(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


class _HookGuess(BaseModel):
    """Small-tier LLM classification of an external reference (enum fields only — no free text kept)."""

    hook_type: HookType
    emotion_arc: list[str] = Field(default_factory=list)


_HOOK_SYSTEM = (
    "You classify the opening hook of a Korean short-form commerce video from its public metadata "
    "and an operator's observation note. Answer with hook_type ∈ {question, shock, before_after, "
    "price_twist, empathy} and an emotion_arc using only these words: 호기심, 놀람, 불편, 공감, 발견, 해소, 기대. "
    + DATA_POLICY
)


class ContentDNAService:
    """M2 service. Stateless apart from ``ctx``; every write is idempotent by deterministic DNA id."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def subscribe(self) -> None:
        """Consume ``perf.updated`` (§6.3) for own-tail DNA learning."""
        self.ctx.bus.subscribe(PerfUpdated.TYPE, "m2_dna", self._handle)

    async def _handle(self, event: Event) -> None:
        await self.on_perf(event)

    # ───────────────────────────── own content ─────────────────────────────

    @staticmethod
    def structure_of(content: Content, perf_percentile: float) -> ContentDNA:
        """Pure structural extraction (no embedding). See module docstring for the formulas."""
        script = content.script
        hook = script.hook_type
        beats = sorted(script.beats, key=lambda b: b.start_sec)
        if beats:
            duration = (content.render.duration_sec if content.render and content.render.duration_sec > 0
                        else script.duration_sec if script.duration_sec > 0 else beats[-1].end_sec)
            cut_count = len(beats)
            avg_cut = sum(b.end_sec - b.start_sec for b in beats) / cut_count
            caption_chars = len(normalize(" ".join(b.narration for b in beats)))
            caption_cps = caption_chars / duration if duration > 0 else 0.0
            cta = next((b for b in beats if b.role == "cta"), beats[-1])
            first = [b for b in beats if b.start_sec < 3.0] or beats[:1]
            lead = first[0]
            move = HOOK_MOVE[hook] if lead.role == "hook" else f"{HOOK_LABEL[hook]} {_ROLE_MOVE[lead.role]}"
            product_shown = any(b.role in ("reveal", "benefits") or _PRODUCT_VISUAL.search(b.visual or "")
                                for b in first)
            parts = [move]
            if any(b.on_screen_text.strip() for b in first):
                parts.append("자막 훅")
            parts.append("제품 노출" if product_shown else "제품 미노출")
            first3s = " + ".join(parts)
            arc = _collapse([_HOOK_EMOTION[hook] if b.role == "hook" else _ROLE_EMOTION.get(b.role, "")
                             for b in beats])
            reference_text = script.narration.strip()
            cta_pos = cta.start_sec
        else:
            body = script.body.strip()
            disclosure = script.disclosure.strip()
            paragraphs = [p.strip() for p in body.splitlines() if p.strip()]
            if disclosure and paragraphs and normalize(paragraphs[0]) == normalize(disclosure):
                paragraphs = paragraphs[1:]  # the mandatory D-01 line is not part of the creative DNA
            text = "\n".join(paragraphs)
            chars = [len(normalize(p)) for p in paragraphs]
            duration = sum(chars) / READ_CPS
            cut_count = len(paragraphs)
            avg_cut = duration / cut_count if cut_count else 0.0
            caption_cps = 0.0
            cta_idx = next((i for i, p in enumerate(paragraphs)
                            if script.cta and normalize(script.cta) and normalize(script.cta) in normalize(p)),
                           max(cut_count - 1, 0))
            cta_pos = sum(chars[:cta_idx]) / READ_CPS
            first3s = f"{HOOK_MOVE[hook]} + 텍스트 첫 문단"
            arc = _collapse([_HOOK_EMOTION[hook], "발견", "해소"])
            reference_text = text
        return ContentDNA(
            id=own_dna_id(content.id), source="own", hook_type=hook, first3s_pattern=first3s,
            cut_count=cut_count, avg_cut_sec=round(avg_cut, 3), caption_cps=round(caption_cps, 3),
            cta_position_sec=round(cta_pos, 3), emotion_arc=arc or ["불편", "발견", "해소"],
            duration_sec=round(duration), perf_percentile=float(min(max(perf_percentile, 0.0), 1.0)),
            reference_text=reference_text, embedding=None, workspace_id=content.workspace_id)

    async def _embed(self, text: str) -> list[float] | None:
        if not text.strip():
            return None
        vec = (await self.ctx.embedder.embed([text]))[0]
        if len(vec) != EMBEDDING_DIM:
            log.warning("dna_embedding_dim_mismatch", got=len(vec), expected=EMBEDDING_DIM)
            return None
        return vec

    async def extract_dna(self, content: Content, perf_percentile: float) -> ContentDNA:
        """DNA of an own content item, embedded with ``ctx.embedder`` (embedding of the own narration)."""
        dna = self.structure_of(content, perf_percentile)
        return dna.model_copy(update={"embedding": await self._embed(dna.reference_text)})

    async def ingest_own(self, content_id: uuid.UUID, perf_percentile: float) -> ContentDNA | None:
        """Learn the DNA of one own content item exactly once (later calls only raise perf_percentile)."""
        dna_id = own_dna_id(content_id)
        async with self.ctx.uow() as r:
            existing = await r.dna.get(dna_id)
            if existing is not None:
                if perf_percentile > existing.perf_percentile:
                    row = await r.session.get(t.ContentDNARow, dna_id)
                    if row is not None:
                        row.perf_percentile = float(min(perf_percentile, 1.0))
                    existing = existing.model_copy(update={"perf_percentile": float(min(perf_percentile, 1.0))})
                return existing
            content = await r.contents.get(content_id)
        if content is None:
            log.warning("dna_content_missing", content_id=str(content_id))
            return None
        dna = await self.extract_dna(content, perf_percentile)
        try:
            async with self.ctx.uow() as r:
                if await r.dna.get(dna.id) is None:
                    await r.dna.add(dna)
        except IntegrityError:  # concurrent ingest of the same content won the insert
            async with self.ctx.uow() as r:
                stored = await r.dna.get(dna.id)
            return stored or dna
        log.info("dna_ingested_own", content_id=str(content_id), hook=dna.hook_type.value,
                 percentile=dna.perf_percentile)
        return dna

    async def on_perf(self, event: Event | PerfUpdated) -> ContentDNA | None:
        """``perf.updated`` handler: tail learning for percentile ≥ :data:`TAIL_PERCENTILE`."""
        payload = event if isinstance(event, PerfUpdated) else event.parsed()
        if not isinstance(payload, PerfUpdated) or payload.percentile < TAIL_PERCENTILE:
            return None
        return await self.ingest_own(payload.content_id, payload.percentile)

    # ───────────────────────────── external references ─────────────────────────────

    @staticmethod
    def _clean_meta(meta: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for raw_key, value in meta.items():
            key = _META_ALIASES.get(raw_key, raw_key)
            if key in EXTERNAL_META_KEYS:
                kept[key] = value
            else:
                dropped.append(str(raw_key))
        return kept, sorted(dropped)

    async def _guess_hook(self, title: str, note: str, platform: str) -> tuple[HookType, list[str]]:
        lexical = infer_hook_type(title, note)
        if self.ctx.llm is None:
            return lexical, []
        user = (data_block("reference_meta", f"platform: {platform}\ntitle: {title}", 600)
                + "\n" + data_block("operator_note", note, 1_500))
        try:
            guess = await self.ctx.llm.structured(tier="small", system=_HOOK_SYSTEM, user=user, schema=_HookGuess,
                                                  purpose="m2_hook_classify")
        except Exception as exc:  # the lexicon is always a valid answer; never fail ingestion on the LLM
            log.warning("dna_hook_llm_failed", error=type(exc).__name__)
            return lexical, []
        return guess.hook_type, [e for e in guess.emotion_arc if e in EMOTION_VOCAB]

    async def _own_views_percentile(self, workspace_id: uuid.UUID, platform: str, views: float) -> float:
        """Rank the reference's views against our own channel's recent views on that platform."""
        now = self.ctx.clock.now()
        since = now - timedelta(days=30)
        async with self.ctx.uow() as r:
            pubs = await r.publications.published_since(since, workspace_id)
        ids = [p.id for p in pubs if p.platform.value == platform and p.state is PublicationState.PUBLISHED]
        if not ids:
            return 0.5
        population = await self.ctx.perf.metric_population(platform, "views", since, ids)
        return percentile_rank(views, population) if population else 0.5

    async def ingest_external(self, workspace_id: uuid.UUID, meta: dict[str, Any], note: str) -> ContentDNA:
        """Store an external reference as metadata-derived DNA only (§4 M2, §8.2)."""
        kept, dropped = self._clean_meta(meta)
        dna_id = external_dna_id(workspace_id, kept)
        async with self.ctx.uow() as r:
            existing = await r.dna.get(dna_id)
        if existing is not None:
            return existing

        title = str(kept.get("title", "") or "")
        platform = str(kept.get("platform", "") or "").lower()
        note = note or ""
        hook, llm_arc = await self._guess_hook(title, note, platform)

        duration = _as_float(kept.get("duration"))
        cuts_match = _CUTS_RE.search(note)
        cut_count = int(cuts_match.group(1)) if cuts_match else 0
        cta_match = _CTA_RE.search(note)
        cta_pos = float(cta_match.group(1)) if cta_match else 0.0
        if _HIDDEN_RE.search(note):
            exposure = "제품 미노출"
        elif _SHOWN_RE.search(note):
            exposure = "제품 노출"
        else:
            exposure = ""
        first3s = " + ".join(p for p in (HOOK_MOVE[hook], exposure) if p) + " (외부 메타 추정)"
        arc = llm_arc or _collapse([_HOOK_EMOTION[hook], "발견", "해소"])

        views = _as_float(kept.get("views"))
        percentile = 0.5
        if views is not None and platform in {p.value for p in Platform}:
            percentile = await self._own_views_percentile(workspace_id, platform, views)

        # Embedding of public metadata + operator words (lossy vector, never reversible to text).
        embedding = await self._embed("\n".join(x for x in (title, first3s, note) if x))
        dna = ContentDNA(
            id=dna_id, source="external_meta" if kept else "operator_note", hook_type=hook,
            first3s_pattern=first3s, cut_count=cut_count,
            avg_cut_sec=round(duration / cut_count, 3) if duration and cut_count else 0.0,
            caption_cps=0.0, cta_position_sec=cta_pos, emotion_arc=arc,
            duration_sec=round(duration) if duration else 0, perf_percentile=percentile,
            reference_text="", embedding=embedding, workspace_id=workspace_id)
        try:
            async with self.ctx.uow() as r:
                if await r.dna.get(dna.id) is None:
                    await r.dna.add(dna)
                    await audit(r, self.ctx.clock, "m2", "dna.external_ingested", str(dna.id),
                                platform=platform, hook_type=hook.value, dropped_keys=dropped,
                                posted_at=str(kept.get("posted_at", "")))
        except IntegrityError:
            async with self.ctx.uow() as r:
                stored = await r.dna.get(dna.id)
            return stored or dna
        if dropped:
            log.info("dna_external_fields_dropped", keys=dropped)  # names only — values may be third-party text
        return dna

    # ───────────────────────────── retrieval ─────────────────────────────

    async def templates_for(self, workspace_id: uuid.UUID, hook_type: HookType | str | None = None,
                            k: int = 5) -> list[ContentDNA]:
        """Structure hints for the script engine (M4): own tail DNA first, then external, by percentile."""
        hook = hook_type.value if isinstance(hook_type, HookType) else hook_type
        async with self.ctx.uow() as r:
            pool = await r.dna.top(workspace_id, limit=max(4 * k, 20), hook_type=hook)
        pool.sort(key=lambda d: (d.source != "own", -d.perf_percentile, str(d.id)))
        return pool[: max(k, 0)]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return out if math.isfinite(out) and out >= 0 else None
