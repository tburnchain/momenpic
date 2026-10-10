"""Category priors for M1 (§4 M1 POS features, §2.5 KPI tree "CVR × AOV ← M1 상품 선별", "r ← 카테고리 믹스").

The Partners search API returns neither ratings nor review counts, so POS imputes them from these
per-category priors (and lists the feature in ``ProductFeatures.imputed``). ``return_risk`` seeds
P_risk ("반품률 높은 카테고리", §4 M1 / §2.1 L_invalid); ``commission_rate`` is the prior for r_cat that
observed M9 commission/GMV shrinks towards; ``demo_base`` is the V_demo heuristic's starting point
(how naturally the category films as a 15-second before/after).

These are operator-maintained starting values, superseded by measured data as it accumulates (§2.5
"가정 — 실측으로 교체").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class CategoryPrior:
    rating: float  # prior mean star rating of a best-category listing
    review_count: int  # prior typical review count of a best-category listing
    return_risk: float  # prior P(return / cancel) → P_risk base, in [0, 1]
    commission_rate: float  # prior Partners commission rate r_cat
    demo_base: float  # V_demo heuristic baseline in [0, 1]


DEFAULT_PRIOR: Final = CategoryPrior(rating=4.4, review_count=500, return_risk=0.12, commission_rate=0.03,
                                     demo_base=0.35)

CATEGORY_PRIORS: Final[dict[str, CategoryPrior]] = {
    "1001": CategoryPrior(4.3, 300, 0.35, 0.03, 0.35),  # 여성패션 — size/fit returns
    "1002": CategoryPrior(4.3, 250, 0.30, 0.03, 0.35),  # 남성패션
    "1010": CategoryPrior(4.5, 800, 0.12, 0.03, 0.40),  # 뷰티
    "1011": CategoryPrior(4.6, 600, 0.10, 0.03, 0.45),  # 출산/유아동
    "1012": CategoryPrior(4.5, 1_500, 0.08, 0.03, 0.30),  # 식품 — taste does not film
    "1013": CategoryPrior(4.5, 900, 0.06, 0.03, 0.50),  # 주방용품
    "1014": CategoryPrior(4.5, 1_200, 0.06, 0.03, 0.50),  # 생활용품
    "1015": CategoryPrior(4.4, 500, 0.12, 0.03, 0.45),  # 홈인테리어
    "1016": CategoryPrior(4.4, 700, 0.15, 0.03, 0.40),  # 가전디지털
    "1017": CategoryPrior(4.4, 400, 0.15, 0.03, 0.40),  # 스포츠/레저
    "1018": CategoryPrior(4.4, 800, 0.08, 0.03, 0.50),  # 자동차용품
    "1019": CategoryPrior(4.7, 300, 0.03, 0.03, 0.10),  # 도서/음반/DVD
    "1020": CategoryPrior(4.5, 400, 0.10, 0.03, 0.45),  # 완구/취미
    "1021": CategoryPrior(4.5, 600, 0.06, 0.03, 0.40),  # 문구/오피스
    "1024": CategoryPrior(4.5, 1_000, 1.00, 0.03, 0.20),  # 헬스/건강식품 — regulated (hard-excluded)
    "1025": CategoryPrior(4.4, 100, 0.25, 0.03, 0.10),  # 국내여행
    "1026": CategoryPrior(4.4, 100, 0.25, 0.03, 0.10),  # 해외여행
    "1029": CategoryPrior(4.5, 700, 0.08, 0.03, 0.45),  # 반려동물용품
    "1030": CategoryPrior(4.4, 300, 0.30, 0.03, 0.35),  # 유아동패션
}


def prior_for(category_id: str) -> CategoryPrior:
    return CATEGORY_PRIORS.get(category_id, DEFAULT_PRIOR)
