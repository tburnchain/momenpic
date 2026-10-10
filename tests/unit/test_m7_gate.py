"""Gate regression suite (§9.2 SLO "게이트 D·C-04·R-01 규칙 누락 0건 (회귀 테스트 100% 커버)", G2).

Every rule in rule_set v1 has at least one violating and one clean case here; a rule without a test
fails `test_every_rule_is_covered`.
"""

from __future__ import annotations

import sys
import types
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest

from acvm.context import AppContext
from acvm.core.enums import (
    AccountStatus,
    AssetSource,
    ContentKind,
    ContentStatus,
    HookType,
    LinkHealth,
    Platform,
    PublicationState,
    Verdict,
)
from acvm.core.ids import publication_key, short_code, sub_id_for, uuid7
from acvm.domain.models import (
    AffiliateLink,
    Asset,
    ChannelAccount,
    Claim,
    Content,
    ContentDNA,
    Publication,
    RenderMeta,
    Script,
    ScriptBeat,
)
from acvm.modules.m7_gate.engine import GateEngine
from acvm.modules.m7_gate.rules import hub_disclosure_ok
from acvm.modules.m7_gate.ruleset import RULES_V1, describe, fingerprint
from acvm.security.linksign import sign_link

STD = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
COVERED: set[str] = set()


def covers(*rule_ids: str):  # type: ignore[no-untyped-def]
    COVERED.update(rule_ids)
    return lambda f: f


@dataclass
class World:
    ctx: AppContext
    ws: Any
    acc: ChannelAccount
    product: Any


@pytest.fixture
async def world(ctx: AppContext, workspace, accounts, product_factory) -> World:  # type: ignore[no-untyped-def]
    ctx.settings.gate.new_template_hold_count = 0  # template maturity tested explicitly in H-01
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    return World(ctx, workspace, accounts[Platform.THREADS], p)


def text_content(w: World, *, body: str, caption: str = "", claims: list[Claim] | None = None,
                 account: ChannelAccount | None = None, template: str = "text-v1") -> Content:
    return Content(
        workspace_id=w.ws.id, product_id=w.product.id, kind=ContentKind.TEXT, persona="30대 여성 직장인",
        template_id=template, target_account_id=(account or w.acc).id, created_at=w.ctx.clock.now(),
        claims=claims or [], script=Script(hooks=["과자 봉지 눅눅해지죠?"] * 3, hook_type=HookType.QUESTION,
                                           body=body, caption=caption or f"{STD}\n과자 봉지 눅눅해지죠?",
                                           persona="30대 여성 직장인", template_id=template, disclosure=STD))


async def save(w: World, c: Content) -> Content:
    async with w.ctx.uow() as r:
        await r.contents.add(c)
    return c


def review_claim() -> Claim:
    return Claim(id="c1", text="리뷰가 3,812개 넘게 달린 상품이에요", kind="review", evidence=["review_count"], grounded=True)


def rule_ids(outcome, *, open_only: bool = False) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.rule_id for f in outcome.check.findings if not (open_only and f.fixed) and f.verdict is not Verdict.PASS}


# ───────────────────────── PASS baseline ─────────────────────────


async def test_clean_text_content_passes_and_is_approved(world: World) -> None:
    c = await save(world, text_content(world, body=f"{STD}\n\n과자 봉지 눅눅해지죠?\n✔ 리뷰가 3,812개 넘게 달린 상품이에요",
                                       claims=[review_claim()]))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.PASS, out.reasons
    async with world.ctx.uow() as r:
        stored = await r.contents.get(c.id)
        events = await r.outbox.pending()
    assert stored.status is ContentStatus.APPROVED
    assert any(e.type == "gate.decided" for e in events)
    assert out.check.rule_set_version == f"v1#{fingerprint()}"


# ───────────────────────── D-01 / D-02 ─────────────────────────


