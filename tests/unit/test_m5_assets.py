"""M5 Asset Engine: rights ledger policy, product imagery, priority selection and voices (§4 M5, R-01, R-03)."""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from acvm.context import AppContext
from acvm.core.enums import AssetSource, ContentKind, HookType
from acvm.core.errors import PolicyViolation, ValidationFailed
from acvm.core.events import Event, ScriptReady
from acvm.domain.models import Asset, Content, Product, Script, ScriptBeat, Workspace
from acvm.media.voices import voice_id_for_persona
from acvm.modules.m5_asset import AssetService, host_allowed

CDN_URL = "https://thumbnail6.coupangcdn.com/thumbnails/remote/492x492ex/image/1001.jpg"


def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 120), color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _beats() -> list[ScriptBeat]:
    return [
        ScriptBeat(start_sec=0, end_sec=3, role="hook", narration="과자 봉지 아직도 집게로 막아두세요?",
                   on_screen_text="과자 봉지 아직도?", visual="hook_text"),
        ScriptBeat(start_sec=3, end_sec=8, role="amplify", narration="하루만 지나도 눅눅해지죠.", visual="problem_scene"),
        ScriptBeat(start_sec=8, end_sec=16, role="reveal", narration="그래서 찾은 게 밀봉 클립이에요.",
                   on_screen_text="과자 밀봉 클립", visual="product_closeup"),
        ScriptBeat(start_sec=16, end_sec=24, role="benefits", narration="20개입이라 넉넉해요.",
                   on_screen_text="20개입 · 11cm · ABS", visual="product_in_use"),
        ScriptBeat(start_sec=24, end_sec=26, role="benefits", narration="리뷰가 많아요.", on_screen_text="리뷰 3,812개",
                   visual="benefit_card"),
        ScriptBeat(start_sec=26, end_sec=29, role="cta", narration="제품 정보는 프로필 링크에서 확인하세요.",
                   on_screen_text="제품 정보는 프로필 링크에서 확인하세요", visual="cta_card"),
    ]


async def _content(ctx: AppContext, ws: Workspace, product: Product, *, kind: ContentKind = ContentKind.SHORT) -> Content:
    script = Script(hooks=["과자 봉지 아직도 집게로 막아두세요?"], hook_type=HookType.QUESTION,
                    beats=_beats() if kind is ContentKind.SHORT else [], body="본문", persona="30대 여성 직장인",
                    template_id="problem-solution-v3", duration_sec=29)
    c = Content(workspace_id=ws.id, product_id=product.id, kind=kind, persona="30대 여성 직장인",
                template_id="problem-solution-v3", script=script, created_at=ctx.clock.now())
    async with ctx.uow() as r:
        await r.products.upsert(product)
        await r.contents.add(c)
    return c


# ───────────────────────────── ledger policy ─────────────────────────────


async def test_register_dedupes_by_sha256(ctx: AppContext) -> None:
    svc = AssetService(ctx)
    a = await svc.ingest_bytes(_png((10, 20, 30)), kind="image", rights_source=AssetSource.LICENSED_STOCK,
                               license="Stock Co. standard commercial", ext=".png", tags=["category:1013"])
    b = await svc.ingest_bytes(_png((10, 20, 30)), kind="image", rights_source=AssetSource.LICENSED_STOCK,
                               license="Stock Co. standard commercial", ext=".png", tags=["category:1013"])
    assert a.id == b.id
    assert a.uri == f"file://assets/{a.sha256}.png"
    async with ctx.uow() as r:
        assert (await r.assets.get(a.id)) is not None


async def test_register_rejects_policy_violations(ctx: AppContext, product_factory) -> None:  # type: ignore[no-untyped-def]
    svc = AssetService(ctx)
    sha = "a" * 64
    with pytest.raises(ValidationFailed):
        await svc.register(Asset(kind="image", uri="file://x", sha256=sha, rights_source=AssetSource.LICENSED_STOCK,
                                 license=" "))
    with pytest.raises(ValidationFailed):
        await svc.register(Asset(kind="image", uri="file://x", sha256=sha, rights_source=AssetSource.LICENSED_STOCK,
                                 license="std", expires_at=date(2020, 1, 1)))
    with pytest.raises(PolicyViolation):  # AI imagery may never stand in for the product
        await svc.register(Asset(kind="image", uri="file://x", sha256=sha,
                                 rights_source=AssetSource.AI_GENERATED_BACKGROUND, license="gen", product_id=1001))
    with pytest.raises(ValidationFailed):
        await svc.register(Asset(kind="image", uri="file://x", sha256=sha,
                                 rights_source=AssetSource.COUPANG_PRODUCT_IMAGE, license="cp"))
    with pytest.raises(ValidationFailed):
        await svc.register(Asset(kind="voice", uri="file://voice.wav", sha256=sha,
                                 rights_source=AssetSource.LICENSED_STOCK, license="tts"))


# ───────────────────────────── product imagery ─────────────────────────────


