"""Beat → visual selection over the rights ledger (§4 M5 시각 소스 우선순위, §8.1 R-01).

M5 (Asset Engine) owns ingestion and registration policy; M6 (Render Engine) must pick the very same visuals
without importing M5. Both therefore call :func:`resolve_visuals`, which

1. reads candidate rows from the ledger (``AssetRepo.for_product`` / ``by_tag``),
2. drops anything unusable (no licence, licence expired before *today*, wrong kind),
3. plans one visual per beat by :attr:`AssetSource.priority` with per-visual eligibility rules, and
4. materialises deterministic generated cards for beats that need one and registers them in the ledger.

Eligibility rules (visual directive vocabulary ``hook_text | problem_scene | product_closeup | product_in_use |
benefit_card | cta_card``; an empty directive falls back to the beat role):

* ``product_*`` — own footage of the product → Coupang product image → product placeholder card. Stock and AI
  imagery are never used to show the product (§4 M5: 실물과 다른 묘사는 기만광고 위험).
* ``problem_scene`` / other scene directives — own footage tagged with the directive or the category →
  licensed stock tagged with the category → AI background tagged with the category → scene card.
* ``hook_text`` / ``benefit_card`` / ``cta_card`` — always a generated text card.

Every asset returned is a ledger row (R-01). Ties inside a priority tier are broken deterministically from
``seed`` (content id) and adjacent beats avoid repeating the same asset when an alternative exists.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

from acvm.core.enums import AssetSource
from acvm.core.errors import PolicyViolation, ValidationFailed
from acvm.domain.models import Asset, Product, ScriptBeat
from acvm.media.cards import (
    DEFAULT_BENEFIT_TITLE,
    DEFAULT_CTA,
    CardSpec,
    benefit_card,
    cta_card,
    hook_card,
    product_card,
    render_card,
    scene_card,
)
from acvm.media.fonts import FontInfo
from acvm.media.safe_area import card_text_box, union

if TYPE_CHECKING:
    from acvm.db.repos import Repos
    from acvm.ports import ObjectStore

VisualClass = Literal["product", "scene", "text"]

PRODUCT_VISUALS = frozenset({"product_closeup", "product_in_use"})
TEXT_VISUALS = frozenset({"hook_text", "benefit_card", "cta_card"})
SCENE_VISUALS = frozenset({"problem_scene"})
ROLE_DEFAULT_VISUAL = {
    "hook": "hook_text", "problem": "problem_scene", "amplify": "problem_scene", "reveal": "product_closeup",
    "benefits": "benefit_card", "cta": "cta_card",
}
GENERATED_LICENSE = "acvm-generated"
VISUAL_KINDS = frozenset({"image", "video"})
_BULLET_SPLIT = re.compile(r"\s*(?:·|\||/|\n|•|ㆍ)\s*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def normalize_visual(beat: ScriptBeat) -> str:
    v = (beat.visual or "").strip()
    return v or ROLE_DEFAULT_VISUAL.get(beat.role, "problem_scene")


def visual_class(visual: str) -> VisualClass:
    if visual in PRODUCT_VISUALS:
        return "product"
    if visual in TEXT_VISUALS:
        return "text"
    return "scene"


def usable(asset: Asset, today: date) -> bool:
    """Licensed, unexpired and a renderable visual kind."""
    if asset.kind not in VISUAL_KINDS or not asset.license.strip():
        return False
    return asset.expires_at is None or asset.expires_at >= today


def category_tags(product: Product) -> list[str]:
    tags = [product.category_id, f"category:{product.category_id}"]
    if product.category_name:
        tags += [product.category_name, f"category:{product.category_name}"]
    return [t for t in dict.fromkeys(tags) if t]


def is_product_asset(asset: Asset, product_id: int) -> bool:
    return asset.product_id == product_id or f"product:{product_id}" in asset.tags


@dataclass(frozen=True, slots=True)
class VisualChoice:
    """Plan for one beat: either an existing ledger asset or a card to generate."""

    beat_index: int
    visual: str
    asset: Asset | None = None
    card: CardSpec | None = None


def _tier_pick(pool: Sequence[Asset], *, seed: str, beat_index: int, avoid: set[str],
               prefer_video: bool = False) -> Asset | None:
    if not pool:
        return None
    ranked = sorted(pool, key=lambda a: (0 if (prefer_video and a.kind == "video") else 1, a.sha256))
    if prefer_video and ranked[0].kind == "video":
        ranked = [a for a in ranked if a.kind == "video"]
    fresh = [a for a in ranked if str(a.id) not in avoid] or ranked
    h = int.from_bytes(hashlib.blake2b(f"{seed}|{beat_index}".encode(), digest_size=4).digest(), "big")
    return fresh[h % len(fresh)]


def _benefit_parts(beat: ScriptBeat) -> tuple[str, list[str]]:
    screen = beat.on_screen_text.strip()
    parts = [p.strip(" .") for p in _BULLET_SPLIT.split(screen) if p.strip(" .")] if screen else []
    if len(parts) >= 2:
        return DEFAULT_BENEFIT_TITLE, parts[:3]
    sentences = [s.strip(" .") for s in _SENTENCE_SPLIT.split(beat.narration.strip()) if s.strip(" .")]
    return (screen or DEFAULT_BENEFIT_TITLE), sentences[:3]


def card_for(beat: ScriptBeat, visual: str, *, product: Product, hook: str = "",
             box: tuple[int, int, int, int] | None = None) -> CardSpec:
    """The deterministic card a beat gets when no ledger visual applies."""
    seed = f"product:{product.id}"
    text = beat.on_screen_text.strip()
    if visual in PRODUCT_VISUALS:
        return product_card(product.name, product.price, seed=seed, box=box)
    if visual == "hook_text":
        return hook_card(text or hook or beat.narration, seed=seed, box=box)
    if visual == "benefit_card":
        title, bullets = _benefit_parts(beat)
        return benefit_card(title, bullets, seed=seed, box=box)
    if visual == "cta_card":
        return cta_card(text or DEFAULT_CTA, subtitle=product.name, seed=seed, box=box)
    return scene_card(text or beat.narration, seed=seed, box=box)


def plan_visuals(beats: Sequence[ScriptBeat], candidates: Sequence[Asset], *, product: Product, today: date,
                 seed: str, hook: str = "", card_box: tuple[int, int, int, int] | None = None) -> list[VisualChoice]:
    """Pure planning step (no I/O): one :class:`VisualChoice` per beat."""
    live = [a for a in {a.id: a for a in candidates}.values() if usable(a, today)]
    pid = product.id
    cat = set(category_tags(product))
    own_product = [a for a in live if a.rights_source is AssetSource.OWN_FOOTAGE and is_product_asset(a, pid)]
    coupang = [a for a in live if a.rights_source is AssetSource.COUPANG_PRODUCT_IMAGE and a.product_id == pid]
    product_cards = [a for a in live if a.rights_source is AssetSource.GENERATED_CARD and is_product_asset(a, pid)
                     and "card:product" in a.tags]
    stock = [a for a in live if a.rights_source is AssetSource.LICENSED_STOCK and cat & set(a.tags)]
    ai_bg = [a for a in live if a.rights_source is AssetSource.AI_GENERATED_BACKGROUND
             and (cat & set(a.tags) or "background" in a.tags) and a.product_id is None]
    box = card_box or card_text_box(union(None))
    choices: list[VisualChoice] = []
    prev_ids: set[str] = set()
    for i, beat in enumerate(beats):
        visual = normalize_visual(beat)
        cls = visual_class(visual)
        picked: Asset | None = None
        if cls == "product":
            for tier in (own_product, coupang, product_cards):
                picked = _tier_pick(tier, seed=seed, beat_index=i, avoid=prev_ids,
                                    prefer_video=visual == "product_in_use")
                if picked is not None:
                    break
        elif cls == "scene":
            own_scene = [a for a in live if a.rights_source is AssetSource.OWN_FOOTAGE
                         and (visual in a.tags or cat & set(a.tags))]
            for tier in (own_scene, stock, ai_bg):
                picked = _tier_pick(tier, seed=seed, beat_index=i, avoid=prev_ids)
                if picked is not None:
                    break
        if picked is not None:
            choices.append(VisualChoice(i, visual, asset=picked))
            prev_ids = {str(picked.id)}
        else:
            choices.append(VisualChoice(i, visual, card=card_for(beat, visual, product=product, hook=hook, box=box)))
            prev_ids = set()
    return choices


async def ledger_candidates(repos: Repos, product: Product) -> list[Asset]:
    """All ledger rows that could serve a beat of ``product``'s content."""
    found: dict[str, Asset] = {}
    for a in await repos.assets.for_product(product.id):
        found[str(a.id)] = a
    for tag in [*category_tags(product), f"product:{product.id}", "background"]:
        for a in await repos.assets.by_tag(tag):
            found.setdefault(str(a.id), a)
    return sorted(found.values(), key=lambda a: (a.rights_source.priority, a.sha256))