@covers("D-01")
async def test_d01_inserts_disclosure_first_and_moves_bottom_disclosure(world: World) -> None:
    c = await save(world, text_content(world, body=f"과자 봉지 눅눅해지죠?\n\n{STD}", caption="과자 봉지 눅눅해지죠?"))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.PASS
    assert out.content.script.body.split("\n")[0] == STD
    assert out.content.script.body.count("쿠팡 파트너스") == 1
    assert out.content.script.caption.startswith(STD)
    assert "D-01" in {f.rule_id for f in out.check.findings if f.fixed}


@covers("D-02")
async def test_d02_replaces_disclosure_without_consideration(world: World) -> None:
    c = await save(world, text_content(world, body="#광고 #쿠팡파트너스\n과자 봉지 눅눅해지죠?"))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.content.script.body.split("\n")[0] == STD
    assert "D-02" in {f.rule_id for f in out.check.findings if f.fixed}


# ───────────────────────── D-03 (video overlay) ─────────────────────────


def short_content(w: World, render: RenderMeta | None, *, narration: str = "과자 봉지 눅눅해지죠?") -> Content:
    beats = [ScriptBeat(start_sec=0, end_sec=3, role="hook", narration=narration, visual="hook_text"),
             ScriptBeat(start_sec=3, end_sec=29, role="cta", narration="제품 정보는 프로필 링크에서 확인하세요", visual="cta_card")]
    return Content(workspace_id=w.ws.id, product_id=w.product.id, kind=ContentKind.SHORT, persona="30대 여성 직장인",
                   template_id="problem-solution-v3", target_account_id=w.acc.id, created_at=w.ctx.clock.now(),
                   status=ContentStatus.RENDERED, render=render,
                   script=Script(hooks=[narration] * 3, hook_type=HookType.QUESTION, beats=beats,
                                 caption=f"{STD}\n{narration}", persona="30대 여성 직장인",
                                 template_id="problem-solution-v3", disclosure=STD))


async def ledger(w: World, *, expired: bool = False, voice: bool = True) -> tuple[list[uuid.UUID], str]:
    img = Asset(kind="image", uri="file://a.png", sha256=("e" if expired else "a") * 64,
                rights_source=AssetSource.GENERATED_CARD,
                license="acvm-generated", expires_at=date(2020, 1, 1) if expired else None)
    v = Asset(kind="voice", uri="tts:mock:ko-F-30s-brisk", sha256="b" * 64, rights_source=AssetSource.LICENSED_STOCK,
              license="mock tts commercial")
    async with w.ctx.uow() as r:
        img = await r.assets.register(img)  # sha-deduplicated: use the stored row's id
        if voice:
            v = await r.assets.register(v)
    return [img.id, *([v.id] if voice else [])], "tts:mock:ko-F-30s-brisk"


def overlay(**over: Any) -> dict[str, Any]:
    base = {"text": "광고 · 쿠팡 파트너스 활동으로 수수료를 받습니다", "start": 0.0, "end": 3.0, "box": [0, 0, 1080, 200],
            "locked": True}
    base.update(over)
    return base


@pytest.fixture
def fake_verify(monkeypatch):  # type: ignore[no-untyped-def]
    state = {"ok": True}

    @dataclass
    class Check:
        ok: bool
        score: float
        details: dict

    async def verify(path, ov, *, ffmpeg="ffmpeg"):  # type: ignore[no-untyped-def]
        return Check(state["ok"], 0.99 if state["ok"] else 0.2, {})

    mod = types.ModuleType("acvm.media.verify")
    mod.verify_disclosure_overlay = verify  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acvm.media.verify", mod)
    return state


async def render_meta(w: World, **over: Any) -> RenderMeta:
    ids, voice = await ledger(w)
    uri = await w.ctx.objects.put_bytes(b"video", "media/x.mp4", "video/mp4")
    base = dict(media_uri=uri, duration_sec=29, width=1080, height=1920, fps=30, disclosure_overlay=overlay(),
                asset_ids=ids, voice_source=voice)
    base.update(over)
    return RenderMeta(**base)


async def link_assets(w: World, c: Content) -> None:
    async with w.ctx.uow() as r:
        for aid in c.render.asset_ids:
            await r.contents.link_asset(c.id, aid, "beat:0")


