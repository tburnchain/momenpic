"""Category resolution (§5.3 category slot) and the §4 M1 hard-exclusion lexicon."""

from __future__ import annotations

import pytest

from acvm.infra.coupang_catalog import (
    CATEGORY_NAMES,
    NICHE_TO_CATEGORY,
    REGULATED_CATEGORIES,
    is_regulated,
    regulated_terms,
    resolve_category,
)


@pytest.mark.parametrize(("command", "expected"), [
    ("주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘", "1013"),
    ("매일 오전 9시에 차량용품 10개", "1018"),
    ("자동차용품 세 개 올려줘", "1018"),
    ("정리수납 3개 만들기만", "1014"),
    ("생활용품 5개", "1014"),
    ("인테리어 소품 위주로", "1015"),
    ("가전 2개", "1016"),
    ("반려동물 간식 말고 용품", "1029"),
    ("육아템 추천", "1011"),
    ("kitchen picks x5", "1013"),
    ("car accessories please", "1018"),
    ("1018 카테고리 베스트", "1018"),
    ("건강식품 5개", "1024"),
    ("card holder", None),  # "car" must not match inside "card"
    ("분석해줘", None),
])
def test_resolve_category(command: str, expected: str | None) -> None:
    assert resolve_category(command) == expected


def test_every_alias_points_at_a_known_category() -> None:
    assert set(NICHE_TO_CATEGORY.values()) <= set(CATEGORY_NAMES)
    assert "1024" in REGULATED_CATEGORIES and len(REGULATED_CATEGORIES) == 1


@pytest.mark.parametrize("name", [
    "종합비타민 120정", "프로바이오틱스 유산균 60캡슐", "다이어트 보조제 60정", "KF94 보건용 마스크 50매",
    "레드 와인 750ml 2병", "캔 맥주 500ml 24캔", "전통 술 선물세트", "전자담배 액상 30ml", "디지털 체온계",
    "가정용 혈압계 자동", "콘돔 12개입", "비비탄 권총 세트", "쿨링 파스 20매", "손소독제 500ml",
])
def test_regulated_goods_are_detected(name: str) -> None:
    assert is_regulated(name, "1014"), regulated_terms(name)


@pytest.mark.parametrize("name", [
    "과자 밀봉 클립 20개입 대용량 세트", "와인잔 크리스탈 4P 세트", "맥주 컵 500ml 6P", "특허기술 밀폐용기",
    "파스타 냄비 24cm", "파스텔 톤 수납함", "와인레드 니트 가디건", "치약 디스펜서 자동", "비타민 샤워필터",
    "손소독제 거치대 스탠드", "미술 도구 세트", "수술용 아닌 일반 고무장갑",
])
def test_accessories_and_lookalikes_are_not_regulated(name: str) -> None:
    assert not is_regulated(name, "1013"), regulated_terms(name)


def test_regulated_category_excludes_everything() -> None:
    assert is_regulated("평범한 물병", "1024")
