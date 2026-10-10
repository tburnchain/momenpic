"""Commander agent (§5.1, §5.3): natural-language command → slots.

The LLM (large tier, read-only tools) only *interprets* the command; plan assembly, cost estimation,
approval and execution are deterministic code (§5: "LLM은 계획만 만들고, 실제 실행은 결정적 코드가 맡는다").
The deterministic parser below handles the slot grammar on its own (and is the fallback when the
LLM is unavailable), so the system degrades to a fully offline vending machine, never to a broken one.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from acvm.core.enums import ContentKind, Intent, Platform
from acvm.core.logging import get_logger
from acvm.domain.models import CommandSlots
from acvm.ports import LLM
from acvm.security.prompt_guard import DATA_POLICY, data_block

log = get_logger(__name__)

_KO_NUM = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4, "다섯": 5, "여섯": 6, "일곱": 7,
           "여덟": 8, "아홉": 9, "열": 10, "스무": 20, "스물": 20, "서른": 30}
_PLATFORM_WORDS: tuple[tuple[str, Platform], ...] = (
    ("스레드", Platform.THREADS), ("threads", Platform.THREADS), ("인스타그램", Platform.INSTAGRAM),
    ("인스타", Platform.INSTAGRAM), ("instagram", Platform.INSTAGRAM), ("릴스", Platform.INSTAGRAM),
    ("유튜브", Platform.YOUTUBE), ("youtube", Platform.YOUTUBE), ("틱톡", Platform.TIKTOK), ("tiktok", Platform.TIKTOK),
    ("페이스북", Platform.FACEBOOK), ("페북", Platform.FACEBOOK), ("facebook", Platform.FACEBOOK),
    ("카카오", Platform.KAKAO), ("카톡", Platform.KAKAO), ("kakao", Platform.KAKAO),
)
_LOCAL_NICHES = {"주방": "1013", "kitchen": "1013", "차량": "1018", "자동차": "1018", "car": "1018", "정리수납": "1014",
                 "수납": "1014", "생활": "1014", "인테리어": "1015", "가전": "1016", "반려": "1029", "육아": "1011",
                 "뷰티": "1010", "식품": "1012", "스포츠": "1017", "완구": "1020", "문구": "1021"}


def resolve_category(text: str) -> str | None:
    try:
        from acvm.infra.coupang_catalog import resolve_category as _rc

        hit = _rc(text)
        if hit:
            return hit
    except ImportError:
        pass
    t = text.lower()
    for word, cid in sorted(_LOCAL_NICHES.items(), key=lambda x: -len(x[0])):
        if word in t:
            return cid
    return None


def _count(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*(개|건|편|가지)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(열|스무|스물|서른)?\s*(한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉)?\s*(개|건|편|가지)", text)
    if m and (m.group(1) or m.group(2)):
        return _KO_NUM.get(m.group(1) or "", 0) + _KO_NUM.get(m.group(2) or "", 0)
    return None


def _budget(text: str) -> float | None:
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(만|천)?\s*원", text)
    if not m:
        m2 = re.search(r"(만|천)\s*원", text)
        return {"만": 10_000.0, "천": 1_000.0}[m2.group(1)] if m2 else None
    val = float(m.group(1).replace(",", ""))
    return val * {"만": 10_000, "천": 1_000}.get(m.group(2) or "", 1)


def _schedule(text: str) -> str:
    m = re.search(r"매일\s*(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
    if m:
        hour = int(m.group(2)) % 24
        if m.group(1) in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        return f"daily@{hour:02d}:{int(m.group(3) or 0):02d}"
    if re.search(r"(오늘\s*)?(밤|저녁)", text):
        return "tonight"
    if "내일" in text:
        return "tomorrow"
    return "now"


def _persona(text: str) -> str | None:
    m = re.search(r"(\d0대)\s*(여자|여성|남자|남성|주부|자취생|직장인|엄마|아빠)?(\s*(여자|여성|남자|남성|직장인))?", text)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()
    for w in ("자취생", "주부", "직장인"):
        if w in text:
            return w
    return None


def _kind(text: str) -> ContentKind | None:
    t = text.replace("유튜브 쇼츠", "유튜브")
    if re.search(r"(쇼츠|숏폼|영상|릴스|동영상|비디오)", t):
        return ContentKind.SHORT
    if re.search(r"(글로|글만|텍스트|포스트|게시글|글\s)", t) or t.rstrip().endswith("글"):
        return ContentKind.TEXT
    return None


def _intent(text: str) -> Intent:
    if re.search(r"(분석해|분석만|리포트|성과\s*보여)", text) and not re.search(r"(올려|게시|만들)", text):
        return Intent.ANALYZE
    if re.search(r"(만들기만|만들어만|초안|올리지\s*말|게시하지\s*말|저장만)", text):
        return Intent.CREATE_ONLY
    return Intent.CREATE_AND_PUBLISH


def parse_rule_based(command: str) -> CommandSlots:
    text = unicodedata.normalize("NFKC", command).strip()
    low = text.lower()
    platforms: list[Platform] = []
    for word, p in _PLATFORM_WORDS:
        if word in low and p not in platforms:
            platforms.append(p)
    if re.search(r"(전체|모든)\s*(플랫폼|채널)", text):
        platforms = [p for p in Platform if p is not Platform.KAKAO]
    keyword = None
    q = re.search(r"[\"“'‘]([^\"”'’]{2,30})[\"”'’]", text)
    if q:
        keyword = q.group(1).strip()
    return CommandSlots(intent=_intent(text), category=resolve_category(text), count=_count(text) or 5,
                        kind=_kind(text), persona=_persona(text), platforms=platforms, schedule=_schedule(text),
                        budget_krw=_budget(text), keyword=keyword)


class LLMSlots(BaseModel):
    intent: Literal["create_and_publish", "create_only", "analyze"]
    category_text: str = Field(description="Niche/category words from the command, '' if none")
    count: int
    kind: Literal["text", "short", ""]
    persona: str
    platforms: list[Literal["threads", "instagram", "facebook", "youtube", "tiktok", "kakao"]]
    schedule: str = Field(description="now | tonight | tomorrow | daily@HH:MM")
    budget_krw: float = Field(description="0 when not specified")
    keyword: str
    ambiguities: list[str]


COMMANDER_SYSTEM = f"""You convert one Korean operator command for an affiliate-content system into structured slots.
Slot grammar (§5.3): intent ("만들어서 올려줘"→create_and_publish, "만들기만"→create_only, "분석해줘"→analyze),
category (niche words such as 주방/차량/정리수납), count (default 5; Korean numerals 다섯=5), kind (글→text, 쇼츠/영상/릴스→short,
'' if unspecified), persona (e.g. "30대 여자"), platforms (스레드→threads, 인스타/릴스→instagram, 유튜브→youtube, 틱톡→tiktok,
페이스북→facebook, 카카오→kakao; [] if unspecified), schedule (now, tonight, tomorrow, or daily@HH:MM for "매일 오전 9시"),
budget_krw ("2천 원 이내"→2000; 0 if unspecified), keyword (a product keyword if the operator named one, else '').
Never invent values the command does not state; list anything ambiguous in `ambiguities`.

{DATA_POLICY}"""


async def interpret(command: str, llm: LLM | None) -> tuple[CommandSlots, list[str]]:
    """Slots from the command. The rule parser always runs; the LLM refines ambiguous fields only when it
    agrees with the grammar (e.g. it may fill a category the regexes missed, never override an explicit one)."""
    base = parse_rule_based(command)
    if llm is None:
        return base, []
    try:
        out = await llm.structured(tier="large", system=COMMANDER_SYSTEM, user=data_block("command", command),
                                   schema=LLMSlots, effort="low", purpose="commander")
    except Exception as exc:
        log.warning("commander_llm_failed", error=str(exc)[:200])
        return base, []
    merged = base.model_copy(update={
        "category": base.category or (resolve_category(out.category_text) if out.category_text else None),
        "kind": base.kind or (ContentKind(out.kind) if out.kind else None),
        "persona": base.persona or (out.persona or None),
        "platforms": base.platforms or [Platform(p) for p in out.platforms],
        "budget_krw": base.budget_krw or (out.budget_krw or None),
        "keyword": base.keyword or (out.keyword or None),
        "count": base.count if _count(command) else max(1, min(out.count, 50)),
    })
    return merged, list(out.ambiguities)