@covers("D-03")
async def test_d03_blocks_missing_render_unlocked_or_failed_frames(world: World, fake_verify) -> None:  # type: ignore[no-untyped-def]
    eng = GateEngine(world.ctx)
    c1 = await save(world, short_content(world, None))
    assert (await eng.evaluate(c1.id)).verdict is Verdict.BLOCK

    c2 = await save(world, short_content(world, await render_meta(world, disclosure_overlay=overlay(locked=False))))
    await link_assets(world, c2)
    out2 = await eng.evaluate(c2.id)
    assert out2.verdict is Verdict.BLOCK and "D-03" in rule_ids(out2)

    fake_verify["ok"] = False
    c3 = await save(world, short_content(world, await render_meta(world)))
    await link_assets(world, c3)
    out3 = await eng.evaluate(c3.id)
    assert out3.verdict is Verdict.BLOCK and "D-03" in rule_ids(out3)

    fake_verify["ok"] = True
    c4 = await save(world, short_content(world, await render_meta(world), narration="봉지 집게가 필요해요?"))
    await link_assets(world, c4)
    out4 = await eng.evaluate(c4.id)
    assert "D-03" not in rule_ids(out4), out4.reasons
    assert out4.verdict is Verdict.PASS, out4.reasons


@covers("R-01")
async def test_r01_blocks_assets_outside_ledger_or_expired(world: World, fake_verify) -> None:  # type: ignore[no-untyped-def]
    eng = GateEngine(world.ctx)
    rm = await render_meta(world)
    c = await save(world, short_content(world, rm.model_copy(update={"asset_ids": [*rm.asset_ids, uuid7()]})))
    out = await eng.evaluate(c.id)
    assert out.verdict is Verdict.BLOCK and "R-01" in rule_ids(out)

    ids, voice = await ledger(world, expired=True)
    uri = await world.ctx.objects.put_bytes(b"v", "media/y.mp4", "video/mp4")
    c2 = await save(world, short_content(world, RenderMeta(media_uri=uri, duration_sec=29, width=1080, height=1920, fps=30,
                                                           disclosure_overlay=overlay(), asset_ids=ids, voice_source=voice)))
    await link_assets(world, c2)
    out2 = await eng.evaluate(c2.id)
    assert out2.verdict is Verdict.BLOCK and "R-01" in rule_ids(out2)


@covers("R-03")
async def test_r03_holds_ip_terms_and_unledgered_voice(world: World, fake_verify) -> None:  # type: ignore[no-untyped-def]
    eng = GateEngine(world.ctx)
    c = await save(world, short_content(world, await render_meta(world), narration="포켓몬 좋아하는 아이들 주목"))
    await link_assets(world, c)
    out = await eng.evaluate(c.id)
    assert out.verdict is Verdict.HOLD and "R-03" in rule_ids(out)

    c2 = await save(world, short_content(world, await render_meta(world, voice_source="tts:unknown:celebrity-clone"),
                                         narration="봉지 집게 하나 있으면 편해요?"))
    await link_assets(world, c2)
    out2 = await eng.evaluate(c2.id)
    assert out2.verdict is Verdict.HOLD and any("보이스" in f.message for f in out2.check.findings)
    async with world.ctx.uow() as r:
        assert len(await r.compliance.pending_reviews(world.ws.id)) == 2


# ───────────────────────── C rules ─────────────────────────


@covers("C-01")
async def test_c01_removes_ungrounded_claims_and_uncovered_facts(world: World) -> None:
    bad = Claim(id="c2", text="리뷰가 9,999개예요", kind="review", evidence=["review_count"])
    body = f"{STD}\n\n✔ 리뷰가 3,812개 넘게 달린 상품이에요\n✔ 리뷰가 9,999개예요\n✔ 판매량 50만 개 돌파"
    c = await save(world, text_content(world, body=body, claims=[review_claim(), bad]))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.PASS
    b = out.content.script.body
    assert "3,812" in b and "9,999" not in b and "50만" not in b
    assert [cl.id for cl in out.content.claims] == ["c1"]


