"""Live compliance lint + automatic grounding for operator-edited copy (Studio editor).

The Studio lets a person rewrite any line. Instead of silently failing at the gate later, every
sentence is checked as it is typed:

* lexicon hits (C-02) → the exact term, its category and a safe replacement when one exists;
* factual sentences (C-01) → *auto-grounded*: we search the product facts for evidence keys whose values
  contain every number in the sentence and that satisfy the cue words (리뷰→review_count, 평점→rating,
  원→price, 로켓→is_rocket, 무료배송→is_free_shipping). If such a set exists the sentence becomes a
  registered, grounded claim automatically; otherwise it is flagged with the reason;
* price sentences (C-03) → flagged when no fresh price fact exists.

The gate applies the same `acvm.policy` definitions, so "clean in the editor" ⇔ "passes C-01/C-02/C-03".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any, Literal

from acvm.domain.models import Claim
from acvm.policy import claims as pc
from acvm.policy import lexicon as lx
from acvm.policy.facts import ProductFacts

IssueKind = Literal["superlative", "efficacy", "fake_experience", "click_bait", "guarantee", "spec_required",
                    "ungrounded", "stale_price", "discount"]
_CUE_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(리뷰|후기)"), "review_count"),
    (re.compile(r"(평점|별점|★)"), "rating"),
    (re.compile(r"\d[\d,]*\s*(만\s*)?(\d[\d,]*\s*)?원"), "price"),
    (re.compile(r"로켓"), "is_rocket"),
    (re.compile(r"무료\s*배송"), "is_free_shipping"),
)
_DISCOUNT = re.compile(r"(할인|세일|특가|쿠폰|\d+\s*%)")


@dataclass(frozen=True)
class LintIssue:
    sentence: str
    kind: IssueKind
    message: str
    term: str = ""
    suggestion: str = ""
    severity: Literal["error", "warning"] = "error"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LintResult:
    issues: list[LintIssue] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)  # auto-grounded claims for factual sentences

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": [i.as_dict() for i in self.issues],
                "claims": [c.model_dump(mode="json") for c in self.claims]}


def _kind_for(evidence: list[str]) -> str:
    if "price" in evidence:
        return "price"
    if "review_count" in evidence or "rating" in evidence:
        return "review"
    if "is_rocket" in evidence or "is_free_shipping" in evidence:
        return "shipping"
    if any(k.startswith("spec.") for k in evidence):
        return "spec"
    return "fact"


def auto_ground(sentence: str, facts: ProductFacts, *, claim_id: str = "a1") -> tuple[Claim | None, str]:
    """Find evidence keys that fully support ``sentence``; returns (claim, "") or (None, reason)."""
    values = facts.values
    required = {key for pat, key in _CUE_KEYS if pat.search(sentence)}
    missing_required = sorted(k for k in required if k not in values)
    if missing_required:
        return None, f"근거 팩트 없음: {', '.join(missing_required)}"
    nums = [n for n in pc.numbers_in(sentence)]
    candidates = [k for k, v in values.items()
                  if k not in required and any(_matches(n, v) for n in nums)]
    # strongest evidence first: operator-verified specs, then structured API fields, then the listing title
    candidates.sort(key=lambda k: (0 if facts.sources.get(k) == "verified" else 2 if k in ("name", "short_name") else 1, k))
    # smallest evidence set first: required keys + 0..3 numeric candidates
    for r in range(0, min(3, len(candidates)) + 1):
        for extra in combinations(candidates, r):
            evidence = sorted(required | set(extra))
            if pc.check_claim(sentence, evidence, values).ok:
                return Claim(id=claim_id, text=sentence, kind=_kind_for(evidence), evidence=evidence,
                             grounded=True), ""
    reasons = pc.check_claim(sentence, sorted(required | set(candidates)), values).reasons
    return None, "; ".join(reasons) or "상품 팩트로 확인되지 않는 사실 주장"


def _matches(n: float, value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return abs(float(value) - n) < 1e-9 or (abs(float(value) - n) <= 0.05 and bool(n % 1))
    return any(abs(x - n) < 1e-9 for x in pc.numbers_in(str(value)))


def lint_text(text: str, facts: ProductFacts, *, start_id: int = 1) -> LintResult:
    """Lint every sentence of ``text`` (disclosure lines are skipped — the gate manages them)."""
    res = LintResult()
    fake_ok = bool(facts.get("spec.operator_used"))
    names = [str(facts.get("short_name", "")), str(facts.get("name", ""))]
    n = start_id
    for sent in pc.split_sentences(text):
        if lx.is_valid_disclosure(sent):
            continue
        for h in lx.scan(sent):
            if h.category == "fake_experience" and fake_ok:
                continue
            cat: IssueKind = h.category  # type: ignore[assignment]
            msg = {"superlative": "최상급·절대 표현 (표시광고법)", "efficacy": "효능·의학적 표현 금지",
                   "fake_experience": "체험 위장 표현 — 실사용 확인 시에만 허용", "click_bait": "클릭 유도 금지 (무효 클릭)",
                   "guarantee": "보장·긴급성 표현 금지"}.get(h.category, "금칙 표현")
            res.issues.append(LintIssue(sent, cat, msg, term=h.term, suggestion=h.replacement or "문장 삭제",
                                        severity="warning" if h.replacement else "error"))
        for term, key in lx.spec_required_hits(sent, set(facts.values)):
            res.issues.append(LintIssue(sent, "spec_required", f"'{term}' 표현은 검증 스펙({key})이 있어야 합니다", term=term))
        probe = sent
        for m in sorted((x for x in names if x), key=len, reverse=True):
            probe = probe.replace(m, "")
        if _DISCOUNT.search(sent):
            res.issues.append(LintIssue(sent, "discount", "할인·특가 표현은 실시간 가격 검증이 불가해 게시 직전 삭제됩니다",
                                        severity="error"))
            continue
        if not pc.is_factual(probe):
            continue
        claim, reason = auto_ground(sent, facts, claim_id=f"a{n}")
        if claim is not None:
            res.claims.append(claim)
            n += 1
        elif re.search(r"\d[\d,]*\s*원", sent) and "price" not in facts.values:
            res.issues.append(LintIssue(sent, "stale_price", "가격이 6시간 이상 지나 검증되지 않았습니다 — 게시 직전 삭제됩니다"))
        else:
            res.issues.append(LintIssue(sent, "ungrounded", f"근거 없는 사실 주장: {reason}"))
    return res


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def claim_key(text: str) -> str:
    """Comparison key for claims: NFKC, no whitespace, no trailing punctuation ("평점은 4.5점이에요." ≡ "평점은 4.5점이에요")."""
    return re.sub(r"[\s.!?…·✔]+", "", unicodedata.normalize("NFKC", text))


def is_covered(text: str, existing: list[str]) -> bool:
    """True when ``text`` restates an existing claim (equal, or a fragment of it such as an on-screen label)."""
    k = claim_key(text)
    return any(k and (k == e or k in e) for e in (claim_key(x) for x in existing))
