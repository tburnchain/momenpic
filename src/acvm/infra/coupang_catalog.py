"""Coupang Partners category vocabulary and the M1 hard-exclusion lexicon.

* ``CATEGORY_NAMES`` — the top-level category IDs accepted by ``products/bestcategories/{categoryId}``.
* ``NICHE_TO_CATEGORY`` / ``resolve_category`` — §5.3 slot grammar: the ``category`` slot ("주방, 차량,
  정리수납" or the account niche) is resolved to a category ID by finding the niche word *inside* a
  free-form Korean command ("주방 카테고리로 쿠팡 파트너스 5개 …").
* ``REGULATED_CATEGORIES`` / ``is_regulated`` — §4 M1 하드 배제 목록: 의약품·의약외품 효능 표현 필요 상품,
  건강기능식품, 의료기기, 주류·담배, 성인용품, 미성년 대상 위험 제품. The same list backs gate rule C-04
  (§8.1, BLOCK), so matching is deliberately conservative: a false positive costs one candidate, a false
  negative can cost the account (표시광고법).

Korean is agglutinative, so terms are matched as substrings of the NFKC-normalised name — except where a
term is also a common morpheme of unrelated words (술 ⊂ 기술/미술, 파스 ⊂ 파스타/파스텔, 와인 ⊂ 와인레드),
and except accessory compounds (와인잔, 맥주컵, 치약 디스펜서) which are kitchen/bath goods, not the
regulated good itself.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

CATEGORY_NAMES: Final[dict[str, str]] = {
    "1001": "여성패션",
    "1002": "남성패션",
    "1010": "뷰티",
    "1011": "출산/유아동",
    "1012": "식품",
    "1013": "주방용품",
    "1014": "생활용품",
    "1015": "홈인테리어",
    "1016": "가전디지털",
    "1017": "스포츠/레저",
    "1018": "자동차용품",
    "1019": "도서/음반/DVD",
    "1020": "완구/취미",
    "1021": "문구/오피스",
    "1024": "헬스/건강식품",
    "1025": "국내여행",
    "1026": "해외여행",
    "1029": "반려동물용품",
    "1030": "유아동패션",
}

CATEGORY_IDS_BY_NAME: Final[dict[str, str]] = {name: cid for cid, name in CATEGORY_NAMES.items()}

# Niche word → category ID. Korean aliases match as substrings (particles attach directly to the
# stem: "주방으로", "차량용품"), English aliases match as whole words only ("car" ⊄ "card").
NICHE_TO_CATEGORY: Final[dict[str, str]] = {
    # 1013 주방용품
    "주방용품": "1013", "주방": "1013", "키친": "1013", "kitchen": "1013",
    # 1018 자동차용품
    "자동차용품": "1018", "자동차": "1018", "차량용품": "1018", "차량": "1018", "카용품": "1018",
    "car": "1018", "auto": "1018",
    # 1014 생활용품 (정리수납)
    "생활용품": "1014", "정리수납": "1014", "수납": "1014", "정리": "1014", "생활": "1014",
    "storage": "1014", "living": "1014", "household": "1014",
    # 1015 홈인테리어
    "홈인테리어": "1015", "인테리어": "1015", "interior": "1015",
    # 1016 가전디지털
    "가전디지털": "1016", "가전": "1016", "디지털": "1016", "electronics": "1016",
    # 1029 반려동물용품
    "반려동물용품": "1029", "반려동물": "1029", "반려": "1029", "애견": "1029", "애묘": "1029",
    "pet": "1029", "pets": "1029",
    # 1011 출산/유아동
    "출산": "1011", "육아": "1011", "유아동": "1011", "baby": "1011",
    # 1030 유아동패션
    "유아동패션": "1030", "아동복": "1030",
    # 1010 뷰티
    "뷰티": "1010", "화장품": "1010", "beauty": "1010",
    # 1012 식품
    "식품": "1012", "먹거리": "1012", "food": "1012",
    # 1017 스포츠/레저
    "스포츠": "1017", "레저": "1017", "캠핑": "1017", "등산": "1017", "sports": "1017",
    # 1020 완구/취미
    "완구": "1020", "장난감": "1020", "취미": "1020", "toys": "1020",
    # 1021 문구/오피스
    "문구": "1021", "오피스": "1021", "사무용품": "1021", "office": "1021",
    # 1019 도서/음반/DVD
    "도서": "1019", "음반": "1019", "books": "1019",
    # 1001 / 1002 패션
    "여성패션": "1001", "여성의류": "1001", "남성패션": "1002", "남성의류": "1002",
    # 1025 / 1026 여행
    "국내여행": "1025", "해외여행": "1026",
    # 1024 헬스/건강식품 — resolvable so a command for it is *refused* (regulated), not silently remapped
    "헬스/건강식품": "1024", "건강기능식품": "1024", "건강식품": "1024",
}

REGULATED_CATEGORIES: Final[frozenset[str]] = frozenset({"1024"})

_ASCII_ALIAS = re.compile(r"^[a-z]+$")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def resolve_category(text: str) -> str | None:
    """Return the category ID named inside a free-form command, or ``None``.

    Resolution order: an explicit known category ID ("1013"), then the longest niche alias found in the
    text (so "정리수납" beats "정리", "자동차용품" beats "자동차"); ties go to the earliest occurrence.
    """
    s = _norm(text)
    for match in re.finditer(r"(?<!\d)(10\d{2})(?!\d)", s):
        if match.group(1) in CATEGORY_NAMES:
            return match.group(1)
    best: tuple[int, int, str] | None = None  # (-length, position, category)
    for alias, cid in NICHE_TO_CATEGORY.items():
        if _ASCII_ALIAS.match(alias):
            m = re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", s)
            pos = m.start() if m else -1
        else:
            pos = s.find(alias)
        if pos < 0:
            continue
        cand = (-len(alias), pos, cid)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


# ─────────────── §4 M1 hard-exclusion lexicon ───────────────

# A regulated term immediately followed by one of these is an accessory for the good, not the good
# itself (와인잔, 맥주 컵, 치약 디스펜서, 비타민 샤워필터, 소독제 거치대).
_ACCESSORY: Final[tuple[str, ...]] = (
    "잔", "컵", "글라스", "오프너", "따개", "마개", "랙", "셀러", "냉장고", "거치대", "홀더", "케이스",
    "보관함", "정리함", "디스펜서", "짜개", "꽂이", "스탠드", "트레이", "선반", "셰이커", "디캔터", "병따개",
    "파우치", "샤워", "필터",
)
_ACC = "|".join(sorted(_ACCESSORY, key=len, reverse=True))

# Regex fragments (Hangul literals unless noted). Colour names and food words that merely contain a
# term are carved out with look-aheads: 파스타/파스텔 are not 파스, 와인레드 is a colour, not wine.
_TERM_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    # 의약품 · 의약외품 (efficacy claims are regulated; KF 마스크·치약·생리대·소독제 are 의약외품 in Korea)
    "drug": (
        "의약품", "의약외품", "일반의약품", "전문의약품", "상비약", "진통제", "해열제", "소화제", "감기약",
        "파스(?![타텔])", "연고", "안약", "인공눈물", "kf94", "kf80", "kf-ad", "보건용마스크", "손소독제",
        "소독제", "치약", "생리대", "탐폰", "니코틴패치", "금연보조",
    ),
    # 건강기능식품 · 다이어트 보조제
    "supplement": (
        "건강기능식품", "건기식", "영양제", "종합비타민", "비타민", "유산균", "프로바이오틱스", "오메가3",
        "루테인", "밀크씨슬", "홍삼", "콜라겐정", "글루코사민", "다이어트보조제", "체지방감소", "식욕억제",
        "가르시니아",
    ),
    # 의료기기
    "medical_device": (
        "의료기기", "의료용", "혈압계", "혈당계", "혈당측정", "체온계", "산소포화도", "저주파치료기", "치료기",
        "보청기", "콘택트렌즈", "렌즈세척액", "임신테스트", "배란테스트",
    ),
    # 주류 · 담배
    "alcohol_tobacco": (
        "주류", "소주", "맥주", "와인(?!레드|컬러|색|빛|딩)", "위스키", "보드카", "막걸리", "사케", "데킬라",
        "브랜디", "코냑", "샴페인(?!골드|컬러|색)", "리큐르", "전통주", "하이볼", "전자담배", "액상담배", "담배",
        "궐련", "니코틴",
    ),
    # 성인용품
    "adult": ("성인용품", "콘돔", "러브젤", "19금"),
    # 미성년 대상 위험 제품
    "minor_hazard": ("비비탄", "에어소프트", "전동건", "가스건", "레이저포인터"),
}

_TERM_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (group, re.compile(rf"(?:{fragment})(?!(?:{_ACC}))"))
    for group, fragments in _TERM_GROUPS.items()
    for fragment in fragments
)
# 술 is also a morpheme of 기술/미술/수술/마술: only a standalone token counts.
_STANDALONE_ALCOHOL: Final[re.Pattern[str]] = re.compile(r"(?<![가-힣])술(?![가-힣])")


def _squash(text: str) -> str:
    """NFKC, lower-case, and drop whitespace before a Hangul syllable so "다이어트 보조제" and
    "다이어트보조제" (or "와인 잔" and "와인잔") hit the same lexicon entry."""
    return re.sub(r"(?<=[가-힣0-9a-z])\s+(?=[가-힣])", "", _norm(text))


def regulated_terms(name: str) -> list[str]:
    """Lexicon groups hit by ``name`` (recorded in audit/explanations); empty when the name is clean."""
    squashed = _squash(name)
    hits: list[str] = []
    for group, pattern in _TERM_PATTERNS:
        if group not in hits and pattern.search(squashed):
            hits.append(group)
    if "alcohol_tobacco" not in hits and _STANDALONE_ALCOHOL.search(_norm(name)):
        hits.append("alcohol_tobacco")
    return hits


def is_regulated(name: str, category_id: str) -> bool:
    """§4 M1 하드 배제 / §8.1 C-04: regulated category, or a regulated good named in the title."""
    return category_id in REGULATED_CATEGORIES or bool(regulated_terms(name))
