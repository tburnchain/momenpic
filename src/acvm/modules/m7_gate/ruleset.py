"""Rule set v1 (§8.1) — the declarative registry. Ordering matters only for reporting; verdict
aggregation is order-independent (worst wins). The fingerprint is logged with every check so an
audit can prove which exact rules judged a piece of content.
"""

from __future__ import annotations

from dataclasses import dataclass

from acvm.core.enums import Verdict
from acvm.core.ids import stable_hash
from acvm.policy.lexicon import LEXICON_VERSION


@dataclass(frozen=True, slots=True)
class RuleSpec:
    id: str
    check: str
    method: str
    on_fail: Verdict
    fix_action: str = ""  # inplace | regenerate | rerender | reschedule | set_flags | ""
    stages: tuple[str, ...] = ("gate", "preflight")


RULES_V1: tuple[RuleSpec, ...] = (
    RuleSpec("D-01", "대가성 문구가 캡션·본문 첫 줄에 있는가", "정규식 + 위치 검사", Verdict.FIX, "inplace"),
    RuleSpec("D-02", "문구가 '수수료를 제공받는다'는 경제적 대가를 명시하는가", "허용 문구 화이트리스트 + 필수 요소", Verdict.FIX,
             "inplace"),
    RuleSpec("D-03", "영상 첫 3초 화면에 '광고' 표시 오버레이가 있는가", "렌더 메타데이터 + 프레임 검증", Verdict.BLOCK,
             stages=("gate",)),
    RuleSpec("D-04", "링크 허브 페이지 최상단 고정 문구", "페이지 빌드 검사", Verdict.BLOCK, stages=("preflight",)),
    RuleSpec("C-01", "근거 없는 사실·효능 주장", "주장-근거 매핑 + LLM 판정", Verdict.FIX, "inplace"),
    RuleSpec("C-02", "최상급·절대 표현", "금칙어 사전", Verdict.FIX, "inplace"),
    RuleSpec("C-03", "가격·할인 표기가 실시간 가격과 일치", "게시 직전 가격 재조회", Verdict.FIX, "inplace"),
    RuleSpec("C-04", "규제 카테고리 상품", "카테고리 화이트리스트 + 규제 품목 사전", Verdict.BLOCK),
    RuleSpec("R-01", "권리 원장에 없는 에셋 사용", "에셋 ID 대조 + 라이선스 만료", Verdict.BLOCK, stages=("gate",)),
    RuleSpec("R-02", "레퍼런스와의 유사도", "코사인 0.80, 5-gram 15%", Verdict.FIX, "regenerate", stages=("gate",)),
    RuleSpec("R-03", "상표·캐릭터·유명인 얼굴/음성", "비전 분류기 + 보이스 출처 + 고유명 사전", Verdict.HOLD,
             stages=("gate",)),
    RuleSpec("S-01", "계정 최근 30일 콘텐츠와 중복", "MinHash 0.6", Verdict.FIX, "regenerate", stages=("gate",)),
    RuleSpec("S-02", "계정 쿼터·쿨다운 상태", "쿼터 관리자", Verdict.FIX, "reschedule"),
    RuleSpec("A-01", "AI 생성물 표시", "플랫폼 라벨 필드 설정", Verdict.FIX, "set_flags"),
    RuleSpec("H-01", "신규 계정 첫 14일, 신규 템플릿 첫 10건", "정책", Verdict.HOLD, stages=("gate",)),
    RuleSpec("L-01", "딥링크 파트너 ID·서명 무결성, 링크 상태", "HMAC 서명 + lptag 검증 + 헬스", Verdict.BLOCK,
             stages=("preflight",)),
    RuleSpec("G-00", "게이트 우회 시도(승인되지 않은 콘텐츠 게시)", "콘텐츠 상태 확인", Verdict.BLOCK, stages=("preflight",)),
)

RULE_INDEX = {r.id: r for r in RULES_V1}


def fingerprint(version: str = "v1") -> str:
    payload = {"version": version, "lexicon": LEXICON_VERSION,
               "rules": [(r.id, r.on_fail.value, r.fix_action, r.stages) for r in RULES_V1]}
    return stable_hash(payload, length=16)


def describe() -> list[dict[str, str]]:
    return [{"id": r.id, "check": r.check, "method": r.method, "on_fail": r.on_fail.value} for r in RULES_V1]