async def test_sandbox_product_image_is_deterministic_placeholder_card(ctx: AppContext, product_factory) -> None:  # type: ignore[no-untyped-def]
    assert ctx.settings.coupang.sandbox
    p = product_factory()
    async with ctx.uow() as r:
        await r.products.upsert(p)
    svc = AssetService(ctx)
    a = await svc.ensure_product_image(p)
    assert a.rights_source is AssetSource.GENERATED_CARD
    assert a.license == "acvm-generated" and a.product_id == p.id and "card:product" in a.tags
    assert (await ctx.objects.local_path(a.uri)).is_file()
    again = await svc.ensure_product_image(p)
    assert again.id == a.id and again.sha256 == a.sha256


@respx.mock
async def test_production_downloads_coupang_image_unaltered(ctx: AppContext, product_factory) -> None:  # type: ignore[no-untyped-def]
    ctx.settings.coupang.sandbox = False
    body = _jpeg()
    respx.get(CDN_URL).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"}))
    p = product_factory().model_copy(update={"image_url": CDN_URL})
    async with ctx.uow() as r:
        await r.products.upsert(p)
    a = await AssetService(ctx).ensure_product_image(p)
    assert a.rights_source is AssetSource.COUPANG_PRODUCT_IMAGE
    assert a.license == "coupang-partners-product-image" and a.product_id == p.id
    assert a.uri == f"file://assets/{a.sha256}.jpg"
    assert await ctx.objects.get_bytes(a.uri) == body  # stored byte-for-byte
    assert (await AssetService(ctx).ensure_product_image(p)).id == a.id


@respx.mock
async def test_download_host_and_payload_guards(ctx: AppContext, product_factory) -> None:  # type: ignore[no-untyped-def]
    ctx.settings.coupang.sandbox = False
    svc = AssetService(ctx)
    p = product_factory()
    with pytest.raises(PolicyViolation):  # conftest product points at image.example
        await svc.ensure_product_image(p)
    redirect = "https://image6.coupangcdn.com/redir.jpg"
    respx.get(redirect).mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/x.jpg"}))
    with pytest.raises(PolicyViolation):
        await svc.ensure_product_image(p.model_copy(update={"image_url": redirect}))
    html = "https://image7.coupangcdn.com/page.jpg"
    respx.get(html).mock(return_value=httpx.Response(200, content=b"<html></html>",
                                                     headers={"content-type": "text/html"}))
    with pytest.raises(ValidationFailed):
        await svc.ensure_product_image(p.model_copy(update={"image_url": html}))
    fake = "https://image8.coupangcdn.com/fake.jpg"
    respx.get(fake).mock(return_value=httpx.Response(200, content=b"\xff\xd8not-a-jpeg",
                                                     headers={"content-type": "image/jpeg"}))
    with pytest.raises(ValidationFailed):
        await svc.ensure_product_image(p.model_copy(update={"image_url": fake}))


def test_host_allow_list() -> None:
    assert host_allowed("https://thumbnail6.coupangcdn.com/a.jpg")
    assert host_allowed("https://www.coupang.com/vp/products/1")
    assert not host_allowed("http://thumbnail6.coupangcdn.com/a.jpg")
    assert not host_allowed("https://coupangcdn.com.evil.example/a.jpg")
    assert not host_allowed("https://evilcoupang.com/a.jpg")
    assert not host_allowed("https://user:pw@www.coupang.com/a.jpg")
    assert not host_allowed("https://www.coupang.com:8443/a.jpg")


# ───────────────────────────── selection ─────────────────────────────


