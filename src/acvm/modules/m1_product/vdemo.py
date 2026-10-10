"""V_demo — 영상화 적합도 (§4 M1 feature table, §5.1 Scout agent, small model tier).

"LLM이 '15초 안에 전후·사용 장면을 보여줄 수 있는가' 0~1 채점; 원본의 '과자 밀봉 클립'이 전형."

* With ``ctx.llm``: the Scout agent (small tier) returns a schema-validated ``VDemoAssessment``. The
  product title is untrusted marketplace text, so it travels only inside a ``data_block`` and the system
  prompt carries ``DATA_POLICY`` (§10 명령 주입 방어). Each LLM score is charged to the run budget
  (``costs.llm_per_score_krw``, §5.2 예산 캐핑); budget exhaustion or an LLM failure falls back to the
  heuristic instead of failing selection.
* Without an LLM: a deterministic lexicon heuristic. Visually demonstrable mechanisms (클립, 밀봉, 접이식,
  거치대 …) raise the score with diminishing returns, before/after potential (얼룩·냄새·공간 절약 …) adds
  a bonus, and non-visual goods (도서, 상품권, 이용권 …) are damped:

      s = 1 − (1 − base_cat) · Π_{top-3 terms} (1 − 0.6·wᵢ)
      s ← s + 0.25·(1 − s)   if any before/after cue
      s ← 0.4·s              if any non-visual cue

Results are cached per (product, title) in ``kv_cache`` — 30 days for LLM/heuristic scores, 1 day for a
heuristic used as an LLM *fallback* so the LLM is retried — which keeps re-runs deterministic (§5.2).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, Field

from acvm.core.errors import ACVMError, BudgetExceeded
from acvm.core.ids import stable_hash
from acvm.core.logging import get_logger
from acvm.domain.models import Product
from acvm.infra.coupang_catalog import CATEGORY_NAMES
from acvm.modules.m1_product.priors import prior_for
from acvm.security.prompt_guard import DATA_POLICY, data_block

if TYPE_CHECKING:
    from acvm.context import AppContext

log = get_logger(__name__)

SCORER_VERSION: Final = "vdemo-v1"
CACHE_TTL: Final = timedelta(days=30)
FALLBACK_TTL: Final = timedelta(days=1)
LLM_CONCURRENCY: Final = 4

VDemoSource = Literal["llm", "heuristic", "heuristic_fallback"]

# Visually demonstrable mechanisms → weight (how strongly the term implies a 15-second demo).
DEMO_LEXICON: Final[dict[str, float]] = {
    "클립": 0.90, "밀봉": 0.90, "밀폐": 0.80, "진공": 0.80, "압축": 0.80, "정리함": 0.80, "칸막이": 0.75,
    "정리": 0.75, "수납": 0.75, "거치대": 0.85, "홀더": 0.70, "걸이": 0.70, "후크": 0.70, "행거": 0.70,
    "선반": 0.65, "트레이": 0.65, "슬라이딩": 0.75, "슬라이드": 0.70, "접이식": 0.85, "폴딩": 0.80,
    "자석": 0.80, "마그네틱": 0.80, "무타공": 0.80, "틈새": 0.80, "원터치": 0.75, "세척": 0.75, "청소": 0.70,
    "거품기": 0.70, "슬라이서": 0.85, "채칼": 0.85, "다지기": 0.80, "집게": 0.65, "거름망": 0.70,
    "논슬립": 0.70, "미끄럼방지": 0.70, "방수": 0.65, "발수": 0.80, "코팅": 0.60, "확장": 0.60,
    "송풍구": 0.70, "햇빛가리개": 0.75, "무선": 0.55, "휴대용": 0.55, "충전": 0.50, "회전": 0.65,
    "연결고리": 0.75, "리빙박스": 0.70, "건조대": 0.65,
}
BEFORE_AFTER_CUES: Final[tuple[str, ...]] = (
    "얼룩", "제거", "냄새", "탈취", "먼지", "물때", "곰팡이", "공간", "절약", "압축", "발수", "진공", "밀봉",
)
NON_VISUAL_CUES: Final[tuple[str, ...]] = (
    "도서", "상품권", "이용권", "쿠폰", "구독", "티켓", "숙박", "보험", "기프트카드", "멤버십", "교환권",
)

SYSTEM_PROMPT: Final = (
    "You are Scout, the product-scouting agent of an affiliate short-form video pipeline.\n"
    "Task: rate how well ONE product can be demonstrated visually in a 15-second vertical video — can a "
    "viewer clearly see a before/after or an in-use scene within 15 seconds? This is the product's "
    "'영상화 적합도' (V_demo).\n"
    "Rubric: 0.9–1.0 the mechanism itself is the hook (a clip sealing a snack bag, a folding rack "
    "popping open); 0.6–0.8 a clear use scene exists but the payoff is less visual; 0.3–0.5 the benefit "
    "is mostly invisible (taste, durability, specs); 0.0–0.2 nothing to show (vouchers, books, services).\n"
    "Judge visual demonstrability only. Never infer health, medical or efficacy properties.\n"
    "Return score in [0, 1] and a rationale of at most two short Korean sentences.\n\n" + DATA_POLICY
)


class VDemoAssessment(BaseModel):
    """Scout's structured output (schema-validated by the LLM adapter)."""

    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=400)


