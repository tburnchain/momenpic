from __future__ import annotations

from datetime import timedelta

import pytest

from acvm.context import AppContext
from acvm.core.enums import ContentKind, HookType, Platform
from acvm.core.ids import uuid7
from acvm.domain.models import Claim
from acvm.modules.m4_script.facts import build_facts, short_name
from acvm.modules.m4_script.grounding import DraftBeatIn, DraftIn, finalize
from acvm.modules.m4_script.service import ScriptEngine, ScriptRequest, fit_short_length
from acvm.modules.m4_script.templates import (
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    TEMPLATES,
    estimate_seconds,
    resolve_persona,
)
from acvm.policy import claims as pc
from acvm.policy import lexicon as lx


def test_short_name_strips_promo_and_counts() -> None:
    assert short_name("[1+1] 과자 밀봉 클립 20개입") == "과자 밀봉 클립"
    assert short_name("과자 밀봉 클립 20개입 대용량 세트") == "과자 밀봉 클립"
    assert short_name("3M 차량용 틈새 수납함 2개") == "3M 차량용 틈새 수납함"


def test_numbers_with_korean_units() -> None:
    assert pc.numbers_in("1만2900원") == [12900.0]
    assert pc.numbers_in("리뷰 3,812개, 평점 4.7") == [3812.0, 4.7]