async def materialize_card(card: CardSpec, *, repos: Repos, objects: ObjectStore, now: datetime,
                           product_id: int | None = None, font: FontInfo | None = None) -> Asset:
    """Render ``card`` (deterministic PNG), store it content-addressed and register it in the ledger."""
    png = render_card(card, font)
    sha = hashlib.sha256(png).hexdigest()
    uri = await objects.put_bytes(png, f"assets/{sha}.png", "image/png")
    tags = ["card", f"card:{card.kind}"]
    if product_id is not None and card.kind == "product":
        tags.append(f"product:{product_id}")
    asset = Asset(kind="image", uri=uri, sha256=sha, rights_source=AssetSource.GENERATED_CARD,
                  license=GENERATED_LICENSE, product_id=product_id if card.kind == "product" else None,
                  attribution=f"ACVM generated {card.kind} card ({card.spec_hash})", tags=tags, created_at=now)
    return await repos.assets.register(asset)


def check_override(asset: Asset, visual: str, today: date) -> None:
    """Operator-chosen visual for a beat (Studio ① 영상소스): must be a licensed, unexpired ledger image/video, and
    AI imagery may never stand in for the product on a product beat (§4 M5)."""
    if asset.kind not in VISUAL_KINDS:
        raise ValidationFailed("beat asset must be an image or a video", asset_id=str(asset.id), kind=asset.kind)
    if not asset.license.strip():
        raise PolicyViolation("R-01: beat asset has no licence in the rights ledger", asset_id=str(asset.id))
    if asset.expires_at is not None and asset.expires_at < today:
        raise PolicyViolation("R-01: beat asset licence expired", asset_id=str(asset.id),
                              expires_at=asset.expires_at.isoformat())
    if visual_class(visual) == "product" and asset.rights_source is AssetSource.AI_GENERATED_BACKGROUND:
        raise PolicyViolation("AI-generated imagery cannot stand in for the product (§4 M5)", asset_id=str(asset.id))