@covers("C-02")
async def test_c02_softens_and_removes_banned_expressions(world: World) -> None:
    body = (f"{STD}\n\n최고의 클립이에요. 면역력에 좋아요. 클릭만 해주세요. 항균 효과가 있어요.\n"
            "✔ 리뷰가 3,812개 넘게 달린 상품이에요")
    c = await save(world, text_content(world, body=body, claims=[review_claim()]))
    out = await GateEngine(world.ctx).evaluate(c.id)
    b = out.content.script.body
    assert "최고" not in b and "면역력" not in b and "클릭만" not in b and "항균" not in b
    assert "만족도 높은 클립이에요" in b
    assert out.verdict is Verdict.PASS


@covers("C-03")
async def test_c03_price_must_match_fresh_price(world: World) -> None:
    price_claim = Claim(id="c3", text="이 구성이 12,900원이에요", kind="price", evidence=["price"], grounded=True)
    wrong = Claim(id="c4", text="지금 9,900원이에요", kind="price", evidence=["price"])
    body = f"{STD}\n\n이 구성이 12,900원이에요. 지금 9,900원이에요. 오늘 30% 할인 중이에요."
    c = await save(world, text_content(world, body=body, claims=[price_claim, wrong]))
    out = await GateEngine(world.ctx).evaluate(c.id)
    b = out.content.script.body
    assert "12,900원" in b and "9,900원이에요" not in b.replace("12,900원", "") and "할인" not in b

    world.ctx.clock.advance(hours=7)  # price now stale (> 360 min)
    c2 = await save(world, text_content(world, body=f"{STD}\n\n이 구성이 12,900원이에요.", claims=[price_claim]))
    out2 = await GateEngine(world.ctx).evaluate(c2.id)
    assert "12,900" not in out2.content.script.body
    assert any(f.rule_id in ("C-03", "C-01") and f.fixed for f in out2.check.findings)


@covers("C-04")
async def test_c04_blocks_regulated_products(world: World, product_factory) -> None:  # type: ignore[no-untyped-def]
    reg = product_factory(2002, category="1024", name="종합 비타민 영양제 180정")
    async with world.ctx.uow() as r:
        await r.products.upsert(reg)
    c = await save(world, text_content(world, body=f"{STD}\n비타민이에요").model_copy(update={"product_id": reg.id}))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.BLOCK and "C-04" in rule_ids(out)
    async with world.ctx.uow() as r:
        assert (await r.contents.get(c.id)).status is ContentStatus.BLOCKED


# ───────────────────────── R-02 / S-01 similarity ─────────────────────────


@covers("R-02")
async def test_r02_requests_regeneration_when_copying_reference(world: World) -> None:
    ref_text = "먹다 남은 과자 봉지 하루만 지나도 눅눅해지죠 고무줄로 묶어두면 금방 풀리고 결국 반은 버리게 돼요"
    emb = (await world.ctx.embedder.embed([ref_text]))[0]
    async with world.ctx.uow() as r:
        await r.dna.add(ContentDNA(source="own", hook_type=HookType.QUESTION, reference_text=ref_text, embedding=emb,
                                   workspace_id=world.ws.id, perf_percentile=0.99))
    c = await save(world, text_content(world, body=f"{STD}\n\n{ref_text}"))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.FIX and "regenerate" in out.actions and "R-02" in rule_ids(out)


@covers("S-01")
async def test_s01_requests_regeneration_for_account_duplicates(world: World) -> None:
    text = "서랍 열 때마다 뭐가 어디 있는지 모르겠죠 필요한 거 하나 찾으려고 서랍을 다 뒤집게 돼요 그래서 찾은 게 정리함이에요"
    await save(world, text_content(world, body=f"{STD}\n\n{text}"))
    world.ctx.clock.advance(hours=1)
    c = await save(world, text_content(world, body=f"{STD}\n\n{text} 진짜로"))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.FIX and "S-01" in rule_ids(out) and "regenerate" in out.actions


