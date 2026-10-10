"""Playbook KB rule mining (§4 M10 "플레이북 KB": 학습 결과를 자연어 규칙으로 요약 → M4 프롬프트에 주입).

For a factor f = v (hook type, template, persona, platform, slot, category) inside a context c
(the whole workspace, or one category × platform pair):

    baseline  B ~ posterior of revenue-per-impression for all arms in c   (economics prior + pooled data)
    factor    F ~ posterior for arms in c with f = v, prior *centred on B* (κ pseudo-contents)
    lift      = E[RPI_F] / E[RPI_B] − 1
    P(lift>0) = (1/S) Σ_s 1[RPI_F^(s) > RPI_B^(s)],   S Monte-Carlo draws, RPI = CTR · λ / θ

Centring F's prior on B shrinks the lift toward 0, so a rule needs real evidence: it is written only when
P(lift > 0) ≥ 0.9 and the factor has ≥ 10 observed publications (trials). Draws use a numpy Generator seeded
from ``ctx.rng(scope)`` so a refresh on unchanged data reproduces the same numbers and the same text.
"""

from __future__ import annotations

import re

import numpy as np

from acvm.core.enums import HookType, Platform
from acvm.quant.bayes import RevenuePosterior

FACTORS: tuple[str, ...] = ("hook_type", "template", "persona", "platform", "slot", "category")
CONTEXT_FACTORS: tuple[str, ...] = ("hook_type", "template", "persona", "slot")
MIN_RULE_TRIALS = 10
MIN_RULE_PROBABILITY = 0.90
MC_SAMPLES = 4_000

HOOK_KO: dict[str, str] = {
    HookType.QUESTION.value: "질문",
    HookType.SHOCK.value: "충격",
    HookType.BEFORE_AFTER.value: "전후비교",
    HookType.PRICE_TWIST.value: "가격반전",
    HookType.EMPATHY.value: "공감",
}
PLATFORM_LABEL: dict[str, str] = {
    Platform.THREADS.value: "Threads",
    Platform.INSTAGRAM.value: "Instagram",
    Platform.FACEBOOK.value: "Facebook",
    Platform.YOUTUBE.value: "YouTube",
    Platform.TIKTOK.value: "TikTok",
    Platform.KAKAO.value: "카카오",
}
SLOT_KO: dict[str, str] = {"morning": "오전", "lunch": "점심", "afternoon": "오후", "evening": "저녁", "night": "심야"}

_NUMBER = re.compile(r"[+\-−]?\d+(?:[.,]\d+)?%?")


def sample_rpi(post: RevenuePosterior, rng: np.random.Generator, n: int) -> np.ndarray:
    """n posterior draws of RPI = CTR · λ / θ (vectorised twin of RevenuePosterior.sample_rpi)."""
    ctr = rng.beta(post.ctr.alpha, post.ctr.beta, n)
    lam = rng.gamma(post.orders_per_click.shape, 1.0 / post.orders_per_click.rate, n)
    theta = rng.gamma(post.commission_rate_param.shape, 1.0 / post.commission_rate_param.rate, n)
    return ctr * lam / np.maximum(theta, 1e-12)


def lift_probability(factor: RevenuePosterior, baseline: RevenuePosterior, seed: int,
                     samples: int = MC_SAMPLES) -> tuple[float, float]:
    """(lift of posterior means, Monte-Carlo P(RPI_factor > RPI_baseline))."""
    rng = np.random.default_rng(seed)
    f = sample_rpi(factor, rng, samples)
    b = sample_rpi(baseline, rng, samples)
    base = baseline.mean_rpi
    lift = factor.mean_rpi / base - 1.0 if base > 0 else 0.0
    return lift, float(np.mean(f > b))


def category_label(category_id: str, names: dict[str, str]) -> str:
    name = names.get(category_id, "").strip()
    return f"{name}({category_id})" if name else category_id


def factor_phrase(factor: str, value: str, names: dict[str, str]) -> str:
    if factor == "hook_type":
        return f"'{HOOK_KO.get(value, value)}({value})' 훅은"
    if factor == "template":
        return f"'{value}' 템플릿은"
    if factor == "persona":
        return f"'{value}' 페르소나는"
    if factor == "platform":
        return f"{PLATFORM_LABEL.get(value, value)} 채널은"
    if factor == "slot":
        return f"{SLOT_KO.get(value, value)}({value}) 시간대 게시는"
    if factor == "category":
        return f"{category_label(value, names)} 카테고리는"
    raise ValueError(f"unknown factor {factor!r}")


def rule_text(factor: str, value: str, category: str | None, platform: str | None, lift: float, prob: float,
              trials: int, names: dict[str, str]) -> str:
    """Korean rule with its numbers, e.g.
    "주방(1013) · Threads에서 '가격반전(price_twist)' 훅은 평균 대비 수익/노출 +38% (확률 0.93, 표본 42건)"."""
    if category and platform:
        prefix = f"{category_label(category, names)} · {PLATFORM_LABEL.get(platform, platform)}에서 "
    else:
        prefix = "전체 채널 기준 "
    return (f"{prefix}{factor_phrase(factor, value, names)} 평균 대비 수익/노출 {lift * 100:+.0f}% "
            f"(확률 {prob:.2f}, 표본 {trials}건)")


def numbers_in(text: str) -> list[str]:
    return [n.replace("−", "-") for n in _NUMBER.findall(text)]


def keeps_numbers(original: str, rephrased: str) -> bool:
    """An LLM rephrasing is accepted only if every number of the original survives verbatim."""
    have = numbers_in(rephrased)
    return all(n in have for n in numbers_in(original))