@dataclass(frozen=True, slots=True)
class VDemoResult:
    score: float
    rationale: str
    source: VDemoSource


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())


def heuristic_vdemo(name: str, category_id: str) -> VDemoResult:
    """Deterministic V_demo (see module docstring for the formula)."""
    s_name = _norm(name)
    base = prior_for(category_id).demo_base
    found = [term for term in DEMO_LEXICON if term in s_name]
    # a term contained in a longer matched term is the same mechanism (정리 ⊂ 정리함)
    distinct = [term for term in found if not any(term != other and term in other for other in found)]
    top = sorted((DEMO_LEXICON[term] for term in distinct), reverse=True)[:3]
    miss = 1.0
    for w in top:
        miss *= 1.0 - 0.6 * w
    score = 1.0 - (1.0 - base) * miss
    cues: list[str] = []
    if any(cue in s_name for cue in BEFORE_AFTER_CUES):
        score += 0.25 * (1.0 - score)
        cues.append("전후 비교 가능")
    if any(cue in s_name for cue in NON_VISUAL_CUES):
        score *= 0.4
        cues.append("시각적 시연 어려움")
    score = round(min(1.0, max(0.0, score)), 4)
    terms = ", ".join(sorted(distinct, key=lambda t_: -DEMO_LEXICON[t_])[:3]) or "시연 요소 없음"
    rationale = f"시연 요소: {terms}" + (f" / {', '.join(cues)}" if cues else "")
    return VDemoResult(score, rationale, "heuristic")


def _user_prompt(product: Product) -> str:
    facts = "\n".join((
        f"상품명: {product.name}",
        f"카테고리: {product.category_name or CATEGORY_NAMES.get(product.category_id, product.category_id)}",
        f"가격: {product.price:,}원",
        f"로켓배송: {'예' if product.is_rocket else '아니오'}",
    ))
    return "Rate the product described in the data block.\n" + data_block("product", facts, max_len=1_000)