# ───────────────────────── account policy ─────────────────────────


@covers("S-02")
async def test_s02_reschedules_for_cooldown_account(world: World) -> None:
    async with world.ctx.uow() as r:
        await r.accounts.set_status(world.acc.id, AccountStatus.COOLDOWN, world.ctx.clock.now() + timedelta(hours=48))
    c = await save(world, text_content(world, body=f"{STD}\n\n과자 봉지 눅눅해지죠?"))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.FIX and out.actions == {"reschedule"}


@covers("H-01")
async def test_h01_holds_new_accounts_and_new_templates(world: World) -> None:
    young = ChannelAccount(workspace_id=world.ws.id, platform=Platform.THREADS, handle="new_kitchen", token_ref="t",
                           daily_cap=20, niche="kitchen", created_at=world.ctx.clock.now() - timedelta(days=5))
    async with world.ctx.uow() as r:
        await r.accounts.add(young)
    c = await save(world, text_content(world, body=f"{STD}\n\n과자 봉지 눅눅해지죠?", account=young))
    out = await GateEngine(world.ctx).evaluate(c.id)
    assert out.verdict is Verdict.HOLD and "H-01" in rule_ids(out)

    world.ctx.settings.gate.new_template_hold_count = 10
    c2 = await save(world, text_content(world, body=f"{STD}\n\n봉지 집게 필요하세요?", template="text-v1"))
    out2 = await GateEngine(world.ctx).evaluate(c2.id)
    assert out2.verdict is Verdict.HOLD and any("신규 템플릿" in f.message for f in out2.check.findings)


async def test_hold_review_approve_and_reject(world: World) -> None:
    world.ctx.settings.gate.new_template_hold_count = 10
    eng = GateEngine(world.ctx)
    c = await save(world, text_content(world, body=f"{STD}\n\n과자 봉지 눅눅해지죠?"))
    assert (await eng.evaluate(c.id)).verdict is Verdict.HOLD
    async with world.ctx.uow() as r:
        item = (await r.compliance.pending_reviews(world.ws.id))[0]
    approved = await eng.decide_review(item.id, approve=True, by="reviewer@tburn", note="OK")
    assert approved.status is ContentStatus.APPROVED
    again = await eng.decide_review(item.id, approve=False, by="other@tburn")
    assert again.status is ContentStatus.APPROVED  # first decision wins; replay is a no-op


# ───────────────────────── preflight ─────────────────────────


async def publication(w: World, c: Content, *, caption: str, placement: str = "body", ai_label: bool = True,
                      tamper: bool = False, health: LinkHealth = LinkHealth.OK) -> Publication:
    key = publication_key(c.id, "threads", w.acc.id)
    sub = sub_id_for(key)
    deeplink = f"https://link.coupang.com/re/AFFSDP?lptag=AF1234567&subid={sub}&pageKey={w.product.id}"
    short = "https://link.coupang.com/a/abc123"
    secret = (await w.ctx.secrets.get(w.ctx.settings.security.link_signing_key_ref)).encode()
    sig = sign_link(secret, sub, deeplink, short, w.ctx.settings.coupang.partner_id)
    link = AffiliateLink(product_id=w.product.id, sub_id=sub, deeplink=deeplink.replace("AF1234567", "AF6666666") if tamper
                         else deeplink, short_url=short, hub_code=short_code(key), signature=sig, publication_key=key,
                         health=health, created_at=w.ctx.clock.now())
    pub = Publication(content_id=c.id, channel_account_id=w.acc.id, platform=Platform.THREADS, link_id=link.id,
                      caption=caption, link_placement=placement, scheduled_at=w.ctx.clock.now(), idempotency_key=key,
                      ai_label=ai_label, state=PublicationState.QUEUED)
    async with w.ctx.uow() as r:
        await r.links.add(link)
        await r.publications.add(pub)
    return pub


async def approved_text(w: World) -> Content:
    c = text_content(w, body=f"{STD}\n\n과자 봉지 눅눅해지죠?").model_copy(update={"status": ContentStatus.APPROVED})
    return await save(w, c)


