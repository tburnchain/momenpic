"""Product facts — the only evidence a script may cite (§4 M4 "상품 팩트(API 필드 + 운영자 검증 항목만)").

Facts are a flat key → value map. API-derived keys are always present; operator-verified specs are
namespaced ``spec.<name>``. Price is a fact only while fresh (§8.1 C-03); a stale price is withheld
so the writer cannot quote it in the first place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from acvm.domain.models import Product

_BRACKETS = re.compile(r"[\[\(【].*?[\]\)】]")
_UNIT_TOKEN = re.compile(r"^(\d+\+\d+|\d+(\.\d+)?\s*(개입|개|매|팩|세트|p|ea|cm|mm|ml|g|kg|종|구|단|인용|롤|켤레)?)$",
                         re.IGNORECASE)
_FILLER = {"대용량", "세트", "신상", "정품", "국산", "특가", "증정", "무료배송", "인기", "베스트", "1+1", "본품", "리필"}


@dataclass(frozen=True)
class ProductFacts:
    product_id: int
    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # key → "api" | "verified"

    def has(self, key: str) -> bool:
        return key in self.values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def numeric_values(self, keys: list[str]) -> list[float]:
        out: list[float] = []
        for k in keys:
            out.extend(_numbers_in(self.values.get(k)))
        return out

    def as_prompt_lines(self) -> str:
        return "\n".join(f"- {k} = {v}  (source: {self.sources.get(k, 'api')})" for k, v in sorted(self.values.items()))


def _numbers_in(value: Any) -> list[float]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", str(value))]


def short_name(name: str, max_len: int = 18) -> str:
    """Display name for narration: strip bracketed promo text and trailing pack counts."""
    s = _BRACKETS.sub("", name).strip()
    words = [w for w in s.replace(",", " ").split() if not _UNIT_TOKEN.match(w) and w not in _FILLER]
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > max_len:
            break
        out = f"{out} {w}".strip()
    return out or s[:max_len]


def build_facts(product: Product, now: datetime, price_fresh_minutes: int) -> ProductFacts:
    values: dict[str, Any] = {
        "name": product.name,
        "short_name": short_name(product.name),
        "category": product.category_name or product.category_id,
        "is_rocket": product.is_rocket,
        "is_free_shipping": product.is_free_shipping,
    }
    sources = dict.fromkeys(values, "api")
    fresh = (product.price_checked_at is not None
             and now - product.price_checked_at <= timedelta(minutes=price_fresh_minutes))
    if fresh and product.price > 0:
        values["price"] = product.price
        values["price_checked_at"] = product.price_checked_at.isoformat() if product.price_checked_at else ""
        sources["price"] = sources["price_checked_at"] = "api"
    if product.review_count is not None:
        values["review_count"] = product.review_count
        sources["review_count"] = "api"
    if product.rating is not None:
        values["rating"] = round(product.rating, 1)
        sources["rating"] = "api"
    for k, v in (product.verified_facts or {}).items():
        if v in (None, ""):
            continue
        values[f"spec.{k}"] = v
        sources[f"spec.{k}"] = "verified"
    return ProductFacts(product_id=product.id, values=values, sources=sources)