class ScoutVDemo:
    """V_demo scorer with caching, budget charging and deterministic fallback."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    @property
    def mode(self) -> str:
        return "llm" if self.ctx.llm is not None else "heuristic"

    def cache_key(self, product: Product) -> str:
        title = stable_hash({"name": product.name, "category": product.category_id}, length=12)
        return f"m1:vdemo:{SCORER_VERSION}:{self.mode}:{product.id}:{title}"

    async def score(self, product: Product, *, workspace_id: uuid.UUID | None = None,
                    run_id: uuid.UUID | None = None) -> VDemoResult:
        return (await self.score_many([product], workspace_id=workspace_id, run_id=run_id))[product.id]

    async def score_many(self, products: Sequence[Product], *, workspace_id: uuid.UUID | None = None,
                         run_id: uuid.UUID | None = None) -> dict[int, VDemoResult]:
        now = self.ctx.clock.now()
        out: dict[int, VDemoResult] = {}
        unique = {p.id: p for p in products}
        async with self.ctx.uow() as repos:
            for p in unique.values():
                hit = await repos.kv.get(self.cache_key(p), now)
                if hit is not None:
                    out[p.id] = VDemoResult(float(hit["score"]), str(hit.get("rationale", "")), hit["source"])
        misses = [p for p in unique.values() if p.id not in out]
        if not misses:
            return out

        fresh: dict[int, VDemoResult]
        if self.ctx.llm is None:
            fresh = {p.id: heuristic_vdemo(p.name, p.category_id) for p in misses}
        else:
            fresh = await self._score_llm(misses, workspace_id, run_id)
        async with self.ctx.uow() as repos:
            for p in misses:
                r = fresh[p.id]
                ttl = FALLBACK_TTL if r.source == "heuristic_fallback" else CACHE_TTL
                await repos.kv.put(self.cache_key(p), {"score": r.score, "rationale": r.rationale,
                                                       "source": r.source}, ttl, now)
        out.update(fresh)
        return out

    async def _score_llm(self, products: Sequence[Product], workspace_id: uuid.UUID | None,
                         run_id: uuid.UUID | None) -> dict[int, VDemoResult]:
        allowed = await self._charge(products, workspace_id, run_id)
        sem = asyncio.Semaphore(LLM_CONCURRENCY)

        async def one(p: Product) -> tuple[int, VDemoResult]:
            if p.id not in allowed:
                h = heuristic_vdemo(p.name, p.category_id)
                return p.id, VDemoResult(h.score, h.rationale, "heuristic_fallback")
            async with sem:
                return p.id, await self._ask(p)

        return dict(await asyncio.gather(*(one(p) for p in products)))

    async def _charge(self, products: Sequence[Product], workspace_id: uuid.UUID | None,
                      run_id: uuid.UUID | None) -> set[int]:
        """Charge one LLM score per product (§9.4 "상품 채점"); products over budget use the heuristic."""
        if workspace_id is None:
            return {p.id for p in products}  # ad-hoc scoring outside a run: the LLM usage sink accounts it
        allowed: set[int] = set()
        amount = self.ctx.settings.costs.llm_per_score_krw
        async with self.ctx.uow() as repos:
            for p in products:
                try:
                    await self.ctx.charge(repos, workspace_id=workspace_id, run_id=run_id, stage="m1.vdemo",
                                          amount_krw=amount, key=f"m1.vdemo:{run_id}:{p.id}",
                                          detail={"product_id": p.id})
                except BudgetExceeded:
                    log.warning("vdemo_budget_exhausted_heuristic_used", product_id=p.id, run_id=str(run_id))
                    continue
                allowed.add(p.id)
        return allowed

    async def _ask(self, product: Product) -> VDemoResult:
        llm = self.ctx.llm
        if llm is None:  # pragma: no cover — guarded by the caller
            return heuristic_vdemo(product.name, product.category_id)
        try:
            out = await llm.structured(tier="small", system=SYSTEM_PROMPT, user=_user_prompt(product),
                                       schema=VDemoAssessment, purpose="m1.vdemo")
        except (ACVMError, ValueError) as exc:
            log.warning("vdemo_llm_failed_heuristic_used", product_id=product.id, error=type(exc).__name__)
            h = heuristic_vdemo(product.name, product.category_id)
            return VDemoResult(h.score, h.rationale, "heuristic_fallback")
        return VDemoResult(round(float(out.score), 4), out.rationale.strip(), "llm")
