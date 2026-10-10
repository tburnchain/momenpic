"""Structure templates (20–25 s fast product pitch) and personas.

Shorts are 20–25 second product pitches delivered FAST and CONCISE (operator directive 2026-10-08). Each template is
a beat skeleton whose narration budget is computed at the fast delivery rate the renderer enforces
(``RenderSpec.target_speech_sps`` ≈ 8 Korean syllables per second), so scripts are short by construction — one
punchy sentence per beat, no narrator-style padding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acvm.core.enums import ContentKind, HookType

SYLLABLES_PER_SECOND = 8.0  # fast shorts delivery (== RenderSpec.target_speech_sps)
SHORT_MIN_SECONDS = 20.0
SHORT_MAX_SECONDS = 25.0


@dataclass(frozen=True)
class BeatSpec:
    role: str  # hook | problem | amplify | reveal | benefits | cta
    start: float
    end: float
    visual: str
    purpose: str

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def syllable_budget(self) -> int:
        return int(self.seconds * SYLLABLES_PER_SECOND)


@dataclass(frozen=True)
class Template:
    id: str
    kind: ContentKind
    beats: tuple[BeatSpec, ...]
    preferred_hooks: tuple[HookType, ...]
    description: str

    @property
    def duration(self) -> float:
        return self.beats[-1].end if self.beats else 0.0


TEMPLATES: dict[str, Template] = {
    "problem-solution-v3": Template(
        id="problem-solution-v3", kind=ContentKind.SHORT,
        description="22초 속도감 있는 구성: 문제 제기 → 불편 증폭 → 상품 등장 → 장점 → CTA",
        preferred_hooks=(HookType.QUESTION, HookType.EMPATHY, HookType.SHOCK),
        beats=(
            BeatSpec("hook", 0, 3, "hook_text", "문제 제기 — 한 문장, 3초 안에"),
            BeatSpec("amplify", 3, 6.5, "problem_scene", "불편 증폭 — 짧게 한 문장"),
            BeatSpec("reveal", 6.5, 11, "product_closeup", "상품 등장 — 무엇이 해결되는지 한 문장"),
            BeatSpec("benefits", 11, 18, "product_in_use", "장점 2~3개 — 근거 있는 사실만, 짧게"),
            BeatSpec("cta", 18, 22, "cta_card", "행동 유도 — 링크 위치 한 문장"),
        ),
    ),
    "before-after-v1": Template(
        id="before-after-v1", kind=ContentKind.SHORT,
        description="22초: 비포 → 전환 → 애프터·근거 → CTA",
        preferred_hooks=(HookType.BEFORE_AFTER, HookType.SHOCK),
        beats=(
            BeatSpec("hook", 0, 3, "hook_text", "전후 비교 예고"),
            BeatSpec("problem", 3, 7, "problem_scene", "비포 — 사용 전의 불편 한 문장"),
            BeatSpec("reveal", 7, 11, "product_closeup", "전환 — 상품 등장"),
            BeatSpec("benefits", 11, 18, "product_in_use", "애프터 — 달라진 점과 근거, 짧게"),
            BeatSpec("cta", 18, 22, "cta_card", "행동 유도"),
        ),
    ),
    "price-twist-v1": Template(
        id="price-twist-v1", kind=ContentKind.SHORT,
        description="21초: 가격 반전 훅 → 상품 → 가성비 근거 → CTA (가격은 신선할 때만)",
        preferred_hooks=(HookType.PRICE_TWIST, HookType.QUESTION),
        beats=(
            BeatSpec("hook", 0, 3, "hook_text", "가격 반전 — 부담 없는 가격대라는 반전 한 문장"),
            BeatSpec("reveal", 3, 8, "product_closeup", "상품 등장과 용도"),
            BeatSpec("benefits", 8, 17, "benefit_card", "가성비 근거 — 구성, 리뷰 수, 배송"),
            BeatSpec("cta", 17, 21, "cta_card", "행동 유도"),
        ),
    ),
    "listicle-v1": Template(
        id="listicle-v1", kind=ContentKind.SHORT,
        description="23초: '이거 하나로 3가지 해결' 리스트형",
        preferred_hooks=(HookType.QUESTION, HookType.EMPATHY),
        beats=(
            BeatSpec("hook", 0, 3, "hook_text", "리스트 예고 — 하나로 해결되는 3가지"),
            BeatSpec("reveal", 3, 7.5, "product_closeup", "상품 소개 한 문장"),
            BeatSpec("benefits", 7.5, 19, "benefit_card", "해결되는 3가지 — 각 한 문장, 근거 포함"),
            BeatSpec("cta", 19, 23, "cta_card", "행동 유도"),
        ),
    ),
    "text-v1": Template(
        id="text-v1", kind=ContentKind.TEXT,
        description="Threads/블로그형 글: 훅 한 줄 → 공감 2줄 → 상품과 근거 3개 → CTA",
        preferred_hooks=(HookType.QUESTION, HookType.EMPATHY, HookType.PRICE_TWIST),
        beats=(),
    ),
}

DEFAULT_TEMPLATE = {ContentKind.SHORT: "problem-solution-v3", ContentKind.TEXT: "text-v1"}


@dataclass(frozen=True)
class Persona:
    name: str
    voice_hint: str
    tone: str
    register: str  # 말투
    openers: tuple[str, ...] = field(default_factory=tuple)  # sentence-initial phrases that fit any clause


PERSONAS: dict[str, Persona] = {
    "30대 여성 직장인": Persona("30대 여성 직장인", "ko-F-30s-brisk", "빠르고 현실적인 공감형",
                           "짧게 끊는 해요체, 퇴근 후 일상 맥락", ("퇴근하고 보면", "솔직히", "진짜로")),
    "20대 자취생": Persona("20대 자취생", "ko-F-20s-bright", "가볍고 빠른 리듬",
                       "반말 섞인 해요체, 자취방 맥락", ("자취하다 보면", "솔직히", "좁은 방에선 특히")),
    "40대 주부": Persona("40대 주부", "ko-F-40s-lively", "생기 있고 빠른 살림 비교형",
                     "짧은 해요체, 살림 맥락", ("살림하다 보면", "매일 쓰다 보면", "아무래도")),
    "30대 남성 직장인": Persona("30대 남성 직장인", "ko-M-30s-brisk", "빠르고 담백한 정보형",
                          "간결한 해요체, 출퇴근·차량 맥락", ("출퇴근하다 보면", "솔직히", "의외로")),
}


def resolve_persona(text: str | None, default: str = "30대 여성 직장인") -> Persona:
    if not text:
        return PERSONAS[default]
    if text in PERSONAS:
        return PERSONAS[text]
    t = text.replace(" ", "")
    male = any(w in t for w in ("남자", "남성"))
    if "20대" in t or "자취" in t:
        return PERSONAS["20대 자취생"]
    if "40대" in t or "주부" in t:
        return PERSONAS["40대 주부"]
    if male:
        return PERSONAS["30대 남성 직장인"]
    if "30대" in t or "여자" in t or "여성" in t:
        return PERSONAS["30대 여성 직장인"]
    return Persona(text, "ko-F-30s-brisk", "빠르고 자연스러운 공감형", "짧은 해요체")


def estimate_seconds(narrations: list[str], *, cta_tail: float = 1.2) -> float:
    """Spoken length of a short at the fast delivery rate (+ the CTA hold the renderer appends)."""
    from acvm.media.korean import estimate_syllables

    syllables = sum(estimate_syllables(w) for text in narrations for w in text.split())
    return syllables / SYLLABLES_PER_SECOND + cta_tail
