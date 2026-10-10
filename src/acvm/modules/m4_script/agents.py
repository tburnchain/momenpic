"""Writer and Critic agents (§5.1).

Writer  — medium tier. Tools: product facts, DNA hints, playbook KB. Produces hooks/beats/claims.
Critic  — large tier, deliberately a *different model* from the Writer, read-only. Judges every claim
          against the facts and flags superlatives / efficacy / fake-experience / click-bait lines.

System prompts are frozen strings (prompt-cache friendly); all variable and untrusted content goes
into the user turn inside <data> blocks (§10 injection containment).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from acvm.security.prompt_guard import DATA_POLICY

Role = Literal["hook", "problem", "amplify", "reveal", "benefits", "cta"]
Visual = Literal["hook_text", "problem_scene", "product_closeup", "product_in_use", "benefit_card", "cta_card"]
HookT = Literal["question", "shock", "before_after", "price_twist", "empathy"]


class DraftClaim(BaseModel):
    id: str = Field(description="c1, c2, …")
    text: str = Field(description="The exact sentence as it appears in the script")
    kind: Literal["fact", "price", "spec", "review", "shipping", "opinion", "cta"]
    evidence: list[str] = Field(description="Fact keys that prove this sentence; [] only for opinion/cta")


class DraftBeat(BaseModel):
    role: Role
    narration: str
    on_screen_text: str
    visual: Visual
    claim_ids: list[str]


class ScriptDraft(BaseModel):
    hooks: list[str] = Field(description="Exactly three alternative opening lines")
    chosen_hook: int = Field(description="Index 0-2 of the hook used in the first beat")
    hook_type: HookT
    beats: list[DraftBeat]
    claims: list[DraftClaim]
    body: str = Field(description="Text-post body for text kind; short description for video kind")
    caption: str
    hashtags: list[str]
    cta: str
    title: str


WRITER_SYSTEM = f"""당신은 한국어 숏폼·스레드 제휴 콘텐츠 작가입니다. 쿠팡 파트너스 상품을 소개하는 대본을 씁니다.

반드시 지킬 규칙:
1. 사실 주장은 제공된 상품 팩트(fact key = value 목록)에 있는 값만 사용합니다. 팩트에 없는 효능·스펙·수치·후기·순위·가격을 지어내지 않습니다.
2. 사실을 담은 문장(숫자, 리뷰, 평점, 배송, 구성, 소재, 크기, 가격)은 모두 claims에 등록하고 evidence에 근거 fact key를 적습니다. 가격은 팩트에 price가 있을 때만 말합니다.
3. 최상급·절대 표현 금지: 최고, 최저가, 무조건, 100%, 완벽, 유일, 1위, 역대급, 평생, 보장 등.
4. 의약·건강 효능 표현 금지(치료, 예방, 면역, 다이어트 효과 등). 체험 위장 금지("제가 써봤는데", "내돈내산", "써보니") — 팩트에 spec.operator_used=true가 있을 때만 허용.
5. 클릭 유도 금지("클릭만 해주세요" 등). 상품과 무관한 내용 금지.
6. 대가성 문구는 시스템이 첫 줄에 자동 삽입하므로 본문에 쓰지 않습니다.
7. 페르소나의 말투와 맥락을 따르되 실존 인물 사칭은 하지 않습니다.
8. 비트 구조와 역할(role), 비트별 음절 예산을 지킵니다(한국어 약 6.5음절/초). 첫 3초 훅은 한 문장, 12~20음절.
9. CTA는 지정된 링크 위치 안내 문구를 사용합니다.
10. 최근 같은 계정에 쓴 훅과 겹치지 않게 새로운 표현을 씁니다.

출력은 지정된 JSON 스키마만 따릅니다.

{DATA_POLICY}"""


class ClaimJudgement(BaseModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "exaggerated", "not_a_claim"]
    reason: str
    rewrite: str = Field(description="A grounded replacement sentence, or empty string to delete")


class SentenceIssue(BaseModel):
    sentence: str
    issue: Literal["ungrounded_fact", "superlative", "efficacy", "fake_experience", "click_bait", "off_topic", "other"]
    fix: Literal["delete", "rewrite"]
    rewrite: str


class CriticReport(BaseModel):
    judgements: list[ClaimJudgement]
    issues: list[SentenceIssue]
    overall_ok: bool


CRITIC_SYSTEM = f"""You are the compliance critic for Korean affiliate marketing scripts (표시광고법, 공정위 추천·보증 심사지침).
You never write marketing copy. For each registered claim decide whether the listed evidence facts fully support it
(numbers must match exactly; qualitative claims must follow directly from a fact). Then scan every sentence of the
script for: factual statements not registered as claims, superlatives/absolute expressions, health or medical efficacy,
fabricated first-person experience, click-bait, and content unrelated to the product. Prefer deletion over creative
rewrites; a rewrite must itself be fully supported by the facts. Be strict: when in doubt, mark unsupported.

{DATA_POLICY}"""
