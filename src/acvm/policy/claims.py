"""Claim ↔ evidence verification (§4 M4 Grounding, §8.1 C-01).

A *factual* sentence (contains a number or a fact cue such as 리뷰/평점/배송/개입/cm) must be backed by
evidence keys whose values contain every number the sentence states. Qualitative shipping claims
(로켓배송, 무료배송) must match the corresponding boolean fact. This is deliberately conservative: the
cost of deleting a true-but-unproven sentence is one weaker line; the cost of publishing an unproven
one is a 표시광고법 violation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_FACT_CUES = re.compile(
    r"(리뷰|후기\s*\d|평점|별점|★|로켓|무료\s*배송|배송|개입|세트|\d+\s*(cm|mm|m|kg|g|ml|L|리터|인치|W|mAh)\b|원\b|%|"
    r"재질|소재|용량|크기|사이즈|무게|구성)", re.IGNORECASE)
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(만|천)?")
_SENT_SPLIT = re.compile(r"(?<=[.!?。…])\s+|\n+|\s*[·•✔]\s*")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(unicodedata.normalize("NFKC", text)) if s and s.strip()]


def numbers_in(text: str) -> list[float]:
    """Numbers with Korean multipliers: '1만 2천' → 10000, 2000; '12,900원' → 12900; '4.7' → 4.7.

    Adjacent 만/천 groups are summed so '1만2900원' → 12900.
    """
    s = unicodedata.normalize("NFKC", text)
    out: list[float] = []
    pending: float | None = None
    last_end = -1
    for m in _NUM.finditer(s):
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        if unit == "만":
            val *= 10_000
        elif unit == "천":
            val *= 1_000
        if pending is not None and m.start() - last_end <= 1 and pending >= 10_000 and val < 10_000:
            pending += val
        else:
            if pending is not None:
                out.append(pending)
            pending = val
        last_end = m.end()
    if pending is not None:
        out.append(pending)
    return out


def is_factual(sentence: str) -> bool:
    s = unicodedata.normalize("NFKC", sentence)
    return bool(re.search(r"\d", s)) or bool(_FACT_CUES.search(s))


def _fact_numbers(value: Any) -> list[float]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    return numbers_in(str(value))


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    ok: bool
    reasons: tuple[str, ...]


def check_claim(text: str, evidence: list[str], facts: Mapping[str, Any]) -> ClaimCheck:
    reasons: list[str] = []
    missing = [k for k in evidence if k not in facts]
    if missing:
        reasons.append(f"evidence keys not in facts: {missing}")
    available = [n for k in evidence if k in facts for n in _fact_numbers(facts[k])]
    for n in numbers_in(text):
        if n in (1.0, 2.0, 3.0) and not available:
            continue  # enumerations ("장점 3개", "1단계") are structure, not claims
        if not any(_close(n, a) for a in available):
            reasons.append(f"number {n:g} not supported by evidence")
    s = unicodedata.normalize("NFKC", text)
    if "로켓" in s and not facts.get("is_rocket"):
        reasons.append("rocket-shipping claim without is_rocket fact")
    if re.search(r"무료\s*배송", s) and not (facts.get("is_free_shipping") or facts.get("is_rocket")):
        reasons.append("free-shipping claim without fact")
    if re.search(r"(리뷰|후기)", s) and re.search(r"\d", s) and "review_count" not in evidence:
        reasons.append("review-count claim must cite review_count")
    if re.search(r"(평점|별점|★)", s) and "rating" not in evidence:
        reasons.append("rating claim must cite rating")
    if re.search(r"\d[\d,]*\s*원", s) and "price" not in evidence:
        reasons.append("price claim must cite a fresh price")
    return ClaimCheck(not reasons, tuple(reasons))


def _close(a: float, b: float) -> bool:
    if a == b:
        return True
    if b != 0 and abs(a - b) / abs(b) < 1e-9:
        return True
    return abs(a - b) <= 0.05 and bool(a % 1 or b % 1)  # ratings 4.7 vs 4.68


def uncovered_factual_sentences(text: str, covered_texts: list[str], mask: Sequence[str] = ()) -> list[str]:
    """Factual sentences in ``text`` that no registered claim covers (substring match after normalising).

    ``mask`` strings (the product name) are removed before the factuality test — naming the product is
    not a claim even when its name contains digits ("3M …", "2단 …").
    """
    norm_cov = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", c)) for c in covered_texts]
    out = []
    for sent in split_sentences(text):
        probe = sent
        for m in sorted((x for x in mask if x), key=len, reverse=True):
            probe = probe.replace(m, "")
        if not is_factual(probe):
            continue
        ns = re.sub(r"\s+", "", sent)
        if not any(ns in c or c in ns for c in norm_cov if c):
            out.append(sent)
    return out