async def load_beat_overrides(repos: Repos, beats: Sequence[ScriptBeat], beat_assets: Mapping[int, uuid.UUID],
                              today: date) -> dict[int, Asset]:
    """Resolve ``render_options.beat_assets`` against the ledger (R-01) → beat index → Asset."""
    out: dict[int, Asset] = {}
    for idx, aid in sorted(beat_assets.items()):
        if not 0 <= idx < len(beats):
            raise ValidationFailed("beat_assets index out of range", beat=idx, beats=len(beats))
        row = await repos.assets.get(aid)
        if row is None:
            raise PolicyViolation("R-01: beat asset not in the rights ledger", asset_id=str(aid))
        check_override(row, normalize_visual(beats[idx]), today)
        out[idx] = row
    return out


async def resolve_visuals(repos: Repos, objects: ObjectStore, *, beats: Sequence[ScriptBeat], product: Product,
                          today: date, now: datetime, seed: str, hook: str = "", font: FontInfo | None = None,
                          card_box: tuple[int, int, int, int] | None = None,
                          overrides: Mapping[int, Asset] | None = None) -> dict[int, Asset]:
    """Plan + materialise: beat index → registered ledger asset (R-01 by construction). ``overrides`` (already
    validated, see :func:`load_beat_overrides`) replace the automatic choice for their beats."""
    candidates = await ledger_candidates(repos, product)
    plan = plan_visuals(beats, candidates, product=product, today=today, seed=seed, hook=hook, card_box=card_box)
    fixed = dict(overrides or {})
    out: dict[int, Asset] = {}
    made: dict[str, Asset] = {}
    for choice in plan:
        if choice.beat_index in fixed:
            out[choice.beat_index] = fixed[choice.beat_index]
            continue
        if choice.asset is not None:
            out[choice.beat_index] = choice.asset
            continue
        assert choice.card is not None
        key = choice.card.spec_hash
        if key not in made:
            made[key] = await materialize_card(choice.card, repos=repos, objects=objects, now=now,
                                               product_id=product.id, font=font)
        out[choice.beat_index] = made[key]
    return out


def ledger_violations(assets: Mapping[int, Asset] | Sequence[Asset], ledger: Mapping[Any, Asset],
                      today: date) -> list[str]:
    """R-01 check: every asset must exist in the ledger (by id) and be licensed/unexpired today."""
    items = list(assets.values()) if isinstance(assets, Mapping) else list(assets)
    problems: list[str] = []
    for a in items:
        row = ledger.get(a.id)
        if row is None:
            problems.append(f"asset {a.id} not in rights ledger")
        elif not row.license.strip():
            problems.append(f"asset {a.id} has no licence")
        elif row.expires_at is not None and row.expires_at < today:
            problems.append(f"asset {a.id} licence expired {row.expires_at.isoformat()}")
    return problems