def test_claim_check_requires_matching_numbers(product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    facts = build_facts(p, p.last_seen_at, 360).values
    assert pc.check_claim("리뷰가 3,812개 넘게 달린 상품이에요", ["review_count"], facts).ok
    assert not pc.check_claim("리뷰가 9,999개 넘게 달린 상품이에요", ["review_count"], facts).ok
    assert not pc.check_claim("리뷰가 3,812개예요", [], facts).ok
    assert not pc.check_claim("12,900원이에요", ["review_count"], facts).ok
    assert pc.check_claim("이 구성이 12,900원이에요", ["price"], facts).ok


def test_stale_price_is_not_a_fact(product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    facts = build_facts(p, p.last_seen_at + timedelta(hours=7), 360)
    assert not facts.has("price")


def test_finalize_removes_ungrounded_and_superlatives(product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    facts = build_facts(p, p.last_seen_at, 360)
    draft = DraftIn(
        hooks=["무조건 사야 하는 최고의 클립", "과자 봉지 눅눅해지죠?", "이거 보세요"], chosen_hook=0,
        hook_type=HookType.QUESTION,
        beats=[
            DraftBeatIn("hook", "무조건 사야 하는 최고의 클립", "", "hook_text", []),
            DraftBeatIn("amplify", "제가 써봤는데 면역력에도 좋아요. 봉지가 금방 눅눅해져요.", "", "problem_scene", []),
            DraftBeatIn("reveal", "그래서 찾은 게 과자 밀봉 클립이에요", "", "product_closeup", []),
            DraftBeatIn("benefits", "리뷰가 3,812개 넘게 달린 상품이에요. 판매량 1위 50만 개 돌파.", "", "benefit_card", ["c1"]),
            DraftBeatIn("cta", "프로필 링크에서 확인하세요", "", "cta_card", []),
        ],
        claims=[Claim(id="c1", text="리뷰가 3,812개 넘게 달린 상품이에요", kind="review", evidence=["review_count"]),
                Claim(id="c2", text="판매량 1위 50만 개 돌파.", kind="fact", evidence=[])],
        body="", caption="최고의 선택", hashtags=["#광고", "#최고템"], cta="프로필 링크에서 확인하세요", title="t")
    script, claims, report = finalize(draft, facts=facts, template=TEMPLATES["problem-solution-v3"],
                                      persona="30대 여성 직장인", disclosure="D", link_placement_cta="프로필 링크에서 확인하세요")
    all_text = " ".join([*script.hooks, script.narration, script.caption, " ".join(script.hashtags)])
    assert not [h for h in lx.scan(all_text) if h.category != "superlative" or h.replacement is None]
    assert "최고" not in all_text and "무조건" not in all_text
    assert "면역력" not in all_text and "제가 써봤는데" not in all_text
    assert "50만" not in all_text and "1위" not in all_text
    assert "3,812" in script.narration
    assert [c.id for c in claims] == ["c1"]
    assert report.ungrounded_claims and report.removed_sentences
    assert [b.role for b in script.beats] == ["hook", "amplify", "reveal", "benefits", "cta"]
    assert script.beats[-1].end_sec == 22  # 20–25 s short

    # an over-long benefits beat is trimmed sentence by sentence (last first) until the fast-rate estimate fits
    benefits = script.beats[3].narration
    filler = " ".join(f"{n}번째로 봉지 입구를 단단하게 잡아줘서 과자가 눅눅해지지 않아요." for n in range(2, 12))
    long = script.model_copy(update={"beats": [b.model_copy(update={"narration": f"{benefits} {filler}"})
                                               if b.role == "benefits" else b for b in script.beats]})
    assert estimate_seconds([b.narration for b in long.beats]) > SHORT_MAX_SECONDS
    fitted, kept = fit_short_length(long, claims, TEMPLATES["problem-solution-v3"])
    assert estimate_seconds([b.narration for b in fitted.beats]) <= SHORT_MAX_SECONDS
    assert fitted.beats[3].narration.startswith(benefits.rstrip("."))  # the grounded claim survives
    assert [b.narration for i, b in enumerate(fitted.beats) if i != 3] == [
        b.narration for i, b in enumerate(script.beats) if i != 3]  # hook/problem/reveal/CTA untouched
    assert [c.id for c in kept] == ["c1"]
    assert all(SHORT_MIN_SECONDS <= t.duration <= SHORT_MAX_SECONDS for t in TEMPLATES.values()
               if t.kind is ContentKind.SHORT)


async def test_generate_short_is_grounded_idempotent_and_emits(ctx: AppContext, workspace, accounts,  # type: ignore[no-untyped-def]
                                                              product_factory) -> None:
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    engine = ScriptEngine(ctx)
    req = ScriptRequest(run_id=uuid7(), workspace_id=workspace.id, product_id=p.id, kind=ContentKind.SHORT,
                        persona="30대 여자", platform=Platform.INSTAGRAM, link_placement="hub",
                        target_account_id=accounts[Platform.INSTAGRAM].id)
    c1 = await engine.generate(req)
    c2 = await engine.generate(req)
    assert c1.id == c2.id
    s = c1.script
    assert s.disclosure.startswith("이 포스팅은 쿠팡 파트너스")
    assert len(s.hooks) == 3 and s.beats[0].narration == s.hook
    assert all(c.grounded for c in c1.claims)
    text = " ".join([s.narration, s.caption, s.body])
    assert not [h for h in lx.scan(text)]
    assert pc.uncovered_factual_sentences(s.narration, [c.text for c in c1.claims],
                                          mask=[p.name, "과자 밀봉 클립"]) == []
    async with ctx.uow() as r:
        stored = await r.contents.get(c1.id)
        pending = await r.outbox.pending()
    assert stored is not None and stored.persona == "30대 여성 직장인"
    assert estimate_seconds([b.narration for b in s.beats]) <= SHORT_MAX_SECONDS  # 20–25 s at the fast rate
    assert [e.type for e in pending].count("script.ready") == 1


async def test_text_post_registers_body_claims(ctx: AppContext, workspace, accounts, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    c = await ScriptEngine(ctx).generate(ScriptRequest(
        run_id=uuid7(), workspace_id=workspace.id, product_id=p.id, kind=ContentKind.TEXT, persona="20대 자취생",
        platform=Platform.THREADS, link_placement="body", target_account_id=accounts[Platform.THREADS].id))
    assert c.template_id == "text-v1" and c.script.body
    assert "✔" in c.script.body
    names = [p.name, build_facts(p, p.last_seen_at, 360).get("short_name")]
    assert pc.uncovered_factual_sentences(c.script.body, [x.text for x in c.claims], mask=names) == []


async def test_variants_stay_below_account_similarity(ctx: AppContext, workspace, accounts, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    engine = ScriptEngine(ctx)
    run = uuid7()
    sims = []
    for v in range(4):
        c = await engine.generate(ScriptRequest(
            run_id=run, workspace_id=workspace.id, product_id=p.id, kind=ContentKind.SHORT, persona="30대 여성 직장인",
            platform=Platform.INSTAGRAM, link_placement="hub", target_account_id=accounts[Platform.INSTAGRAM].id,
            variant_index=v))
        sims.append(c.similarity_max)
    assert sims[0] == 0.0
    assert sum(s <= 0.6 for s in sims) >= 3


@pytest.mark.parametrize(("text", "name"), [("30대 여자", "30대 여성 직장인"), ("40대 주부 느낌", "40대 주부"),
                                            ("20대 자취생", "20대 자취생"), ("30대 남자", "30대 남성 직장인")])
def test_persona_resolution(text: str, name: str) -> None:
    assert resolve_persona(text).name == name


@pytest.mark.parametrize(("word", "expected"), [("ABS 플라스틱", "이에요"), ("과자 밀봉 클립", "이에요"), ("수납함", "이에요"),
                                                ("정리 트레이", "예요"), ("11cm", "예요"), ("3M", "이에요"), ("500g", "이에요")])
def test_copula_particle(word: str, expected: str) -> None:
    from acvm.modules.m4_script.korean import ida

    assert ida(word) == expected


def test_euro_particle() -> None:
    from acvm.modules.m4_script.korean import euro

    assert euro("클립") == "으로" and euro("트레이") == "로" and euro("거치대") == "로"
    assert euro("필름") == "으로" and euro("물") == "로" and euro("케이블") == "로"


@pytest.mark.parametrize("template_id", ["problem-solution-v3", "before-after-v1", "price-twist-v1", "listicle-v1"])
def test_composer_fills_short_to_the_20_25s_window_with_grounded_or_neutral_lines(product_factory,  # type: ignore[no-untyped-def]
                                                                                  template_id: str) -> None:
    import random

    from acvm.modules.m4_script.composer import compose

    p = product_factory()
    facts = build_facts(p, p.last_seen_at, 360)
    t = TEMPLATES[template_id]
    for seed in range(5):
        cs = compose(facts=facts, category_id="1013", template=t, persona=resolve_persona("30대 여성"),
                     hook_type=t.preferred_hooks[0], link_placement="hub", rng=random.Random(seed))
        narrations = [b.narration for b in cs.beats]
        assert SHORT_MIN_SECONDS <= estimate_seconds(narrations) <= SHORT_MAX_SECONDS - 0.5
        text = " ".join(narrations)
        assert pc.uncovered_factual_sentences(text, [c.text for c in cs.claims], mask=[p.name, "과자 밀봉 클립"]) == []
        assert not lx.scan(text)  # fillers never add superlatives / testimony
        sentences = pc.split_sentences(text)
        assert len(sentences) == len(set(sentences))  # no repeated filler line
