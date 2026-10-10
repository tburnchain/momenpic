"""Compliance lexicons (§8.1 C-02, C-01, D-02; §8.2). Versioned with the rule set — a change here is a
rule change (owner/reviewer only, audited) and bumps ``LEXICON_VERSION``.

Sources: 표시·광고의 공정화에 관한 법률 (부당 표시·광고 유형), 공정위 추천·보증 심사지침 (2024-12-01 개정),
and the Coupang Partners disclosure guidance.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LEXICON_VERSION = "lex-2026.10.1"


@dataclass(frozen=True, slots=True)
class LexHit:
    term: str
    category: str  # superlative | efficacy | fake_experience | click_bait | guarantee
    start: int
    end: int
    replacement: str | None


# term → safer replacement ("" = delete the term; None = sentence must be removed)
SUPERLATIVES: dict[str, str] = {
    "최고의": "만족도 높은", "최고": "만족스러운", "최상의": "좋은", "최강": "든든한", "최저가": "",
    "무조건": "한 번쯤", "100%": "", "백퍼센트": "", "백퍼": "", "완벽하게": "깔끔하게", "완벽한": "깔끔한",
    "완벽": "깔끔", "유일한": "", "유일하게": "", "단연": "", "역대급": "눈에 띄는", "역대 최고": "",
    "세계 최초": "", "국내 최초": "", "절대": "", "평생": "오래", "영원히": "오래", "기적의": "",
    "기적같은": "", "혁명적인": "새로운", "1등": "", "1위": "", "넘버원": "", "No.1": "", "보장": "",
}

EFFICACY: tuple[str, ...] = (
    "치료", "완치", "예방", "효능", "약효", "의학적", "임상", "면역력", "혈압", "혈당", "해독", "디톡스",
    "피부 재생", "탈모 방지", "살 빠지", "살이 빠", "다이어트 효과", "체지방 감소", "부작용 없", "통증 완화",
    "염증", "항암", "숙면 효과", "피로 회복",
)

# Claims that require a verified spec to be stated (e.g. lab-tested antibacterial ratios)
SPEC_REQUIRED: dict[str, str] = {"항균": "spec.antibacterial", "살균": "spec.sterilization", "99.9": "spec.test_report",
                                 "KC인증": "spec.kc_cert", "친환경": "spec.eco_cert", "무독성": "spec.non_toxic_cert"}

FAKE_EXPERIENCE: tuple[str, ...] = (
    "제가 써봤", "직접 써보", "직접 사용해", "써봤어요", "써보니", "사용해보니", "내돈내산", "실사용 후기", "제가 샀",
    "저희 집에서 써", "한 달 써", "일주일 써",
)

CLICK_BAIT: tuple[str, ...] = (
    "클릭만 해주세요", "클릭만 해 주세요", "클릭 부탁", "링크만 눌러", "구매 안 해도", "눌러만 주세요", "클릭해주시면 저에게",
)

GUARANTEE: tuple[str, ...] = ("환불 보장", "효과 보장", "만족 보장", "품절 임박", "오늘만 이 가격", "마지막 기회")

_FIXED: list[tuple[str, str, str | None]] = [(t, "superlative", r) for t, r in SUPERLATIVES.items()]
_FIXED += [(t, "efficacy", None) for t in EFFICACY]
_FIXED += [(t, "fake_experience", None) for t in FAKE_EXPERIENCE]
_FIXED += [(t, "click_bait", None) for t in CLICK_BAIT]
_FIXED += [(t, "guarantee", None) for t in GUARANTEE]
_FIXED.sort(key=lambda x: -len(x[0]))  # longest match first ("최고의" before "최고")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


_norm = normalize


def scan(text: str) -> list[LexHit]:
    """All non-overlapping lexicon hits, longest-first."""
    s = _norm(text)
    taken = [False] * len(s)
    hits: list[LexHit] = []
    for term, cat, repl in _FIXED:
        for m in re.finditer(re.escape(term), s):
            if any(taken[m.start() : m.end()]):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            hits.append(LexHit(term, cat, m.start(), m.end(), repl))
    return sorted(hits, key=lambda h: h.start)


def spec_required_hits(text: str, fact_keys: set[str]) -> list[tuple[str, str]]:
    s = _norm(text)
    return [(term, key) for term, key in SPEC_REQUIRED.items() if term in s and key not in fact_keys]


def soften(text: str) -> tuple[str, list[LexHit]]:
    """Replace replaceable superlatives; return the new text and the hits that need sentence removal."""
    hits = scan(text)
    s = _norm(text)
    out, last, unresolved = [], 0, []
    for h in hits:
        if h.replacement is None:
            unresolved.append(h)
            continue
        out.append(s[last : h.start])
        out.append(h.replacement)
        last = h.end
    out.append(s[last:])
    joined = "".join(out)
    # collapse runs of spaces left by deletions, but keep line structure (the disclosure must stay line 1)
    softened = "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in joined.split("\n")).strip()
    return softened, unresolved


# ── disclosure (D-01, D-02) ──
_DISCLOSURE_REQUIRED = (re.compile(r"쿠팡\s*파트너스"), re.compile(r"수수료"), re.compile(r"(제공\s*받|받습니다|받아요|지급)"))
DISCLOSURE_WHITELIST: tuple[str, ...] = (
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
    "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
    "이 영상은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.",
    "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다",
)


def is_valid_disclosure(line: str) -> bool:
    """D-02: names the program AND states the economic consideration (수수료 + 받음)."""
    s = _norm(line).strip()
    if s in DISCLOSURE_WHITELIST:
        return True
    return all(p.search(s) for p in _DISCLOSURE_REQUIRED)