async def test_select_for_follows_source_priority(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory()
    c = await _content(ctx, workspace, p)
    svc = AssetService(ctx)
    coupang = await svc.register(Asset(kind="image", uri="file://assets/cp.jpg", sha256="c" * 64,
                                       rights_source=AssetSource.COUPANG_PRODUCT_IMAGE,
                                       license="coupang-partners-product-image", product_id=p.id))
    await ctx.objects.put_bytes(_jpeg(), "assets/cp.jpg", "image/jpeg")
    own = await svc.ingest_bytes(_png((1, 2, 3)), kind="image", rights_source=AssetSource.OWN_FOOTAGE,
                                 license="own shoot", ext=".png", product_id=p.id, tags=[f"product:{p.id}"])
    stock = await svc.ingest_bytes(_png((4, 5, 6)), kind="image", rights_source=AssetSource.LICENSED_STOCK,
                                   license="stock std", ext=".png", tags=[f"category:{p.category_id}"])
    ai = await svc.ingest_bytes(_png((7, 8, 9)), kind="image", rights_source=AssetSource.AI_GENERATED_BACKGROUND,
                                license="ai bg", ext=".png", tags=[p.category_id])

    picks = await svc.select_for(c, p)
    assert set(picks) == set(range(6))
    assert picks[2].id == own.id and picks[3].id == own.id  # product beats → own footage first
    assert picks[1].id == stock.id  # scene beat → category stock (never the product photo)
    for i in (0, 4, 5):  # text beats → generated cards
        assert picks[i].rights_source is AssetSource.GENERATED_CARD and "card" in picks[i].tags
    async with ctx.uow() as r:  # R-01: everything returned is a ledger row
        rows = await r.assets.get_many([a.id for a in picks.values()])
    assert {a.id for a in picks.values()} <= set(rows)

    # expire own footage and stock → Coupang image for product beats, AI background for the scene beat
    async with ctx.uow() as r:
        from acvm.db import tables as t

        for aid in (own.id, stock.id):
            row = await r.session.get(t.AssetRow, aid)
            assert row is not None
            row.expires_at = date(2026, 10, 1)
    picks2 = await svc.select_for(c, p)
    assert picks2[2].id == coupang.id and picks2[3].id == coupang.id
    assert picks2[1].id == ai.id


async def test_select_without_any_media_uses_cards_and_is_stable(ctx: AppContext, workspace: Workspace,
                                                                  product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory(pid=2002)
    c = await _content(ctx, workspace, p)
    svc = AssetService(ctx)
    first = await svc.select_for(c, p)
    second = await svc.select_for(c, p)
    assert {i: a.id for i, a in first.items()} == {i: a.id for i, a in second.items()}
    assert all(a.rights_source is AssetSource.GENERATED_CARD for a in first.values())
    assert "card:product" in first[2].tags and first[2].product_id == p.id
    assert "card:scene" in first[1].tags and "card:cta" in first[5].tags and "card:benefit" in first[4].tags


async def test_product_in_use_prefers_own_video(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory(pid=3003)
    c = await _content(ctx, workspace, p)
    svc = AssetService(ctx)
    await svc.ingest_bytes(_png((9, 9, 9)), kind="image", rights_source=AssetSource.OWN_FOOTAGE, license="own",
                           ext=".png", product_id=p.id)
    vid = await svc.ingest_bytes(b"\x00\x00\x00\x18ftypmp42fake-video", kind="video",
                                 rights_source=AssetSource.OWN_FOOTAGE, license="own", ext=".mp4", product_id=p.id)
    picks = await svc.select_for(c, p)
    assert picks[3].id == vid.id


async def test_select_for_rejects_text_content(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    p = product_factory(pid=4004)
    c = await _content(ctx, workspace, p, kind=ContentKind.TEXT)
    with pytest.raises(ValidationFailed):
        await AssetService(ctx).select_for(c, p)


# ───────────────────────────── voices & events ─────────────────────────────


@pytest.mark.parametrize(("persona", "voice"), [
    ("30대 여성 직장인", "ko-F-30s-brisk"),
    ("20대 대학생 여자", "ko-F-20s-bright"),
    ("40대 주부", "ko-F-40s-lively"),
    ("50대 여성", "ko-F-40s-lively"),
    ("아나운서 톤 30대 여성", "ko-F-30s-rapid"),
    ("ASMR 좋아하는 20대", "ko-F-20s-bright"),
    ("30대 남성 캠핑족", "ko-M-30s-brisk"),
    ("40대 남자", "ko-M-40s-punchy"),
    ("50대 남성 등산", "ko-M-40s-punchy"),
    ("직장인", "ko-F-30s-brisk"),
])
def test_persona_voice_mapping(persona: str, voice: str) -> None:
    assert voice_id_for_persona(persona) == voice


async def test_voice_for_registers_provider_licensed_voice(ctx: AppContext) -> None:
    ctx.extras["tts.provider"] = "acvm-tone"
    svc = AssetService(ctx)
    v = await svc.voice_for("30대 여성 직장인")
    assert v.kind == "voice" and v.uri == "tts:acvm-tone:ko-F-30s-brisk"
    assert v.rights_source is AssetSource.LICENSED_STOCK and v.license == "acvm-tone commercial TTS license"
    assert (await svc.voice_for("30대 여성")).id == v.id
    el = await svc.voice_for("20대 여자", provider="elevenlabs")
    assert el.uri == "tts:elevenlabs:ko-F-20s-bright" and el.license == "elevenlabs commercial TTS license"
    chosen = await svc.voice_for("30대 여성", voice_id="ko-M-40s-punchy")
    assert chosen.uri == "tts:acvm-tone:ko-M-40s-punchy"


async def test_handle_script_ready_prepares_ledger(ctx: AppContext, workspace: Workspace, product_factory) -> None:  # type: ignore[no-untyped-def]
    ctx.extras["tts.provider"] = "acvm-tone"
    p = product_factory(pid=5005)
    c = await _content(ctx, workspace, p)
    ev = Event.of(ScriptReady(content_id=c.id, claims=[], persona=c.persona))
    visuals = await AssetService(ctx).handle_script_ready(ev)
    assert len(visuals) == 6
    async with ctx.uow() as r:
        voices = await r.assets.by_tag("voice", kind="voice")
    assert any(v.uri.endswith("ko-F-30s-brisk") for v in voices)


async def test_ingest_file_roundtrip(ctx: AppContext, tmp_path: Path) -> None:
    f = tmp_path / "broll.png"
    f.write_bytes(_png((50, 60, 70)))
    a = await AssetService(ctx).ingest_file(f, kind="image", rights_source=AssetSource.OWN_FOOTAGE,
                                            license="own shoot 2026", tags=["category:1013"],
                                            expires_at=date.today() + timedelta(days=365))
    assert await ctx.objects.get_bytes(a.uri) == f.read_bytes()