@covers("G-00")
async def test_g00_blocks_unapproved_content_at_preflight(world: World) -> None:
    c = await save(world, text_content(world, body=f"{STD}\n\n과자 봉지?"))
    pub = await publication(world, c, caption=f"{STD}\n과자 봉지?")
    out = await GateEngine(world.ctx).preflight(pub.id)
    assert out.verdict is Verdict.BLOCK and "G-00" in rule_ids(out)


@covers("L-01")
async def test_l01_link_integrity_and_health(world: World) -> None:
    eng = GateEngine(world.ctx)
    c = await approved_text(world)
    ok = await publication(world, c, caption=f"{STD}\n과자 봉지?")
    assert (await eng.preflight(ok.id)).verdict is Verdict.PASS

    c2 = await approved_text(world)
    bad = await publication(world, c2, caption=f"{STD}\n과자 봉지?", tamper=True)
    out = await eng.preflight(bad.id)
    assert out.verdict is Verdict.BLOCK and "L-01" in rule_ids(out)

    c3 = await approved_text(world)
    sold = await publication(world, c3, caption=f"{STD}\n과자 봉지?", health=LinkHealth.SOLD_OUT)
    assert (await eng.preflight(sold.id)).verdict is Verdict.HOLD


@covers("A-01")
async def test_a01_sets_ai_label_and_d01_fixes_caption_at_preflight(world: World) -> None:
    c = await approved_text(world)
    pub = await publication(world, c, caption="과자 봉지 눅눅해지죠? 링크는 본문에", ai_label=False)
    out = await GateEngine(world.ctx).preflight(pub.id)
    assert out.verdict is Verdict.PASS
    assert out.publication.ai_label is True and out.publication.caption.startswith(STD)
    async with world.ctx.uow() as r:
        stored = await r.publications.get(pub.id)
    assert stored.caption.startswith(STD) and stored.ai_label


@covers("D-04")
async def test_d04_hub_page_disclosure(world: World) -> None:
    eng = GateEngine(world.ctx)
    c = await approved_text(world)
    pub = await publication(world, c, caption=f"{STD}\n프로필 링크에서 확인", placement="hub")
    assert (await eng.preflight(pub.id)).verdict is Verdict.BLOCK  # hub page not built

    good = f"<html><body><div id='acvm-disclosure'>{STD}</div><a href='/r/x'>상품</a></body></html>"
    uri = await world.ctx.objects.put_bytes(good.encode(), "hub/kitchen.html", "text/html")
    async with world.ctx.uow() as r:
        await r.kv.put(f"hub:{world.acc.id}", {"uri": uri}, timedelta(days=1), world.ctx.clock.now())
    assert (await eng.preflight(pub.id)).verdict is Verdict.PASS

    bad = f"<html><body><h1>오늘의 추천</h1><footer>{STD}</footer></body></html>"
    await world.ctx.objects.put_bytes(bad.encode(), "hub/kitchen.html", "text/html")
    out = await eng.preflight(pub.id)
    assert out.verdict is Verdict.BLOCK and "D-04" in rule_ids(out)


def test_hub_parser_ignores_scripts() -> None:
    ok, first = hub_disclosure_ok(f"<body><script>var x='광고';</script><p>{STD}</p></body>")
    assert ok and first == STD


def test_rule_registry_matches_design() -> None:
    ids = [r["id"] for r in describe()]
    for rid in ("D-01", "D-02", "D-03", "D-04", "C-01", "C-02", "C-03", "C-04", "R-01", "R-02", "R-03", "S-01",
                "S-02", "A-01", "H-01"):
        assert rid in ids
    blocks = {r.id for r in RULES_V1 if r.on_fail is Verdict.BLOCK}
    assert {"D-03", "D-04", "C-04", "R-01"} <= blocks


def test_every_rule_is_covered() -> None:
    assert {r.id for r in RULES_V1} <= COVERED, f"untested rules: {sorted({r.id for r in RULES_V1} - COVERED)}"
